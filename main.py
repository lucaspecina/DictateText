"""DictateText: dicta con la voz y pega el texto donde estaba el cursor.

Atajos globales (configurables en .env):
    Ctrl+Alt+D        empezar a dictar / terminar y pegar (toggle)
    Ctrl+Alt+X        cancelar el dictado en curso (descarta)
    Ctrl+Alt+Shift+D  salir

Flujo: hotkey -> guarda la ventana activa -> graba el microfono mientras
Azure transcribe en streaming -> hotkey de nuevo -> vuelve a la ventana
original (si el usuario se fue a otra), pone el texto en el clipboard y
simula Ctrl+V. El clipboard anterior se restaura despues de pegar.

Por que el pegado cae exactamente donde estaba el cursor:
  - RegisterHotKey no roba el foco, asi que si no te moviste, el caret
    nunca se entero de nada.
  - Si te moviste, SetForegroundWindow reactiva la ventana original
    (permiso concedido por venir de un hotkey) y Windows le devuelve el
    foco al mismo control, que conserva caret y seleccion.

Capa 100% Windows (hotkeys, clipboard, SendInput, foreground, tkinter).
El STT (stt.py) y el grabador (recorder.py) son portables; para Mac se
reescribe solo esto.
"""

import ctypes
import logging
import os
import queue
import sys
import threading
import time
import tkinter as tk
import wave
from ctypes import wintypes
from pathlib import Path
from tkinter import scrolledtext

import win32clipboard
import win32con
from dotenv import load_dotenv, set_key

from recorder import MicRecorder, list_input_devices, refresh_devices, resolve_device
from stt import AzureSpeechSTT

APP_DIR = Path(__file__).resolve().parent

log = logging.getLogger("dictate_text")

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# --- Constantes Win32 -------------------------------------------------------

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN = 0x1, 0x2, 0x4, 0x8
# Sin esto, mantener el hotkey apretado medio segundo dispara WM_HOTKEY
# repetidos (key repeat) y el toggle arranca y corta la grabacion al instante.
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
HK_DICTATE, HK_CANCEL, HK_QUIT = 1, 2, 3

VK_CONTROL, VK_MENU, VK_SHIFT, VK_LWIN, VK_RWIN = 0x11, 0x12, 0x10, 0x5B, 0x5C
VK_V = 0x56
KEYEVENTF_KEYUP = 0x0002
INPUT_KEYBOARD = 1
SW_RESTORE = 9
GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000

MOD_NAMES = {"ctrl": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT, "win": MOD_WIN}


# --- Instancia unica ----------------------------------------------------------

_MUTEX_NAME = "DictateText_singleton"
_SHOW_EVENT_NAME = "DictateText_show"
_ERROR_ALREADY_EXISTS = 183
_WAIT_INFINITE = 0xFFFFFFFF


def acquire_single_instance():
    """Handle del mutex si somos la primera instancia; None si ya hay otra
    (a la que ademas le pedimos que muestre su ventana)."""
    handle = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    if kernel32.GetLastError() == _ERROR_ALREADY_EXISTS:
        event = kernel32.CreateEventW(None, False, False, _SHOW_EVENT_NAME)
        if event:
            kernel32.SetEvent(event)
            kernel32.CloseHandle(event)
        return None
    return handle


def watch_show_requests(ui_q: "queue.Queue") -> None:
    event = kernel32.CreateEventW(None, False, False, _SHOW_EVENT_NAME)
    while True:
        if kernel32.WaitForSingleObject(event, _WAIT_INFINITE) != 0:
            return
        ui_q.put(("show", None))


# --- SendInput (teclas sinteticas) ------------------------------------------
# OJO: la union del INPUT debe incluir MOUSEINPUT (el miembro mas grande);
# si solo se declara KEYBDINPUT el sizeof queda corto y SendInput rechaza todo.

ULONG_PTR = ctypes.POINTER(wintypes.ULONG)


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]

    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


