"""Backend de speech-to-text: Azure Speech (via recurso Foundry).

El audio del microfono se empuja en chunks PCM a un PushAudioInputStream y
Azure reconoce en streaming MIENTRAS se habla: cuando el usuario corta, casi
todo ya esta transcripto y el resultado final llega en menos de un segundo
(no hay que esperar a procesar todo el audio junto).

Interfaz que debe cumplir todo backend (para poder cambiar de motor despues,
p. ej. gpt-4o-transcribe, igual que tts.py en SpeakSelectedText):
    start()          -> abre una sesion de reconocimiento
    feed(pcm_bytes)  -> audio PCM 16 kHz 16-bit mono
    stop() -> str    -> cierra la sesion y devuelve el texto final
    cancel()         -> descarta la sesion actual sin resultado
Los parciales van llegando por on_partial(texto_acumulado) para mostrar en vivo.
"""

import logging
import threading

import azure.cognitiveservices.speech as speechsdk

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000  # lo que espera Azure STT; recorder.py entrega esto


class AzureSpeechSTT:
    def __init__(self, key: str, endpoint: str, languages: list[str],
                 on_partial=lambda text: None, on_error=lambda msg: None):
        self.key = key
        self.endpoint = endpoint
        self.languages = languages
        self.on_partial = on_partial
        self.on_error = on_error
        self._recognizer = None
        self._stream = None
        self._pieces: list[str] = []
        self._interim = ""
        self._ended = threading.Event()

    # -- sesion ---------------------------------------------------------------

    def start(self) -> None:
        fmt = speechsdk.audio.AudioStreamFormat(
            samples_per_second=SAMPLE_RATE, bits_per_sample=16, channels=1
        )
        self._stream = speechsdk.audio.PushAudioInputStream(stream_format=fmt)
        audio_cfg = speechsdk.audio.AudioConfig(stream=self._stream)
        cfg = speechsdk.SpeechConfig(subscription=self.key, endpoint=self.endpoint)

        self._pieces = []
        self._interim = ""
        self._ended = threading.Event()

        if len(self.languages) > 1:
            # Deteccion de idioma continua: puede cambiar es<->en a mitad
            # del dictado. Si diera problemas, fijar un solo idioma en .env.
            try:
                cfg.set_property(
                    speechsdk.PropertyId.SpeechServiceConnection_LanguageIdMode,
                    "Continuous",
                )
            except Exception:
                log.debug("No pude activar LID continuo", exc_info=True)
            auto = speechsdk.languageconfig.AutoDetectSourceLanguageConfig(
                languages=self.languages
            )
            self._recognizer = speechsdk.SpeechRecognizer(
                speech_config=cfg,
                auto_detect_source_language_config=auto,
                audio_config=audio_cfg,
            )
        else:
            cfg.speech_recognition_language = self.languages[0]
            self._recognizer = speechsdk.SpeechRecognizer(
                speech_config=cfg, audio_config=audio_cfg
            )

        r = self._recognizer
        r.recognizing.connect(self._on_recognizing)
        r.recognized.connect(self._on_recognized)
        r.canceled.connect(self._on_canceled)
        r.session_stopped.connect(lambda evt: self._ended.set())
        # No bloquea: el handshake con Azure corre en paralelo mientras el
        # push stream va acumulando el audio del microfono.
        r.start_continuous_recognition_async()

    def feed(self, pcm: bytes) -> None:
        stream = self._stream
        if stream is not None:
            stream.write(pcm)

    def stop(self, timeout: float = 8.0) -> str:
        """Cierra el audio, espera los resultados finales y devuelve el texto."""
        if self._recognizer is None:
            return ""
        # close() = fin de audio: Azure emite los `recognized` pendientes y
        # despues session_stopped/canceled(EndOfStream), que setea _ended.
        self._stream.close()
        if not self._ended.wait(timeout):
            log.warning("Timeout (%.0fs) esperando el final del reconocimiento; "
                        "devuelvo lo que hay", timeout)
        self._teardown()
        return " ".join(p for p in self._pieces if p).strip()

    def cancel(self) -> None:
        if self._recognizer is None:
            return
        try:
            self._stream.close()
        except Exception:
            pass
        self._teardown()
        self._pieces = []

    def _teardown(self) -> None:
        try:
            self._recognizer.stop_continuous_recognition_async().get()
        except Exception:
            log.debug("stop_continuous fallo", exc_info=True)
        self._recognizer = None
        self._stream = None

    # -- eventos del SDK (llegan en threads del SDK) --------------------------

    def _running_text(self) -> str:
        parts = [p for p in self._pieces if p]
        if self._interim:
            parts.append(self._interim)
        return " ".join(parts)

    def _on_recognizing(self, evt) -> None:
        self._interim = evt.result.text or ""
        self.on_partial(self._running_text())

    def _on_recognized(self, evt) -> None:
        if evt.result.reason == speechsdk.ResultReason.RecognizedSpeech and evt.result.text:
            self._pieces.append(evt.result.text)
        self._interim = ""
        self.on_partial(self._running_text())

    def _on_canceled(self, evt) -> None:
        details = getattr(evt, "cancellation_details", None)
        if details and details.reason == speechsdk.CancellationReason.Error:
            log.error("Azure STT error: %s", details.error_details)
            self.on_error("ERROR de Azure — ver log")
        self._ended.set()
