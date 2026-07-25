"""Pausa y reanuda la musica mientras se dicta (version macOS de media.py).

Dos vias, con la misma politica que en Windows (nada de toggles ciegos:
se consulta el estado real, se pausa solo lo que estaba sonando y se
reanuda solo eso, y solo si el usuario no lo reanudo a mano):

1. `media-control` (opcional, recomendado): habla con el sistema "Now
   Playing" de macOS — el mismo que muestra la caratula en el Centro de
   Control — asi que ve TAMBIEN a los navegadores (YouTube en Chrome, etc.),
   que AppleScript no alcanza. Instalar con:
       brew tap ungive/media-control && brew install media-control
   En Macs ARM con Homebrew de Intel el framework es x86_64: se detecta y
   se relanza solo bajo Rosetta (arch -x86_64).

2. AppleScript contra Spotify y Music: siempre disponible, cubre los
   reproductores clasicos si media-control no esta instalado.

La primera vez macOS puede pedir permiso de Automatizacion ("...quiere
controlar Spotify/Music"); aceptarlo una sola vez.
"""

import json
import logging
import os
import shutil
import subprocess
import threading

from AppKit import NSWorkspace

log = logging.getLogger(__name__)

# Apps scriptables con la misma interfaz de player (player state / pause / play)
_PLAYER_BUNDLES = {"Spotify": "com.spotify.client", "Music": "com.apple.Music"}
_PLAYERS = tuple(_PLAYER_BUNDLES)


def _is_running(bundle_id: str) -> bool:
    """Via NSWorkspace, sin AppleScript: referenciar por nombre una app NO
    instalada bloquea a osascript buscandola (dialogo "Where is ...?").
    (Truco heredado de SpeakSelectedText/media_mac.py.)"""
    apps = NSWorkspace.sharedWorkspace().runningApplications()
    return any(a.bundleIdentifier() == bundle_id for a in apps)


def _tell(app: str, cmd: str, timeout: float = 5.0) -> str | None:
    """'tell application X to <cmd>'; solo llamar con la app ya corriendo."""
    try:
        out = subprocess.run(
            ["osascript", "-e", f'tell application "{app}" to {cmd}'],
            capture_output=True, text=True, timeout=timeout,
        )
        if out.returncode != 0:
            log.debug("osascript fallo (%s -> %s): %s", app, cmd,
                      out.stderr.strip())
            return None
        return out.stdout.strip()
    except subprocess.TimeoutExpired:
        # Tipico: el dialogo de Automatizacion esta esperando respuesta.
        log.warning("osascript no respondio (%s -> %s); ¿falta aceptar el "
                    "permiso de Automatizacion?", app, cmd)
        return None
    except Exception:
        log.debug("osascript no corrio", exc_info=True)
        return None


def _player_state(app: str) -> str | None:
    """'playing' | 'paused' | 'stopped' | None si no corre o no responde."""
    if not _is_running(_PLAYER_BUNDLES[app]):
        return None
    return _tell(app, "player state as string")


