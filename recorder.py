"""Captura de microfono con sounddevice (PortAudio). Portable (Windows/Mac).

Entrega chunks PCM 16 kHz 16-bit mono (el formato que espera stt.py) y expone
`level` (0..1, pico del ultimo chunk) para el medidor visual del overlay.
Si el dispositivo no acepta 16 kHz, se abre a 48 kHz y se decima x3 promediando.
"""

import array
import logging
import threading
import time

import sounddevice as sd

log = logging.getLogger(__name__)

TARGET_RATE = 16000
FALLBACK_RATE = 48000
CHUNK_MS = 100


def list_input_devices() -> list[tuple[int, str]]:
    """(indice, nombre) de cada dispositivo con canales de entrada."""
    seen = set()
    out = []
    for idx, dev in enumerate(sd.query_devices()):
        if dev.get("max_input_channels", 0) > 0 and dev["name"] not in seen:
            seen.add(dev["name"])
            out.append((idx, dev["name"]))
    return out


# Un mismo microfono aparece una vez por hostapi; no todos abren igual de
# bien. MME resamplea y tolera casi todo; DirectSound (deprecado) falla
# seguido con Bluetooth (AirPods, etc.).
_HOSTAPI_PREFERENCE = ("MME", "WASAPI", "DirectSound", "WDM-KS")


def _api_rank(dev: dict, hostapis) -> int:
    api = hostapis[dev["hostapi"]]["name"].lower()
    for i, pref in enumerate(_HOSTAPI_PREFERENCE):
        if pref.lower() in api:
            return i
    return len(_HOSTAPI_PREFERENCE)


def resolve_candidates(name: str | None) -> list[int | None]:
    """Nombre (o fragmento) -> TODOS los indices que matchean, ordenados por
    hostapi de mas a menos confiable, para ir probando hasta que uno abra.
    [None] = default del sistema."""
    if not name:
        return [None]
    name_l = name.strip().lower()
    hostapis = sd.query_hostapis()
    exact, partial = [], []
    for idx, dev in enumerate(sd.query_devices()):
        if dev.get("max_input_channels", 0) <= 0:
            continue
        d = dev["name"].lower()
        if d == name_l:
            exact.append((idx, dev))
        # El hostapi MME trunca los nombres a 31 chars, asi que el nombre
        # completo de Windows puede CONTENER al de PortAudio, o al reves.
        elif name_l in d or (len(d) >= 10 and d in name_l):
            partial.append((idx, dev))

    # El hostapi manda sobre la exactitud del nombre: el match "exacto" suele
    # ser WASAPI/DirectSound (nombre completo) y el de MME queda truncado,
    # pero MME es el que abre bien casi siempre.
    matches = sorted([(it, 0) for it in exact] + [(it, 1) for it in partial],
                     key=lambda p: (_api_rank(p[0][1], hostapis), p[1]))
    out = [idx for (idx, _dev), _exactness in matches]
    if not out:
        log.warning("Microfono %r no encontrado; uso el default", name)
        return [None]
    return out


def resolve_device(name: str | None) -> int | None:
    """El mejor candidato solo (para usos simples como test_stt)."""
    return resolve_candidates(name)[0]


def device_label(idx: int | None) -> str:
    if idx is None:
        return "(default del sistema)"
    try:
        dev = sd.query_devices(idx)
        api = sd.query_hostapis(dev["hostapi"])["name"]
        return f"{dev['name']} [{api}]"
    except Exception:
        return f"dispositivo {idx}"


def refresh_devices() -> None:
    """Refresca el snapshot de dispositivos de PortAudio: sin esto, un headset
    enchufado despues de arrancar la app no aparece. Solo llamar sin streams
    abiertos."""
    try:
        sd._terminate()
        sd._initialize()
    except Exception:
        log.debug("No pude refrescar la lista de dispositivos", exc_info=True)


