"""DictateText para macOS: dicta con la voz y pega el texto donde estaba el cursor.

Atajos globales (configurables en .env):
    Ctrl+Alt+D        empezar a dictar / terminar y pegar (toggle)
    Ctrl+Alt+X        cancelar el dictado en curso (descarta)
    Ctrl+Alt+Shift+D  salir

Flujo: hotkey -> guarda la app activa -> graba el microfono mientras Azure
transcribe en streaming -> hotkey de nuevo -> reactiva la app original,
pone el texto en el clipboard y simula Cmd+V. El clipboard anterior se
restaura despues de pegar.

Equivalencias con la version Windows (main.py):
    RegisterHotKey        -> event tap de Quartz (CGEventTapCreate)
    GetForegroundWindow   -> NSWorkspace.frontmostApplication
    SetForegroundWindow   -> NSRunningApplication.activateWithOptions_
    clipboard Win32       -> NSPasteboard
    SendInput Ctrl+V      -> CGEventPost Cmd+V
    WS_EX_NOACTIVATE      -> ::tk::unsupported::MacWindowStyle help/noActivates
    media.py (winrt)      -> media_mac.py (AppleScript: Spotify/Music)

Permisos que macOS va a pedir (una sola vez, a la app que corre esto —
Terminal, iTerm o VS Code):
    - Accesibilidad: para el event tap (hotkeys) y para inyectar Cmd+V.
    - Microfono: para grabar.
    - Automatizacion (Spotify/Music): para pausar la musica al dictar.
El STT (stt.py) y el grabador (recorder.py) son los mismos que en Windows.
"""

import fcntl
import logging
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import tkinter as tk
import wave
from pathlib import Path
from tkinter import scrolledtext

import Quartz
from AppKit import (NSApplication, NSApplicationActivateIgnoringOtherApps,
                    NSPasteboard, NSPasteboardTypeString, NSScreen, NSSound,
                    NSWorkspace)
from dotenv import load_dotenv, set_key

from media_mac import MediaDucker
from recorder import (MicRecorder, list_input_devices, refresh_devices,
                      resolve_candidates)
from stt import AzureSpeechSTT

APP_DIR = Path(__file__).resolve().parent

log = logging.getLogger("dictate_text")

# --- Constantes Quartz --------------------------------------------------------

MASK_CTRL = Quartz.kCGEventFlagMaskControl
MASK_ALT = Quartz.kCGEventFlagMaskAlternate
MASK_SHIFT = Quartz.kCGEventFlagMaskShift
MASK_CMD = Quartz.kCGEventFlagMaskCommand
MASK_FN = Quartz.kCGEventFlagMaskSecondaryFn  # la tecla fn/🌐 de los teclados Apple
MASK_ALL_MODS = MASK_CTRL | MASK_ALT | MASK_SHIFT | MASK_CMD | MASK_FN

MOD_NAMES = {"ctrl": MASK_CTRL, "alt": MASK_ALT, "option": MASK_ALT,
             "opt": MASK_ALT, "shift": MASK_SHIFT, "cmd": MASK_CMD,
             "fn": MASK_FN, "globe": MASK_FN}

# Virtual keycodes ANSI (kVK_ANSI_*): son posiciones fisicas del teclado US;
# para letras y digitos coinciden en casi todos los layouts latinos.
KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7,
    "c": 8, "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15,
    "y": 16, "t": 17, "o": 31, "u": 32, "i": 34, "p": 35, "l": 37,
    "j": 38, "k": 40, "n": 45, "m": 46,
    "1": 18, "2": 19, "3": 20, "4": 21, "5": 23, "6": 22, "7": 26,
    "8": 28, "9": 25, "0": 29,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97,
    "f7": 98, "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
}
VK_V = KEYCODES["v"]

HK_DICTATE, HK_CANCEL, HK_QUIT = 1, 2, 3


# --- Instancia unica -----------------------------------------------------------

_lock_handle = None  # mantiene el flock vivo mientras corre la app
_LOCK_PATH = APP_DIR / ".dictate_text.lock"