def _send_keys(events: list[tuple[int, bool]]) -> None:
    """events: lista de (vk, is_keyup)."""
    inputs = (INPUT * len(events))()
    for i, (vk, up) in enumerate(events):
        inputs[i].type = INPUT_KEYBOARD
        inputs[i].ki.wVk = vk
        # Algunas apps ignoran input sintetico sin scan code.
        inputs[i].ki.wScan = user32.MapVirtualKeyW(vk, 0)
        inputs[i].ki.dwFlags = KEYEVENTF_KEYUP if up else 0
    sent = user32.SendInput(len(events), inputs, ctypes.sizeof(INPUT))
    if sent != len(events):
        log.warning(
            "SendInput inyecto %d/%d eventos (err=%d)",
            sent, len(events), kernel32.GetLastError(),
        )


def _wait_modifiers_released(timeout: float = 0.5) -> None:
    """Espera a que el usuario suelte fisicamente los modificadores del hotkey."""
    mods = (VK_CONTROL, VK_MENU, VK_SHIFT, VK_LWIN, VK_RWIN)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in mods):
            return
        time.sleep(0.01)


def send_ctrl_v() -> None:
    # Si Alt/Shift del hotkey siguen apretados, el Ctrl+V sintetico llega
    # como Ctrl+Alt+V y no pega. Esperar la soltada fisica y ademas inyectar
    # keyups por las dudas.
    _wait_modifiers_released()
    _send_keys([(VK_MENU, True), (VK_SHIFT, True), (VK_LWIN, True), (VK_RWIN, True)])
    time.sleep(0.05)
    _send_keys([(VK_CONTROL, False), (VK_V, False), (VK_V, True), (VK_CONTROL, True)])


# --- Clipboard ---------------------------------------------------------------

def _clipboard_op(fn, retries: int = 10):
    """El clipboard es un recurso global; puede estar tomado por otra app."""
    for _ in range(retries):
        try:
            win32clipboard.OpenClipboard()
            try:
                return fn()
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            time.sleep(0.03)
    return None


def get_clipboard_text() -> str | None:
    def read():
        if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
        return None

    return _clipboard_op(read)


def set_clipboard_text(text: str) -> None:
    def write():
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
        return True

    _clipboard_op(write)


# --- Microfono default de Windows --------------------------------------------

def windows_default_mic() -> str | None:
    """Nombre del microfono default de Windows, priorizando el rol de
    COMUNICACIONES (el que usan Teams y las llamadas: el headset si hay uno).
    PortAudio solo conoce el default general, que suele ser el mic de la
    notebook aunque el usuario hable por el auricular."""
    try:
        import comtypes
        from pycaw.utils import AudioUtilities

        try:
            comtypes.CoInitialize()  # los handlers de hotkey son threads nuevos
        except Exception:
            pass
        enum = AudioUtilities.GetDeviceEnumerator()
        for role in (2, 0):  # eCommunications, eConsole
            try:
                dev = enum.GetDefaultAudioEndpoint(1, role)  # 1 = eCapture
                name = AudioUtilities.CreateDevice(dev).FriendlyName
                if name:
                    return name
            except Exception:
                continue
    except Exception:
        log.debug("No pude leer el mic default de Windows", exc_info=True)
    return None


# --- Ventana destino (a donde volver a pegar) --------------------------------