def _decimate3(pcm: bytes) -> bytes:
    """48 kHz -> 16 kHz promediando cada 3 muestras (suficiente para voz)."""
    a = array.array("h", pcm)
    n = len(a) - len(a) % 3
    return array.array(
        "h", ((a[i] + a[i + 1] + a[i + 2]) // 3 for i in range(0, n, 3))
    ).tobytes()


class MicRecorder:
    """Una instancia por sesion de dictado: start() -> chunks -> stop().

    `device` puede ser un indice, None (default) o una LISTA de candidatos
    (de resolve_candidates): se prueba cada uno a 16 y 48 kHz hasta que
    alguno abra."""

    def __init__(self, device=None, on_chunk=lambda pcm: None):
        self.candidates = device if isinstance(device, list) else [device]
        self.device: int | None = None  # el que realmente abrio
        self.on_chunk = on_chunk
        self._kick = None  # salida manos-libres abierta para activar BT
        self._last_exc: Exception | None = None
        self.level = 0.0
        self.peak = 0.0  # maximo de toda la sesion (diagnostico de mic mudo)
        self.bytes_total = 0
        # Se setea cuando el mic entrega el PRIMER chunk: recien ahi esta
        # realmente escuchando (un Bluetooth puede tardar ~1s en activarse).
        self.first_chunk = threading.Event()
        self._stream = None
        self._decimate = False

    def start(self) -> None:
        # Los AirPods (y otros Bluetooth) no entregan microfono hasta que algo
        # reproduce por su salida manos-libres: abrirla con silencio fuerza el
        # cambio de perfil. Proactivo (el kick se auto-descarta si el mic no
        # es manos-libres): probar capturas muertas primero costaria ~7s.
        if self._open_hfp_kick():
            time.sleep(0.3)  # que el headset termine de cambiar de perfil
        if self._try_candidates():
            return
        raise self._last_exc or RuntimeError("Sin candidatos de microfono")

    def _try_candidates(self) -> bool:
        for dev in self.candidates:
            for rate, decimate in ((TARGET_RATE, False), (FALLBACK_RATE, True)):
                try:
                    stream = self._open(rate, dev)
                    stream.start()
                except Exception as e:
                    self._last_exc = e
                    log.info("No abrio %s @ %d Hz: %s",
                             device_label(dev), rate, e)
                    continue
                # Algunos endpoints Bluetooth (WASAPI sobre todo) abren sin
                # error pero no entregan NI UN chunk: verificar que fluya
                # audio de verdad antes de darlo por bueno. Un mic vivo manda
                # chunks aunque haya silencio (llegan ceros igual).
                self._decimate = decimate
                if self.first_chunk.wait(1.2):
                    self._stream = stream
                    self.device = dev
                    log.info("Mic abierto: %s @ %d Hz", device_label(dev), rate)
                    return True
                log.info("%s @ %d Hz abrio pero no entrega audio; sigo",
                         device_label(dev), rate)
                self._last_exc = RuntimeError(
                    f"{device_label(dev)} abrio pero no entrega audio "
                    "(¿Bluetooth sin activar el perfil manos-libres?)")
                try:
                    stream.abort()
                    stream.close()
                except Exception:
                    pass
        return False

    def _open_hfp_kick(self) -> bool:
        """Abre (y deja abierta durante la sesion) la salida manos-libres del
        mismo aparato que el microfono buscado, con un toque de silencio."""
        names = []
        for dev in self.candidates:
            if dev is not None:
                try:
                    names.append(sd.query_devices(dev)["name"])
                except Exception:
                    continue
        name = max(names, key=len).lower() if names else ""
        if "hands-free" not in name:
            return False
        hostapis = sd.query_hostapis()
        outs = []
        for idx, dev in enumerate(sd.query_devices()):
            if dev.get("max_output_channels", 0) <= 0:
                continue
            d = dev["name"].lower()
            if "hands-free" in d and (d in name or name in d):
                outs.append((idx, dev))
        outs.sort(key=lambda it: _api_rank(it[1], hostapis))
        for idx, _dev in outs:
            try:
                kick = sd.RawOutputStream(samplerate=16000, channels=1,
                                          dtype="int16", device=idx)
                kick.start()
                kick.write(b"\x00" * 6400)  # 200 ms de silencio
            except Exception:
                log.debug("Kick fallo en %s", device_label(idx), exc_info=True)
                continue
            self._kick = kick
            log.info("Activando perfil manos-libres via %s", device_label(idx))
            return True
        return False

    def _open(self, rate: int, device: int | None) -> sd.RawInputStream:
        return sd.RawInputStream(
            samplerate=rate,
            blocksize=rate * CHUNK_MS // 1000,  # multiplo de 3 en 48k
            device=device,
            channels=1,
            dtype="int16",
            callback=self._callback,
        )

    def _callback(self, indata, frames, time_info, status) -> None:
        if status:
            log.debug("Mic status: %s", status)
        pcm = bytes(indata)
        if self._decimate:
            pcm = _decimate3(pcm)
        samples = array.array("h", pcm)
        self.level = (max(abs(s) for s in samples) / 32768.0) if samples else 0.0
        self.peak = max(self.peak, self.level)
        self.bytes_total += len(pcm)
        self.first_chunk.set()
        self.on_chunk(pcm)

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        kick, self._kick = self._kick, None
        self.level = 0.0
        for s in (stream, kick):
            if s is not None:
                try:
                    s.abort()
                    s.close()
                except Exception:
                    log.debug("Error cerrando el mic", exc_info=True)
