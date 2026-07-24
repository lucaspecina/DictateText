"""Pausa y reanuda lo que este sonando (Spotify, YouTube, etc.).

Usa las Global System Media Transport Controls de Windows: la misma API que
alimenta el overlay de volumen con la caratula. A diferencia de simular la
tecla media play/pause (toggle ciego que puede ARRANCAR musica que no
sonaba), aca se consulta el estado real de cada sesion: se pausan solo las
que estaban reproduciendo y se reanudan solo esas — y solo si siguen
pausadas (si el usuario ya las reanudo a mano, no se tocan).

Solo Windows. Si los paquetes winrt no estan, la clase queda desactivada.
"""

import asyncio
import logging
import threading

log = logging.getLogger(__name__)

try:
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as _SessionManager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as _Status,
    )
    _AVAILABLE = True
except ImportError:  # pragma: no cover
    _AVAILABLE = False


class MediaDucker:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled and _AVAILABLE
        if enabled and not _AVAILABLE:
            log.warning("Paquetes winrt no instalados: no se pausara la musica")
        self._paused_ids: set[str] = set()
        self._lock = threading.Lock()  # serializa pausa/reanudacion

    # -- API publica ------------------------------------------------------------

    def pause_playing(self) -> None:
        """Bloqueante (~100-400 ms). Pausa lo que suena y lo recuerda.
        Llamar desde un worker thread, nunca desde el callback de audio."""
        if not self.enabled:
            return
        with self._lock:
            try:
                asyncio.run(self._pause())
            except Exception:
                log.exception("No pude pausar la musica")

    def resume_paused(self) -> None:
        """Bloqueante. Reanuda solo lo que pausamos nosotros."""
        if not self.enabled:
            return
        with self._lock:
            if not self._paused_ids:
                return
            try:
                asyncio.run(self._resume())
            except Exception:
                log.exception("No pude reanudar la musica")

    def resume_paused_async(self) -> None:
        """Version no bloqueante, segura para llamar desde callbacks."""
        if not self.enabled or not self._paused_ids:
            return
        threading.Thread(target=self.resume_paused, daemon=True).start()

    # -- implementacion -----------------------------------------------------------

    async def _pause(self) -> None:
        mgr = await _SessionManager.request_async()
        for session in mgr.get_sessions():
            try:
                if session.get_playback_info().playback_status == _Status.PLAYING:
                    if await session.try_pause_async():
                        self._paused_ids.add(session.source_app_user_model_id)
                        log.info("Musica pausada: %s", session.source_app_user_model_id)
            except Exception:
                continue

    async def _resume(self) -> None:
        mgr = await _SessionManager.request_async()
        for session in mgr.get_sessions():
            app_id = session.source_app_user_model_id
            if app_id not in self._paused_ids:
                continue
            try:
                if session.get_playback_info().playback_status == _Status.PAUSED:
                    await session.try_play_async()
                    log.info("Musica reanudada: %s", app_id)
            except Exception:
                log.debug("No pude reanudar %s", app_id, exc_info=True)
        self._paused_ids.clear()