def acquire_single_instance() -> bool:
    """True si somos la primera instancia. Si ya hay otra, le mandamos
    SIGUSR1 (el equivalente del evento "show" de la version Windows) para
    que muestre su ventana, y devolvemos False."""
    global _lock_handle
    # "a+" y no "w": abrir con "w" truncaria el PID de la instancia que ya
    # esta corriendo antes de saber si el lock esta tomado.
    _lock_handle = open(_LOCK_PATH, "a+")
    try:
        fcntl.flock(_lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            pid = int(_LOCK_PATH.read_text().strip() or 0)
            if pid:
                os.kill(pid, signal.SIGUSR1)
        except Exception:
            pass
        return False
    _lock_handle.truncate(0)
    _lock_handle.seek(0)
    _lock_handle.write(str(os.getpid()))
    _lock_handle.flush()
    return True


# --- Permiso de Accesibilidad ---------------------------------------------------

def ensure_accessibility() -> bool:
    """True si el proceso puede usar event taps e inyectar teclas. Si no,
    dispara el dialogo del sistema para concederlo (una sola vez)."""
    try:
        from ApplicationServices import (AXIsProcessTrustedWithOptions,
                                         kAXTrustedCheckOptionPrompt)
        return bool(AXIsProcessTrustedWithOptions(
            {kAXTrustedCheckOptionPrompt: True}))
    except Exception:
        log.debug("No pude consultar AXIsProcessTrusted", exc_info=True)
        return True  # que lo intente igual; el tap va a fallar si no hay permiso


# --- Sonidos --------------------------------------------------------------------

def _play_sound(name: str) -> None:
    try:
        snd = NSSound.soundNamed_(name)
        if snd:
            snd.stop()
            snd.play()
    except Exception:
        log.debug("No pude reproducir el sonido %s", name, exc_info=True)


def beep_ready() -> None:   # "ya podes hablar"
    _play_sound("Tink")


def beep_warn() -> None:    # error / no entendi
    _play_sound("Basso")


# --- Teclas sinteticas (Cmd+V) ---------------------------------------------------

def _wait_modifiers_released(timeout: float = 0.5) -> None:
    """Espera a que el usuario suelte fisicamente los modificadores del hotkey:
    si Ctrl/Alt siguen apretados, el Cmd+V sintetico llega como Cmd+Ctrl+Alt+V
    y la app destino no pega."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        flags = Quartz.CGEventSourceFlagsState(
            Quartz.kCGEventSourceStateCombinedSessionState)
        if not flags & MASK_ALL_MODS:
            return
        time.sleep(0.01)


def send_cmd_v() -> None:
    _wait_modifiers_released()
    down = Quartz.CGEventCreateKeyboardEvent(None, VK_V, True)
    up = Quartz.CGEventCreateKeyboardEvent(None, VK_V, False)
    Quartz.CGEventSetFlags(down, MASK_CMD)
    Quartz.CGEventSetFlags(up, MASK_CMD)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
    time.sleep(0.01)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)


# --- Clipboard -------------------------------------------------------------------

def get_clipboard_text() -> str | None:
    try:
        text = NSPasteboard.generalPasteboard().stringForType_(NSPasteboardTypeString)
        return str(text) if text is not None else None
    except Exception:
        log.debug("No pude leer el clipboard", exc_info=True)
        return None


def set_clipboard_text(text: str) -> None:
    try:
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(text, NSPasteboardTypeString)
    except Exception:
        log.debug("No pude escribir el clipboard", exc_info=True)


# --- App destino (a donde volver a pegar) -----------------------------------------

def frontmost_app():
    """NSRunningApplication de la app activa (el equivalente del hwnd destino)."""
    return NSWorkspace.sharedWorkspace().frontmostApplication()


def focused_ax_window(app):
    """Referencia AX a la VENTANA enfocada de la app (no alcanza con la app:
    si tiene ventanas en varios escritorios, activarla a secas puede llevar
    a macOS a otro Space; con la ventana exacta volvemos a la del cursor).
    Requiere el permiso de Accesibilidad, que ya tenemos por el event tap."""
    if app is None:
        return None
    try:
        from ApplicationServices import (AXUIElementCopyAttributeValue,
                                         AXUIElementCreateApplication)
        ax_app = AXUIElementCreateApplication(app.processIdentifier())
        err, win = AXUIElementCopyAttributeValue(ax_app, "AXFocusedWindow", None)
        return win if err == 0 else None
    except Exception:
        log.debug("No pude leer la ventana enfocada via AX", exc_info=True)
        return None


def raise_ax_window(window) -> bool:
    try:
        from ApplicationServices import AXUIElementPerformAction
        return AXUIElementPerformAction(window, "AXRaise") == 0
    except Exception:
        log.debug("AXRaise fallo", exc_info=True)
        return False


def app_title(app) -> str:
    if app is None:
        return "(desconocida)"
    return str(app.localizedName() or "(sin nombre)")


def _wait_frontmost(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        front = frontmost_app()
        if front is not None and front.processIdentifier() == pid:
            return True
        time.sleep(0.02)
    return False


def reactivate_app(app, window=None, timeout: float = 1.5) -> bool:
    """Vuelve a activar la app donde empezo el dictado — y su VENTANA exacta:
    AXRaise primero, para que la activacion enfoque esa ventana (que esta en
    el escritorio del usuario) y no la ultima usada de la app, que puede
    vivir en otro Space. El campo conserva caret y seleccion."""
    if app is None or app.isTerminated():
        return False
    pid = app.processIdentifier()
    if window is not None:
        raise_ax_window(window)
    front = frontmost_app()
    if front is not None and front.processIdentifier() == pid:
        return True
    app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
    if _wait_frontmost(pid, timeout / 2):
        return True
    app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
    return _wait_frontmost(pid, timeout / 2)


# --- Logica de la app --------------------------------------------------------------

class App:
    """Maquina de estados: idle -> recording -> finishing -> idle.
    (Identica a la version Windows; cambian solo las primitivas de sistema.)"""

    def __init__(self, stt: AzureSpeechSTT, ducker: MediaDucker, get_device,
                 max_seconds: int, restore_clipboard: bool, restore_delay: float,
                 notify=lambda msg: None, set_overlay=lambda mode: None):
        self.stt = stt
        self.ducker = ducker
        self.get_device = get_device  # callable: nombre del mic elegido en la GUI
        self.max_seconds = max_seconds
        self.restore_clipboard = restore_clipboard
        self.restore_delay = restore_delay
        self.notify = notify
        self.set_overlay = set_overlay
        self.recorder: MicRecorder | None = None
        self.record_started = 0.0
        self.target_app = None
        self.target_window = None  # ventana AX exacta (por los Spaces)
        self.target_title = ""
        self._state = "idle"
        self._lock = threading.Lock()
        self._session = 0
        self._last_hotkey = 0.0
        self._audio = bytearray()  # copia de la sesion para last_dictation.wav

    @property
    def state(self) -> str:
        return self._state

    def level(self) -> float:
        rec = self.recorder
        return rec.level if rec else 0.0

    def elapsed(self) -> float:
        return time.monotonic() - self.record_started if self._state == "recording" else 0.0

    def on_dictate_hotkey(self) -> None:
        with self._lock:
            # Refuerzo anti-repeat: eventos a menos de 300ms solo pueden ser
            # key repeat o un rebote, nunca un toggle intencional.
            now = time.monotonic()
            last, self._last_hotkey = self._last_hotkey, now
            if now - last < 0.3:
                return
            if self._state == "idle":
                self._state = "recording"
                self._session += 1
                sid = self._session
                action = "start"
            elif self._state == "recording":
                self._state = "finishing"
                action = "finish"
            else:
                return  # ya esta transcribiendo/pegando; ignorar
        try:
            if action == "start":
                self._start(sid)
            else:
                self._finish()
        except Exception:
            log.exception("Error en el handler del hotkey")
            self.notify("ERROR — ver log")
            with self._lock:
                self._state = "idle"
            self.set_overlay(None)

    def cancel(self) -> None:
        with self._lock:
            if self._state != "recording":
                return
            self._state = "finishing"
        mic_bt = True
        if self.recorder:
            mic_bt = "macbook" not in self.recorder.device_name().lower()
            self.recorder.stop()
            self.recorder = None
        self.stt.cancel()
        self.set_overlay(None)
        self.notify("Dictado cancelado")
        log.info("Dictado cancelado")
        with self._lock:
            self._state = "idle"
        self._resume_music_later(time.monotonic(), mic_bt)
        threading.Thread(target=self._refresh_mic_cache, daemon=True).start()

    def _resume_music_later(self, mic_closed_at: float | None = None,
                            bluetooth: bool = True) -> None:
        """Reanuda la musica recien cuando los AirPods volvieron al perfil de
        musica (~2s despues de CERRAR el mic — el reloj corre desde ahi, no
        desde el pegado: el cierre de Azure ya consume ~1s de esa espera).
        Con un mic de cable/integrado no hubo cambio de perfil: reanudacion
        inmediata. Si el usuario ya arranco otro dictado para cuando vence
        el timer, no tocar nada."""
        if not bluetooth:
            self.ducker.resume_paused_async()
            return
        anchor = mic_closed_at if mic_closed_at is not None else time.monotonic()
        delay = max(0.0, anchor + 2.0 - time.monotonic())
        if delay < 0.05:
            self.ducker.resume_paused_async()
            return
        sid = self._session

        def fire() -> None:
            if self._state == "idle" and self._session == sid:
                self.ducker.resume_paused()

        timer = threading.Timer(delay, fire)
        timer.daemon = True
        timer.start()

    def _refresh_mic_cache(self) -> None:
        """Re-escanea PortAudio para que un cambio de dispositivos (conectar
        los AirPods, enchufar un mic) se vea en el proximo dictado. El default
        del sistema lo resuelve CoreAudio solo: sigue al aparato en uso."""
        try:
            refresh_devices()
        except Exception:
            log.debug("No pude refrescar el cache de mic", exc_info=True)

    def _start(self, sid: int) -> None:
        t_press = time.monotonic()
        # Resetear YA el reloj del overlay: se muestra antes de que el mic
        # este listo, y si no quedaria contando desde la sesion anterior.
        self.record_started = t_press
        # PRIMERO la app y la ventana destino, antes de tocar cualquier otra cosa.
        self.target_app = frontmost_app()
        self.target_window = focused_ax_window(self.target_app)
        self.target_title = app_title(self.target_app)
        self._audio = bytearray()

        def on_chunk(pcm: bytes) -> None:
            self._audio.extend(pcm)
            self.stt.feed(pcm)

        # "warming" = ⏳ todavia NO hables (el beep avisa cuando si).
        self.set_overlay("warming")
        # La musica se pausa ANTES de abrir el mic, a proposito y en secuencia
        # (en Windows va en paralelo): abrir el mic de unos AirPods los cambia
        # al perfil de llamada con un salto de volumen — mejor que el video ya
        # este pausado cuando eso pase. Cuesta <1s antes del beep.
        self.ducker.pause_playing()
        # Vacio = default del sistema: CoreAudio sigue al aparato en uso
        # (AirPods si estan conectados, el integrado si no) — mismo espiritu
        # que el mic "de comunicaciones" en Windows. recorder.py lo abre al
        # rate nativo (los AirPods a 16 kHz entregan silencio).
        mic_name = self.get_device()
        try:
            self.stt.start()
            rec = MicRecorder(device=resolve_candidates(mic_name), on_chunk=on_chunk)
            rec.start()
        except Exception:
            log.exception("No pude arrancar el dictado (¿microfono? ¿permisos?)")
            self.notify("ERROR al arrancar (¿microfono? ¿permisos?) — ver log")
            beep_warn()
            self.stt.cancel()
            self.set_overlay(None)
            with self._lock:
                self._state = "idle"
            return
        self.recorder = rec
        # El beep significa "ya podes hablar": suena recien cuando el mic
        # entrego SEÑAL de verdad (first_audio), no el primer chunk a secas —
        # mientras los AirPods cambian de perfil, CoreAudio entrega chunks de
        # puros ceros y lo hablado en ese rato no existe para nadie.
        if rec.first_audio.wait(4.0):
            log.info("Mic captando en %d ms — destino: %s",
                     (time.monotonic() - t_press) * 1000, self.target_title)
        else:
            log.warning("El mic no entrego señal en 4s (¿mic muteado?) — "
                        "destino: %s", self.target_title)
        self.record_started = time.monotonic()
        self.set_overlay("recording")  # ahora si: 🔴 habla
        beep_ready()
        timer = threading.Timer(self.max_seconds, lambda: self._auto_stop(sid))
        timer.daemon = True
        timer.start()

    def _auto_stop(self, sid: int) -> None:
        with self._lock:
            if self._state != "recording" or self._session != sid:
                return
            self._state = "finishing"
        log.info("Corte automatico a los %d segundos (MAX_SECONDS)", self.max_seconds)
        self._finish()

    def _finish(self) -> None:
        self.set_overlay("finishing")
        audio_secs, peak = 0.0, 0.0
        mic_closed_at, mic_bt = time.monotonic(), True
        if self.recorder:
            audio_secs = self.recorder.bytes_total / 32000  # 16 kHz * 2 bytes
            peak = self.recorder.peak
            # ¿Hubo cambio de perfil Bluetooth? Con el mic integrado no hay
            # nada que esperar para devolver la musica.
            mic_bt = "macbook" not in self.recorder.device_name().lower()
            self.recorder.stop()
            self.recorder = None
            mic_closed_at = time.monotonic()
        log.info("Fin del dictado: %.1fs de audio, pico de mic %.3f", audio_secs, peak)
        self._save_debug_wav()
        t0 = time.monotonic()
        try:
            text = self.stt.stop()
        except Exception:
            log.exception("Error cerrando el reconocimiento")
            text = ""
        log.info("Reconocimiento cerrado en %.1fs", time.monotonic() - t0)
        self.set_overlay(None)
        if text:
            self._paste(text)
        elif audio_secs < 1.0:
            log.warning("Grabacion de %.1fs: se corto casi al instante", audio_secs)
            self.notify("Grabacion demasiado corta — no pego")
            beep_warn()
        else:
            log.warning("Azure no reconocio voz en %.1fs de audio (pico %.3f); "
                        "audio guardado en last_dictation.wav", audio_secs, peak)
            if peak < 0.15:
                log.warning("Nivel de mic bajo: probablemente esta escuchando el "
                            "microfono equivocado — elegi otro en el desplegable "
                            "de la ventana de DictateText")
            self.notify("No entendi nada — no pego (ver log)")
            beep_warn()
        with self._lock:
            self._state = "idle"
        self._resume_music_later(mic_closed_at, mic_bt)
        threading.Thread(target=self._refresh_mic_cache, daemon=True).start()

    def _save_debug_wav(self) -> None:
        """Guarda el audio de la sesion: si Azure no reconocio nada, este
        archivo dice si el problema es el mic (silencio/voz lejana) o Azure."""
        if not self._audio:
            return
        try:
            with wave.open(str(APP_DIR / "last_dictation.wav"), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(bytes(self._audio))
        except Exception:
            log.debug("No pude guardar last_dictation.wav", exc_info=True)

    def _paste(self, text: str) -> None:
        log.info("Transcripto %d caracteres: %.80r", len(text), text)
        old_clip = get_clipboard_text()
        set_clipboard_text(text)
        if get_clipboard_text() != text:  # clipboard tomado por otra app
            set_clipboard_text(text)
        if not reactivate_app(self.target_app, self.target_window):
            # Nunca pegar a ciegas en cualquier lado. El texto queda en el
            # clipboard (por eso aca NO se restaura el anterior).
            log.warning("No pude reactivar %r; el texto queda en el clipboard",
                        self.target_title)
            self.notify(f"No pude volver a «{self.target_title}» — "
                        "el texto quedo en el portapapeles (Cmd+V)")
            beep_warn()
            return
        send_cmd_v()
        self.notify(f"Pegado en «{self.target_title}»")
        log.info("Pegado en %s", self.target_title)
        if self.restore_clipboard and old_clip is not None:
            # Darle tiempo a la app destino a leer el clipboard antes de pisarlo.
            time.sleep(self.restore_delay)
            set_clipboard_text(old_clip)


# --- Hotkeys globales (event tap de Quartz en thread propio) -----------------------

class HotkeyThread(threading.Thread):
    """Event tap de Quartz con su propio CFRunLoop. A diferencia de
    RegisterHotKey en Windows, el tap ve TODAS las teclas: matcheamos
    keycode+modificadores a mano y nos tragamos el evento (return None)
    para que el hotkey no llegue ademas a la app activa."""

    def __init__(self, bindings: dict[int, tuple[str, "callable"]]):
        super().__init__(daemon=True, name="hotkeys")
        self.bindings = bindings
        self.errors: list[str] = []
        self.ready = threading.Event()
        self._parsed: list[tuple[int, int, "callable", str]] = []
        self._tap = None
        self._runloop = None
        self._last_fire: dict[int, float] = {}

    def run(self) -> None:
        for hk_id, (spec, cb) in self.bindings.items():
            try:
                mods, keycode = parse_hotkey(spec)
            except ValueError as e:
                self.errors.append(str(e))
                continue
            self._parsed.append((mods, keycode, cb, spec))

        mask = Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
        self._tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap,
            Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionDefault,
            mask,
            self._on_event,
            None,
        )
        if not self._tap:
            # Escuchar el teclado pide "Monitoreo de entrada" (Input
            # Monitoring), un permiso DISTINTO de Accesibilidad (que cubre
            # inyectar el Cmd+V). Hacen falta los dos.
            self.errors.append(
                "No pude crear el event tap de hotkeys: falta un permiso — "
                "revisar Ajustes → Privacidad y seguridad → 【Monitoreo de "
                "entrada】 Y 【Accesibilidad】 (los dos), y reabrir la app"
            )
            self.ready.set()
            return
        source = Quartz.CFMachPortCreateRunLoopSource(None, self._tap, 0)
        self._runloop = Quartz.CFRunLoopGetCurrent()
        Quartz.CFRunLoopAddSource(self._runloop, source,
                                  Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(self._tap, True)
        self.ready.set()
        log.info("Event tap creado: hotkeys activos (%s)",
                 ", ".join(spec for _m, _k, _cb, spec in self._parsed))
        Quartz.CFRunLoopRun()

    def _on_event(self, proxy, type_, event, refcon):
        # macOS desactiva el tap si un callback tarda demasiado: reactivarlo.
        if type_ in (Quartz.kCGEventTapDisabledByTimeout,
                     Quartz.kCGEventTapDisabledByUserInput):
            Quartz.CGEventTapEnable(self._tap, True)
            return event
        try:
            keycode = Quartz.CGEventGetIntegerValueField(
                event, Quartz.kCGKeyboardEventKeycode)
            flags = Quartz.CGEventGetFlags(event) & MASK_ALL_MODS
            for mods, kc, cb, _spec in self._parsed:
                if keycode == kc and flags == mods:
                    # El autorepeat de mantener el hotkey apretado dispararia
                    # el toggle mil veces: tragarlo sin ejecutar nada.
                    if not Quartz.CGEventGetIntegerValueField(
                            event, Quartz.kCGKeyboardEventAutorepeat):
                        threading.Thread(target=cb, daemon=True).start()
                    return None  # el hotkey no llega a la app activa
        except Exception:
            log.exception("Error en el callback del event tap")
        return event

    def stop(self) -> None:
        if self._runloop is not None:
            Quartz.CFRunLoopStop(self._runloop)


def parse_hotkey(spec: str) -> tuple[int, int]:
    """'ctrl+alt+d' -> (mascara de modificadores, virtual keycode)."""
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    mods, keycode = 0, None
    for p in parts:
        if p in MOD_NAMES:
            mods |= MOD_NAMES[p]
        elif p in KEYCODES:
            keycode = KEYCODES[p]
        else:
            raise ValueError(f"Tecla no soportada en hotkey: {p!r}")
    if keycode is None:
        raise ValueError(f"Hotkey sin tecla principal: {spec!r}")
    return mods, keycode


# --- Ventana ------------------------------------------------------------------------

class QueueLogHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def emit(self, record: logging.LogRecord) -> None:
        self.q.put(("log", self.format(record)))


# Paleta oscura (Catppuccin Mocha-ish, igual que la version Windows)
BG = "#1e1e2e"
PANEL = "#313244"
PANEL_HI = "#45475a"
FG = "#cdd6f4"
MUTED = "#7f849c"
ACCENT = "#89b4fa"
GREEN = "#a6e3a1"
YELLOW = "#f9e2af"
RED = "#f38ba8"

FONT = "Helvetica Neue"
MONO = "Menlo"

IDLE_MSG = "En espera — cursor en un campo de texto y apreta el atajo"


def _fmt_time(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


def _work_area() -> tuple[int, int, int, int]:
    """Area de pantalla sin la barra de menu ni el Dock (left, top, right,
    bottom) en coordenadas de Tk (origen arriba a la izquierda; las de Cocoa
    tienen el origen abajo)."""
    try:
        screen = NSScreen.screens()[0]  # la pantalla con la barra de menu
        full, vis = screen.frame(), screen.visibleFrame()
        left = int(vis.origin.x)
        right = int(vis.origin.x + vis.size.width)
        top = int(full.size.height - (vis.origin.y + vis.size.height))
        bottom = int(full.size.height - vis.origin.y)
        return left, top, right, bottom
    except Exception:
        log.debug("No pude leer el area visible de la pantalla", exc_info=True)
        return 0, 0, 1440, 900


def pin_overlay(tk_toplevel) -> None:
    """Hace que el overlay viva en TODOS los escritorios (Spaces) y flote
    incluso sobre apps fullscreen. Sin esto, mostrar el overlay activa la app
    y macOS ARRASTRA al usuario al Space donde quedo la ventana principal de
    DictateText; con el pin, el foco cae en el overlay (que existe en el
    Space actual) y nadie se mueve. El Cmd+V no corre riesgo: _paste()
    siempre reactiva la app/ventana destino antes de pegar.
    (Solucion portada de SpeakSelectedText/native_mac.py.)"""
    try:
        tk_toplevel.update_idletasks()
        want_h = tk_toplevel.winfo_reqheight()
        # sharedApplication() recien existe despues del tk.Tk() de la GUI.
        for w in NSApplication.sharedApplication().windows():
            # El overlay es la unica ventana sin barra de titulo
            # (overrideredirect); el matching por altura la desambigua.
            if w.styleMask() & 1:  # NSWindowStyleMaskTitled -> ventana normal
                continue
            if abs(int(w.frame().size.height) - want_h) > 24:
                continue
            w.setCollectionBehavior_(
                w.collectionBehavior()
                | (1 << 0)   # CanJoinAllSpaces: visible en todos los Spaces
                | (1 << 4)   # Stationary: Mission Control no la mueve
                | (1 << 8)   # FullScreenAuxiliary: tambien sobre fullscreen
            )
            w.setLevel_(25)  # NSStatusWindowLevel: sobre ventanas comunes
            return
        log.debug("No encontre el NSWindow del overlay")
    except Exception:
        log.debug("No pude fijar el overlay a todos los Spaces", exc_info=True)


def run_gui(app: App, ui_q: queue.Queue, hotkeys: HotkeyThread,
            specs: dict[str, str], languages: list[str], mic: dict) -> None:
    root = tk.Tk()
    root.title("DictateText")
    root.geometry("680x500")
    root.configure(bg=BG)
    root.attributes("-topmost", True)
    root.after(2000, lambda: root.attributes("-topmost", False))  # asomar al abrir

    # -- header
    header = tk.Frame(root, bg=BG)
    header.pack(fill=tk.X, padx=14, pady=(12, 0))
    tk.Label(header, text="DictateText", bg=BG, fg=ACCENT,
             font=(FONT, 16, "bold")).pack(side=tk.LEFT)
    tk.Label(header, text=" · ".join(languages), bg=BG, fg=MUTED,
             font=(FONT, 11)).pack(side=tk.RIGHT)

    # -- estado
    status = tk.StringVar(value=IDLE_MSG)
    status_lbl = tk.Label(root, textvariable=status, bg=BG, fg=MUTED,
                          font=(FONT, 15, "bold"))
    status_lbl.pack(pady=(10, 2))

    # -- transcripcion en vivo
    live = tk.StringVar(value="")
    tk.Label(root, textvariable=live, bg=BG, fg=FG, font=(FONT, 12),
             wraplength=640, justify=tk.LEFT, height=3, anchor="n"
             ).pack(fill=tk.X, padx=14)

    # -- microfono y salir
    controls = tk.Frame(root, bg=BG)
    controls.pack(pady=(2, 0))

    mics = ["(default)"] + [name for _idx, name in list_input_devices()]
    mic_var = tk.StringVar(value=mic["name"] if mic["name"] in mics else "(default)")

    def on_mic(name: str) -> None:
        mic["name"] = "" if name == "(default)" else name
        set_key(str(APP_DIR / ".env"), "MIC_DEVICE", mic["name"], quote_mode="never")
        log.info("Microfono: %s (aplica al proximo dictado)", name)

    tk.Label(controls, text="Microfono:", bg=BG, fg=MUTED,
             font=(FONT, 11)).pack(side=tk.LEFT)
    mm = tk.OptionMenu(controls, mic_var, *mics, command=on_mic)
    mm.configure(bg=PANEL, fg=FG, activebackground=PANEL_HI, activeforeground=FG,
                 bd=0, relief=tk.FLAT, font=(FONT, 11), highlightthickness=0,
                 cursor="hand2")
    mm["menu"].configure(bg=PANEL, fg=FG, activebackground=PANEL_HI,
                         activeforeground=FG, bd=0)
    mm.pack(side=tk.LEFT, padx=(6, 18))
    tk.Button(controls, text="✕ Salir", command=lambda: root.destroy(),
              bg=PANEL, fg=FG, activebackground=PANEL_HI, activeforeground=FG,
              bd=0, relief=tk.FLAT, font=(FONT, 12), padx=12, pady=4,
              cursor="hand2").pack(side=tk.LEFT)

    # -- campo de prueba: como la GUI es una ventana mas, dictar con el cursor
    #    aca adentro prueba el circuito completo sin salir de la app
    tk.Label(root, text="Campo de prueba — clic adentro y dicta con el atajo:",
             bg=BG, fg=MUTED, font=(FONT, 11)).pack(pady=(10, 2))
    testbox = tk.Text(root, height=3, font=(FONT, 12), bg=PANEL, fg=FG,
                      insertbackground=FG, bd=0, relief=tk.FLAT, wrap=tk.WORD,
                      padx=8, pady=6)
    testbox.pack(fill=tk.X, padx=14)

    # -- atajos
    tk.Label(
        root,
        text=(f"{specs['dictate'].upper()} dictar / pegar   ·   "
              f"{specs['cancel'].upper()} cancelar   ·   "
              f"{specs['quit'].upper()} salir"),
        bg=BG, fg=MUTED, font=(FONT, 11),
    ).pack(pady=(8, 2))

    # -- log
    logbox = scrolledtext.ScrolledText(
        root, height=8, state=tk.DISABLED, font=(MONO, 10),
        bg="#181825", fg=MUTED, insertbackground=FG, bd=0, relief=tk.FLAT,
    )
    logbox.pack(fill=tk.BOTH, expand=True, padx=14, pady=(4, 12))

    # -- overlay de grabacion (abajo a la derecha, nunca toma foco) ------------
    mini = tk.Toplevel(root)
    mini.withdraw()
    # Sin decoracion PERO activable (clase "simple"): overrideredirect en
    # Aqua marca la ventana como NO-activable, y entonces al mostrarse el
    # overlay la activacion caeria en la ventana principal — que puede vivir
    # en OTRO escritorio, y macOS arrastra al usuario hasta ahi. Con una
    # ventana activable y pineada a todos los Spaces (pin_overlay), el foco
    # cae en el overlay del Space actual y nadie se mueve.
    # (Mismo esquema que SpeakSelectedText/main.py.)
    mini.tk.call("::tk::unsupported::MacWindowStyle", "style",
                 mini._w, "simple", "none")
    mini.attributes("-topmost", True)
    mini.configure(bg=PANEL_HI)
    mini_inner = tk.Frame(mini, bg=BG)
    mini_inner.pack(padx=1, pady=1)  # borde fino

    rec_lbl = tk.Label(mini_inner, text="🔴", bg=BG, fg=RED, font=(FONT, 13))
    rec_lbl.pack(side=tk.LEFT, padx=(8, 2))
    mini_time = tk.Label(mini_inner, text="0:00", bg=BG, fg=MUTED, font=(MONO, 11))
    mini_time.pack(side=tk.LEFT, padx=2)
    vu = tk.Canvas(mini_inner, width=50, height=10, bg=PANEL, highlightthickness=0)
    vu.pack(side=tk.LEFT, padx=4)
    mini_text = tk.Label(mini_inner, text="", bg=BG, fg=FG, font=(FONT, 11),
                         width=42, anchor="e")
    mini_text.pack(side=tk.LEFT, padx=4)

    def mkmini(text, cmd, fg=FG):
        return tk.Button(
            mini_inner, text=text, command=cmd,
            bg=BG, fg=fg, activebackground=PANEL_HI, activeforeground=FG,
            bd=0, relief=tk.FLAT, font=(FONT, 12), padx=6, pady=2,
            cursor="hand2", takefocus=0,
        )

    mkmini("⏹", lambda: threading.Thread(target=app.on_dictate_hotkey,
                                          daemon=True).start(), fg=GREEN).pack(side=tk.LEFT)
    mkmini("✕", lambda: threading.Thread(target=app.cancel,
                                          daemon=True).start(), fg=RED).pack(side=tk.LEFT, padx=(0, 4))

    # En todos los Spaces: mostrar el overlay no arrastra al usuario al
    # escritorio donde este la ventana principal.
    pin_overlay(mini)

    overlay_mode = [None]  # None | "warming" | "recording" | "finishing"

    def show_overlay(mode: str | None) -> None:
        prev = overlay_mode[0]
        overlay_mode[0] = mode
        if mode is None:
            mini.withdraw()
            live.set("")
            return
        if mode == "recording":
            rec_lbl.configure(text="🔴", fg=RED)
            mini_text.configure(text="¡hablá ahora!")
        elif mode == "warming":
            rec_lbl.configure(text="⏳", fg=YELLOW)
            mini_text.configure(text="esperá el beep…")
        else:  # finishing
            rec_lbl.configure(text="⏳", fg=YELLOW)
            mini_text.configure(text="transcribiendo…")
        if prev is not None:
            return  # ya esta visible: solo cambiaron los textos.
        # Posicionar UNA sola vez por sesion, al aparecer: reposicionar en
        # cada cambio de estado movia la ventanita (el 🔴 mide distinto que
        # el ⏳ y el recalculo la hundia bajo el borde de la pantalla).
        mini.update_idletasks()
        left, top, right, bottom = _work_area()
        mini.geometry(f"+{right - mini.winfo_reqwidth() - 12}"
                      f"+{bottom - mini.winfo_reqheight() - 12}")
        mini.deiconify()
        mini.attributes("-topmost", True)

    def apply_state(state: str) -> None:
        if state == "recording":
            status.set(f"🔴  HABLÁ AHORA — {specs['dictate'].upper()} para pegar")
            status_lbl.configure(fg=GREEN)
        elif state == "warming":
            status.set("⏳  Preparando el micrófono — todavía no hables…")
            status_lbl.configure(fg=YELLOW)
        elif state == "finishing":
            status.set("⏳  Transcribiendo…")
            status_lbl.configure(fg=YELLOW)
        else:
            status.set(IDLE_MSG)
            status_lbl.configure(fg=MUTED)

    def redraw_overlay() -> None:
        if overlay_mode[0] != "recording":
            return
        mini_time.configure(text=_fmt_time(app.elapsed()))
        vu.delete("all")
        vu.create_rectangle(0, 0, int(50 * min(1.0, app.level() * 1.5)), 10,
                            fill=GREEN, width=0)

    # cerrar la ventana principal la esconde (la app sigue viva en los hotkeys);
    # clic en el icono del Dock la vuelve a mostrar
    root.protocol("WM_DELETE_WINDOW", root.withdraw)
    try:
        root.createcommand("::tk::mac::ReopenApplication",
                           lambda: (root.deiconify(), root.lift()))
    except tk.TclError:
        pass

    def poll() -> None:
        try:
            while True:
                kind, payload = ui_q.get_nowait()
                if kind == "log":
                    logbox.configure(state=tk.NORMAL)
                    logbox.insert(tk.END, payload + "\n")
                    logbox.see(tk.END)
                    logbox.configure(state=tk.DISABLED)
                elif kind == "overlay":
                    show_overlay(payload)
                    apply_state(payload or "idle")
                elif kind == "status":
                    status.set(payload)
                    status_lbl.configure(fg=YELLOW)
                elif kind == "partial":
                    live.set(payload)
                    tail = payload[-60:]
                    mini_text.configure(text=("…" + tail[-57:]) if len(payload) > 60 else tail)
                elif kind == "show":  # otra instancia pidio que nos mostremos
                    root.deiconify()
                    root.lift()
                    root.attributes("-topmost", True)
                    root.after(1500, lambda: root.attributes("-topmost", False))
                    root.focus_force()
                elif kind == "quit":
                    root.destroy()
                    return
        except queue.Empty:
            pass
        redraw_overlay()
        root.after(100, poll)

    poll()
    try:
        root.mainloop()
    finally:
        hotkeys.stop()
        try:
            app.cancel()
        except Exception:
            pass
        # Si salimos en medio de un dictado, devolver la musica (bloqueante
        # a proposito: un thread daemon moriria antes de terminar).
        try:
            app.ducker.resume_paused()
        except Exception:
            pass
        log.info("Saliendo")


# --- main -----------------------------------------------------------------------

def main() -> int:
    if not acquire_single_instance():
        print("DictateText ya esta corriendo.")
        return 0

    load_dotenv(APP_DIR / ".env")
    ui_q: queue.Queue = queue.Queue()
    # Otra instancia (doble clic en la app cuando ya corre) manda SIGUSR1
    # para que mostremos la ventana. El handler corre en el main thread la
    # proxima vez que Tk le da aire a Python (el poll de 100ms alcanza).
    signal.signal(signal.SIGUSR1, lambda *_: ui_q.put(("show", None)))

    handlers: list[logging.Handler] = [
        logging.FileHandler(APP_DIR / "dictate_text.log", encoding="utf-8"),
        QueueLogHandler(ui_q),
        logging.StreamHandler(),
    ]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )

    key = os.environ.get("SPEECH_KEY")
    endpoint = os.environ.get("SPEECH_ENDPOINT")
    if not key or not endpoint:
        subprocess.run(["osascript", "-e",
                        'display alert "DictateText" message '
                        '"Faltan SPEECH_KEY / SPEECH_ENDPOINT en .env"'])
        return 1
    languages = [x.strip() for x in
                 os.environ.get("LANGUAGES", "es-MX,en-US").split(",") if x.strip()]
    mic_name = os.environ.get("MIC_DEVICE", "").strip()
    max_seconds = int(os.environ.get("MAX_SECONDS", "300"))
    restore_clip = os.environ.get("RESTORE_CLIPBOARD", "1") == "1"
    restore_delay = int(os.environ.get("RESTORE_DELAY_MS", "300")) / 1000
    specs = {
        "dictate": os.environ.get("DICTATE_HOTKEY", "ctrl+alt+d"),
        "cancel": os.environ.get("CANCEL_HOTKEY", "ctrl+alt+x"),
        "quit": os.environ.get("QUIT_HOTKEY", "ctrl+alt+shift+d"),
    }

    if not ensure_accessibility():
        log.warning("Sin permiso de Accesibilidad: los hotkeys y el pegado no "
                    "van a andar hasta que lo habilites (Ajustes → Privacidad "
                    "y seguridad → Accesibilidad) y reinicies la app")
        ui_q.put(("status", "ATENCION: falta el permiso de Accesibilidad"))

    stt = AzureSpeechSTT(
        key, endpoint, languages,
        phrases_path=APP_DIR / "phrases.txt",
        lid_mode=os.environ.get("LID_MODE", "AtStart"),
        on_partial=lambda text: ui_q.put(("partial", text)),
        on_error=lambda msg: ui_q.put(("status", msg)),
    )
    mic = {"name": mic_name}  # compartido con la GUI (dropdown de microfono)
    ducker = MediaDucker(enabled=os.environ.get("DUCK_MEDIA", "1") == "1")
    app = App(
        stt,
        ducker,
        get_device=lambda: mic["name"],
        max_seconds=max_seconds,
        restore_clipboard=restore_clip,
        restore_delay=restore_delay,
        notify=lambda msg: ui_q.put(("status", msg)),
        set_overlay=lambda mode: ui_q.put(("overlay", mode)),
    )

    threading.Thread(target=app._refresh_mic_cache, daemon=True,
                     name="mic-cache").start()

    hotkeys = HotkeyThread({
        HK_DICTATE: (specs["dictate"], app.on_dictate_hotkey),
        HK_CANCEL: (specs["cancel"], app.cancel),
        HK_QUIT: (specs["quit"], lambda: ui_q.put(("quit", None))),
    })
    hotkeys.start()
    hotkeys.ready.wait(timeout=5)
    for err in hotkeys.errors:
        log.error(err)
        ui_q.put(("status", "ATENCION: " + err))

    log.info("Listo. %s dictar/pegar | %s cancelar | %s salir | idiomas=%s",
             specs["dictate"], specs["cancel"], specs["quit"], ",".join(languages))
    run_gui(app, ui_q, hotkeys, specs, languages, mic)
    return 0


if __name__ == "__main__":
    sys.exit(main())
