"""Valida credenciales de Azure y microfono, sin la capa de hotkeys.

    python test_stt.py              # graba 5 segundos del microfono y transcribe
    python test_stt.py --wav f.wav  # transcribe un WAV 16 kHz 16-bit mono (sin mic)

Si esto anda y main.py no, el problema esta en la capa de hotkeys/pegado.
"""

import logging
import os
import sys
import time
import wave
from pathlib import Path

from dotenv import load_dotenv

from stt import AzureSpeechSTT

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

APP_DIR = Path(__file__).resolve().parent
SECONDS = 5


def make_stt() -> AzureSpeechSTT:
    load_dotenv(APP_DIR / ".env")
    key, endpoint = os.environ.get("SPEECH_KEY"), os.environ.get("SPEECH_ENDPOINT")
    if not key or not endpoint:
        sys.exit("Faltan SPEECH_KEY / SPEECH_ENDPOINT en .env")
    languages = [x.strip() for x in
                 os.environ.get("LANGUAGES", "es-AR,en-US").split(",") if x.strip()]
    return AzureSpeechSTT(key, endpoint, languages,
                          on_partial=lambda t: print(f"  … {t}", flush=True))


def from_wav(path: str) -> str:
    with wave.open(path, "rb") as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (16000, 1, 2):
            print(f"AVISO: {path} no es 16 kHz/mono/16-bit "
                  f"({w.getframerate()} Hz, {w.getnchannels()} ch); "
                  "el resultado puede ser malo")
        pcm = w.readframes(w.getnframes())
    stt = make_stt()
    stt.start()
    for i in range(0, len(pcm), 3200):  # chunks de 100 ms, como el mic
        stt.feed(pcm[i:i + 3200])
    return stt.stop()


def from_mic() -> str:
    from recorder import MicRecorder, resolve_device

    stt = make_stt()
    stt.start()
    rec = MicRecorder(device=resolve_device(os.environ.get("MIC_DEVICE", "")),
                      on_chunk=stt.feed)
    rec.start()
    print(f"Grabando {SECONDS} segundos… habla ahora.")
    time.sleep(SECONDS)
    rec.stop()
    return stt.stop()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--wav":
        text = from_wav(sys.argv[2])
    else:
        text = from_mic()
    print(f"\nTranscripcion final: {text!r}")
    sys.exit(0 if text else 1)