def window_title(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(128)
    user32.GetWindowTextW(hwnd, buf, 128)
    return buf.value or "(sin titulo)"


def _wait_foreground(hwnd, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if user32.GetForegroundWindow() == hwnd:
            return True
        time.sleep(0.02)
    return False


def reactivate_window(hwnd, timeout: float = 1.5) -> bool:
    """Trae al frente la ventana donde empezo el dictado. Windows le devuelve
    el foco al mismo control, que conserva caret y seleccion aunque la
    ventana haya estado atras."""
    if not hwnd or not user32.IsWindow(hwnd):
        return False
    if user32.GetForegroundWindow() == hwnd:
        return True
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    # Permitido porque el usuario acaba de apretar nuestro hotkey registrado
    # (eso nos da permiso de foreground). Si igual falla, el toque de Alt
    # sintetico es el fallback clasico para destrabar SetForegroundWindow.
    user32.SetForegroundWindow(hwnd)
    if _wait_foreground(hwnd, timeout / 2):
        return True
    _send_keys([(VK_MENU, False), (VK_MENU, True)])
    user32.SetForegroundWindow(hwnd)
    return _wait_foreground(hwnd, timeout / 2)


# --- Logica de la app ---------------------------------------------------------

class App:
    """Maquina de estados: idle -> recording -> finishing -> idle."""

    def __init__(self, stt: AzureSpeechSTT, get_device, max_seconds: int,
                 restore_clipboard: bool, restore_delay: float,
                 notify=lambda msg: None, set_overlay=lambda mode: None):
        self.stt = stt
        self.get_device = get_device  # callable: nombre del mic elegido en la GUI
        self.max_seconds = max_seconds
        self.restore_clipboard = restore_clipboard
        self.restore_delay = restore_delay
        self.notify = notify
        self.set_overlay = set_overlay
        self.recorder: MicRecorder | None = None
        self.record_started = 0.0
        self.target_hwnd = None
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
            # Refuerzo de MOD_NOREPEAT: eventos a menos de 300ms solo pueden
            # ser key repeat o un rebote, nunca un toggle intencional.
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
        if self.recorder:
            self.recorder.stop()
            self.recorder = None
        self.stt.cancel()
        self.set_overlay(None)
        self.notify("Dictado cancelado")
        log.info("Dictado cancelado")
        with self._lock:
            self._state = "idle"

    def _start(self, sid: int) -> None:
        # PRIMERO la ventana destino, antes de tocar cualquier otra cosa.
        self.target_hwnd = user32.GetForegroundWindow()
        self.target_title = window_title(self.target_hwnd)
        self._audio = bytearray()

        def on_chunk(pcm: bytes) -> None:
            self._audio.extend(pcm)
            self.stt.feed(pcm)

        refresh_devices()  # que un headset recien enchufado aparezca
        mic_name = self.get_device()
        if not mic_name:
            mic_name = windows_default_mic() or ""
            if mic_name:
                log.info("Mic default de Windows (comunicaciones): %s", mic_name)
        try:
            self.stt.start()
            rec = MicRecorder(device=resolve_device(mic_name), on_chunk=on_chunk)
            rec.start()
        except Exception:
            log.exception("No pude arrancar el dictado (¿microfono?)")
            self.notify("ERROR al arrancar (¿microfono? ¿permisos?) — ver log")
            user32.MessageBeep(0x10)
            self.stt.cancel()
            with self._lock:
                self._state = "idle"
            return
        self.recorder = rec
        self.record_started = time.monotonic()
        log.info("Grabando — destino: %s", self.target_title)
        self.set_overlay("recording")
        user32.MessageBeep(0x40)
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
        if self.recorder:
            audio_secs = self.recorder.bytes_total / 32000  # 16 kHz * 2 bytes
            peak = self.recorder.peak
            self.recorder.stop()
            self.recorder = None
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
            user32.MessageBeep(0x30)
        else:
            log.warning("Azure no reconocio voz en %.1fs de audio (pico %.3f); "
                        "audio guardado en last_dictation.wav", audio_secs, peak)
            if peak < 0.15:
                log.warning("Nivel de mic bajo: probablemente esta escuchando el "
                            "microfono equivocado — elegi otro en el desplegable "
                            "de la ventana de DictateText")
            self.notify("No entendi nada — no pego (ver log)")
            user32.MessageBeep(0x30)
        with self._lock:
            self._state = "idle"

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
        if not reactivate_window(self.target_hwnd):
            # Nunca pegar a ciegas en cualquier lado. El texto queda en el
            # clipboard (por eso aca NO se restaura el anterior).
            log.warning("No pude reactivar %r; el texto queda en el clipboard",
                        self.target_title)
            self.notify(f"No pude volver a «{self.target_title}» — "
                        "el texto quedo en el portapapeles (Ctrl+V)")
            user32.MessageBeep(0x30)
            return
        send_ctrl_v()
        self.notify(f"Pegado en «{self.target_title}»")
        log.info("Pegado en %s", self.target_title)
        if self.restore_clipboard and old_clip is not None:
            # Darle tiempo a la app destino a leer el clipboard antes de pisarlo.
            time.sleep(self.restore_delay)
            set_clipboard_text(old_clip)


# --- Hotkeys globales (thread propio con message loop) -------------------------

class HotkeyThread(threading.Thread):
    """RegisterHotKey exige que el registro y el GetMessage esten en el
    mismo thread; el main thread queda para tkinter."""

    def __init__(self, bindings: dict[int, tuple[str, "callable"]]):
        super().__init__(daemon=True, name="hotkeys")
        self.bindings = bindings
        self.errors: list[str] = []
        self.ready = threading.Event()
        self._tid: int | None = None

    def run(self) -> None:
        self._tid = kernel32.GetCurrentThreadId()
        registered = []
        for hk_id, (spec, _cb) in self.bindings.items():
            try:
                mods, vk = parse_hotkey(spec)
            except ValueError as e:
                self.errors.append(str(e))
                continue
            if user32.RegisterHotKey(None, hk_id, mods | MOD_NOREPEAT, vk):
                registered.append(hk_id)
            else:
                self.errors.append(
                    f"No pude registrar {spec} (¿otra instancia ya corriendo?)"
                )
        self.ready.set()
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                if msg.message == WM_HOTKEY and msg.wParam in self.bindings:
                    cb = self.bindings[msg.wParam][1]
                    threading.Thread(target=cb, daemon=True).start()
        finally:
            for hk_id in registered:
                user32.UnregisterHotKey(None, hk_id)

    def stop(self) -> None:
        if self._tid:
            user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)


def parse_hotkey(spec: str) -> tuple[int, int]:
    """'ctrl+alt+d' -> (modificadores, virtual-key)."""
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    mods, vk = 0, None
    for p in parts:
        if p in MOD_NAMES:
            mods |= MOD_NAMES[p]
        elif len(p) == 1 and (p.isalpha() or p.isdigit()):
            vk = ord(p.upper())
        elif p.startswith("f") and p[1:].isdigit():
            vk = 0x70 + int(p[1:]) - 1  # VK_F1..
        else:
            raise ValueError(f"Tecla no soportada en hotkey: {p!r}")
    if vk is None:
        raise ValueError(f"Hotkey sin tecla principal: {spec!r}")
    return mods, vk


# --- Ventana -------------------------------------------------------------------

class QueueLogHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def emit(self, record: logging.LogRecord) -> None:
        self.q.put(("log", self.format(record)))


# Paleta oscura (Catppuccin Mocha-ish, igual que SpeakSelectedText)
BG = "#1e1e2e"
PANEL = "#313244"
PANEL_HI = "#45475a"
FG = "#cdd6f4"
MUTED = "#7f849c"
ACCENT = "#89b4fa"
GREEN = "#a6e3a1"
YELLOW = "#f9e2af"
RED = "#f38ba8"

IDLE_MSG = "En espera — cursor en un campo de texto y apreta el atajo"


def _fmt_time(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


def _work_area() -> tuple[int, int, int, int]:
    """Area de pantalla sin la barra de tareas (left, top, right, bottom)."""

    class RECT(ctypes.Structure):
        _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                    ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

    rect = RECT()
    user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)  # SPI_GETWORKAREA
    return rect.left, rect.top, rect.right, rect.bottom


def _make_noactivate(win: tk.Toplevel) -> None:
    """WS_EX_NOACTIVATE: los clicks en el overlay funcionan pero NUNCA roban
    el foco — si lo robaran, el Ctrl+V iria a parar al overlay y no al campo."""
    win.update_idletasks()
    try:
        hwnd = int(win.wm_frame(), 16)
    except (ValueError, tk.TclError):
        hwnd = user32.GetParent(win.winfo_id()) or win.winfo_id()
    get_long = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
    set_long = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
    style = get_long(hwnd, GWL_EXSTYLE)
    set_long(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE)


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
             font=("Segoe UI", 14, "bold")).pack(side=tk.LEFT)
    tk.Label(header, text=" · ".join(languages), bg=BG, fg=MUTED,
             font=("Segoe UI", 9)).pack(side=tk.RIGHT)

    # -- estado
    status = tk.StringVar(value=IDLE_MSG)
    status_lbl = tk.Label(root, textvariable=status, bg=BG, fg=MUTED,
                          font=("Segoe UI", 13, "bold"))
    status_lbl.pack(pady=(10, 2))

    # -- transcripcion en vivo
    live = tk.StringVar(value="")
    tk.Label(root, textvariable=live, bg=BG, fg=FG, font=("Segoe UI", 10),
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
             font=("Segoe UI", 9)).pack(side=tk.LEFT)
    mm = tk.OptionMenu(controls, mic_var, *mics, command=on_mic)
    mm.configure(bg=PANEL, fg=FG, activebackground=PANEL_HI, activeforeground=FG,
                 bd=0, relief=tk.FLAT, font=("Segoe UI", 9), highlightthickness=0,
                 cursor="hand2")
    mm["menu"].configure(bg=PANEL, fg=FG, activebackground=PANEL_HI,
                         activeforeground=FG, bd=0)
    mm.pack(side=tk.LEFT, padx=(6, 18))
    tk.Button(controls, text="✕ Salir", command=lambda: root.destroy(),
              bg=PANEL, fg=FG, activebackground=PANEL_HI, activeforeground=FG,
              bd=0, relief=tk.FLAT, font=("Segoe UI", 10), padx=12, pady=4,
              cursor="hand2").pack(side=tk.LEFT)

    # -- campo de prueba: como la GUI es una ventana mas, dictar con el cursor
    #    aca adentro prueba el circuito completo sin salir de la app
    tk.Label(root, text="Campo de prueba — clic adentro y dicta con el atajo:",
             bg=BG, fg=MUTED, font=("Segoe UI", 9)).pack(pady=(10, 2))
    testbox = tk.Text(root, height=3, font=("Segoe UI", 10), bg=PANEL, fg=FG,
                      insertbackground=FG, bd=0, relief=tk.FLAT, wrap=tk.WORD,
                      padx=8, pady=6)
    testbox.pack(fill=tk.X, padx=14)

    # -- atajos
    tk.Label(
        root,
        text=(f"{specs['dictate'].upper()} dictar / pegar   ·   "
              f"{specs['cancel'].upper()} cancelar   ·   "
              f"{specs['quit'].upper()} salir"),
        bg=BG, fg=MUTED, font=("Segoe UI", 9),
    ).pack(pady=(8, 2))

    # -- log
    logbox = scrolledtext.ScrolledText(
        root, height=8, state=tk.DISABLED, font=("Consolas", 9),
        bg="#181825", fg=MUTED, insertbackground=FG, bd=0, relief=tk.FLAT,
    )
    logbox.pack(fill=tk.BOTH, expand=True, padx=14, pady=(4, 12))

    # -- overlay de grabacion (abajo a la derecha, nunca toma foco) -----------
    mini = tk.Toplevel(root)
    mini.withdraw()
    mini.overrideredirect(True)
    mini.attributes("-topmost", True)
    mini.configure(bg=PANEL_HI)
    mini_inner = tk.Frame(mini, bg=BG)
    mini_inner.pack(padx=1, pady=1)  # borde fino

    rec_lbl = tk.Label(mini_inner, text="🔴", bg=BG, fg=RED, font=("Segoe UI", 12))
    rec_lbl.pack(side=tk.LEFT, padx=(8, 2))
    mini_time = tk.Label(mini_inner, text="0:00", bg=BG, fg=MUTED, font=("Consolas", 10))
    mini_time.pack(side=tk.LEFT, padx=2)
    vu = tk.Canvas(mini_inner, width=50, height=10, bg=PANEL, highlightthickness=0)
    vu.pack(side=tk.LEFT, padx=4)
    mini_text = tk.Label(mini_inner, text="", bg=BG, fg=FG, font=("Segoe UI", 9),
                         width=42, anchor="e")
    mini_text.pack(side=tk.LEFT, padx=4)

    def mkmini(text, cmd, fg=FG):
        return tk.Button(
            mini_inner, text=text, command=cmd,
            bg=BG, fg=fg, activebackground=PANEL_HI, activeforeground=FG,
            bd=0, relief=tk.FLAT, font=("Segoe UI", 11), padx=6, pady=2,
            cursor="hand2", takefocus=0,
        )

    mkmini("⏹", lambda: threading.Thread(target=app.on_dictate_hotkey,
                                          daemon=True).start(), fg=GREEN).pack(side=tk.LEFT)
    mkmini("✕", lambda: threading.Thread(target=app.cancel,
                                          daemon=True).start(), fg=RED).pack(side=tk.LEFT, padx=(0, 4))

    _make_noactivate(mini)

    overlay_mode = [None]  # None | "recording" | "finishing"

    def show_overlay(mode: str | None) -> None:
        overlay_mode[0] = mode
        if mode is None:
            mini.withdraw()
            live.set("")
            return
        rec_lbl.configure(text="🔴" if mode == "recording" else "⏳",
                          fg=RED if mode == "recording" else YELLOW)
        mini.update_idletasks()
        left, top, right, bottom = _work_area()
        mini.geometry(f"+{right - mini.winfo_reqwidth() - 12}"
                      f"+{bottom - mini.winfo_reqheight() - 12}")
        mini.deiconify()
        mini.attributes("-topmost", True)

    def apply_state(state: str) -> None:
        if state == "recording":
            status.set(f"🔴  GRABANDO — {specs['dictate'].upper()} para pegar")
            status_lbl.configure(fg=GREEN)
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

    # cerrar la ventana principal la esconde (la app sigue viva en los hotkeys)
    root.protocol("WM_DELETE_WINDOW", root.withdraw)

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
        log.info("Saliendo")


