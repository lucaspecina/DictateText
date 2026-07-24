"""Captura de microfono con sounddevice (PortAudio). Portable (Windows/Mac).

Entrega chunks PCM 16 kHz 16-bit mono (el formato que espera stt.py) y expone
`level` (0..1, pico del ultimo chunk) para el medidor visual del overlay.
Si el dispositivo no acepta 16 kHz, se abre a 48 kHz y se decima x3 promediando.
"""

import array
import logging
import threading

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


def resolve_device(name: str | None) -> int | None:
    """Nombre (o fragmento) de .env -> indice; None = default del sistema."""
    if not name:
        return None
    name_l = name.strip().lower()
    devices = list_input_devices()
    for idx, dev_name in devices:
        if dev_name.lower() == name_l:
            return idx
    for idx, dev_name in devices:
        d = dev_name.lower()
        # El hostapi MME trunca los nombres a 31 chars, asi que el nombre
        # completo de Windows puede CONTENER al de PortAudio, o al reves.
        if name_l in d or (len(d) >= 10 and d in name_l):
            return idx
    log.warning("Microfono %r no encontrado; uso el default", name)
    return None


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
    """Una instancia por sesion de dictado: start() -> chunks -> stop()."""

    def __init__(self, device: int | None = None, on_chunk=lambda pcm: None):
        self.device = device
        self.on_chunk = on_chunk
        self.level = 0.0
        self.peak = 0.0  # maximo de toda la sesion (diagnostico de mic mudo)
        self.bytes_total = 0
        # Se setea cuando el mic entrega el PRIMER chunk: recien ahi esta
        # realmente escuchando (un Bluetooth puede tardar ~1s en activarse).
        self.first_chunk = threading.Event()
        self._stream = None
        self._decimate = False

    def start(self) -> None:
        try:
            self._stream = self._open(TARGET_RATE)
            self._decimate = False
        except Exception:
            log.info("El microfono no acepta 16 kHz; abro a 48 kHz y decimo")
            self._stream = self._open(FALLBACK_RATE)
            self._decimate = True
        self._stream.start()

    def _open(self, rate: int) -> sd.RawInputStream:
        return sd.RawInputStream(
            samplerate=rate,
            blocksize=rate * CHUNK_MS // 1000,  # multiplo de 3 en 48k
            device=self.device,
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
        self.level = 0.0
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                log.debug("Error cerrando el mic", exc_info=True)