class _MediaControl:
    """Wrapper de la CLI media-control (github.com/ungive/media-control).

    Ademas del probe, deja corriendo `media-control stream` de fondo y cachea
    el estado "now playing" en memoria: consultar el estado con `get` levanta
    un perl (bajo Rosetta) que tarda ~1 segundo, y ese segundo se sentia
    entero en el arranque de cada dictado. Con el cache, decidir si hay que
    pausar es instantaneo y solo el pause/play real cuesta una invocacion."""

    def __init__(self):
        self.cmd: list[str] | None = None
        self._state: dict = {}
        self._state_lock = threading.Lock()
        self._stream_proc: subprocess.Popen | None = None
        path = shutil.which("media-control") or (
            "/usr/local/bin/media-control"
            if os.path.exists("/usr/local/bin/media-control") else None)
        if not path:
            return
        # realpath: el script usa FindBin para ubicar ../Frameworks, y via el
        # symlink de /usr/local/bin no lo encuentra.
        path = os.path.realpath(path)
        # Rosetta PRIMERO: el framework del Homebrew de Intel es x86_64, y la
        # invocacion nativa puede fallar EN SILENCIO TOTAL (exit 0, stderr
        # limpio, stdout vacio) segun el entorno. Solo se acepta un candidato
        # que demuestre que anda: JSON no vacio en stdout.
        for candidate in (["arch", "-x86_64", path], [path]):
            try:
                out = subprocess.run(candidate + ["get"], capture_output=True,
                                     text=True, timeout=8)
                if out.returncode != 0:
                    continue
                if "framework" in (out.stderr or "").lower():
                    continue
                text = out.stdout.strip()
                if not text:
                    continue  # el modo roto tipico: exit 0 sin decir nada
                json.loads(text)  # tira si es un mensaje de error
                self.cmd = candidate
                log.info("media-control disponible (%s)",
                         "Rosetta" if candidate[0] == "arch" else "nativo")
                self._start_stream()
                return
            except Exception:
                continue
        log.info("media-control instalado pero no corre; solo Spotify/Music")

    def _start_stream(self) -> None:
        try:
            self._stream_proc = subprocess.Popen(
                self.cmd + ["stream"], stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True,
            )
        except Exception:
            log.debug("No pude lanzar media-control stream", exc_info=True)
            self._stream_proc = None
            return
        threading.Thread(target=self._read_stream, daemon=True,
                         name="media-stream").start()

    def _read_stream(self) -> None:
        """Lineas JSON: {"type":"data","diff":bool,"payload":{...}}.
        diff=false -> estado completo; diff=true -> solo lo que cambio."""
        proc = self._stream_proc
        try:
            for line in proc.stdout:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("type") != "data":
                    continue
                payload = msg.get("payload") or {}
                with self._state_lock:
                    if msg.get("diff"):
                        self._state.update(payload)
                    else:
                        self._state = dict(payload)
        except Exception:
            log.debug("media-control stream se corto", exc_info=True)
        finally:
            self._stream_proc = None  # now_playing() vuelve al `get` lento

    def now_playing(self) -> dict | None:
        """{'bundleIdentifier': ..., 'playing': bool, ...} o None."""
        if not self.cmd:
            return None
        # Camino rapido: el estado cacheado por el stream de fondo.
        if self._stream_proc is not None:
            with self._state_lock:
                return dict(self._state) if self._state else None
        # Fallback lento (~1s): el stream murio o no arranco.
        try:
            out = subprocess.run(self.cmd + ["get"], capture_output=True,
                                 text=True, timeout=5)
            if out.returncode != 0 or not out.stdout.strip():
                return None
            data = json.loads(out.stdout)
            return data if isinstance(data, dict) else None
        except Exception:
            log.debug("media-control get fallo", exc_info=True)
            return None

    def send(self, action: str) -> bool:
        if not self.cmd:
            return False
        try:
            out = subprocess.run(self.cmd + [action], capture_output=True,
                                 text=True, timeout=5)
            return out.returncode == 0
        except Exception:
            log.debug("media-control %s fallo", action, exc_info=True)
            return False


class MediaDucker:
    """Misma interfaz que media.MediaDucker (Windows)."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._mc = _MediaControl() if enabled else None
        self._mc_paused: str | None = None  # bundle id pausado via media-control
        self._paused: set[str] = set()      # apps pausadas via AppleScript
        self._lock = threading.Lock()       # serializa pausa/reanudacion

    # -- API publica ----------------------------------------------------------

    def pause_playing(self) -> None:
        """Bloqueante (~100-800 ms). Pausa lo que suena y lo recuerda."""
        if not self.enabled:
            return
        with self._lock:
            skip: set[str] = set()
            # 1) Lo que el sistema considere "Now Playing" (incluye navegadores)
            info = self._mc.now_playing() if self._mc else None
            if info and info.get("playing"):
                bundle = info.get("bundleIdentifier", "")
                if self._mc.send("pause"):
                    self._mc_paused = bundle
                    log.info("Musica pausada (now playing): %s", bundle)
                    skip = {app for app, b in _PLAYER_BUNDLES.items()
                            if b == bundle}
            # 2) Spotify/Music por AppleScript (si no los pausamos recien)
            for app in _PLAYERS:
                if app in skip:
                    continue
                if _player_state(app) == "playing":
                    if _tell(app, "pause") is not None:
                        self._paused.add(app)
                        log.info("Musica pausada: %s", app)

    def resume_paused(self) -> None:
        """Bloqueante. Reanuda solo lo que pausamos nosotros."""
        if not self.enabled:
            return
        with self._lock:
            if self._mc_paused is not None:
                info = self._mc.now_playing() if self._mc else None
                # Solo si el "now playing" sigue siendo la misma app y sigue
                # pausada (si el usuario ya la reanudo o cambio, no tocar).
                if (info and not info.get("playing")
                        and info.get("bundleIdentifier", "") == self._mc_paused):
                    if self._mc.send("play"):
                        log.info("Musica reanudada (now playing): %s",
                                 self._mc_paused)
                self._mc_paused = None
            for app in list(self._paused):
                # si el usuario ya la reanudo a mano, no tocar
                if _player_state(app) == "paused":
                    _tell(app, "play")
                    log.info("Musica reanudada: %s", app)
            self._paused.clear()

    def resume_paused_async(self) -> None:
        """Version no bloqueante, segura para llamar desde callbacks."""
        if not self.enabled or (self._mc_paused is None and not self._paused):
            return
        threading.Thread(target=self.resume_paused, daemon=True).start()