# --- main ----------------------------------------------------------------------

def main() -> int:
    mutex = acquire_single_instance()
    if mutex is None:
        return 0  # ya hay una instancia corriendo; le pedimos que se muestre

    load_dotenv(APP_DIR / ".env")
    ui_q: queue.Queue = queue.Queue()
    threading.Thread(target=watch_show_requests, args=(ui_q,), daemon=True,
                     name="show-watcher").start()

    handlers: list[logging.Handler] = [
        logging.FileHandler(APP_DIR / "dictate_text.log", encoding="utf-8"),
        QueueLogHandler(ui_q),
    ]
    if sys.stderr is not None:  # pythonw no tiene consola
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )

    key = os.environ.get("SPEECH_KEY")
    endpoint = os.environ.get("SPEECH_ENDPOINT")
    if not key or not endpoint:
        user32.MessageBoxW(None, "Faltan SPEECH_KEY / SPEECH_ENDPOINT en .env",
                           "DictateText", 0x10)
        return 1
    languages = [x.strip() for x in
                 os.environ.get("LANGUAGES", "es-AR,en-US").split(",") if x.strip()]
    mic_name = os.environ.get("MIC_DEVICE", "").strip()
    max_seconds = int(os.environ.get("MAX_SECONDS", "300"))
    restore_clip = os.environ.get("RESTORE_CLIPBOARD", "1") == "1"
    restore_delay = int(os.environ.get("RESTORE_DELAY_MS", "300")) / 1000
    specs = {
        "dictate": os.environ.get("DICTATE_HOTKEY", "ctrl+alt+d"),
        "cancel": os.environ.get("CANCEL_HOTKEY", "ctrl+alt+x"),
        "quit": os.environ.get("QUIT_HOTKEY", "ctrl+alt+shift+d"),
    }

    stt = AzureSpeechSTT(
        key, endpoint, languages,
        on_partial=lambda text: ui_q.put(("partial", text)),
        on_error=lambda msg: ui_q.put(("status", msg)),
    )
    mic = {"name": mic_name}  # compartido con la GUI (dropdown de microfono)
    app = App(
        stt,
        get_device=lambda: mic["name"],
        max_seconds=max_seconds,
        restore_clipboard=restore_clip,
        restore_delay=restore_delay,
        notify=lambda msg: ui_q.put(("status", msg)),
        set_overlay=lambda mode: ui_q.put(("overlay", mode)),
    )

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
