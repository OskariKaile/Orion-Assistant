"""Local wake-word detection for Orion using openWakeWord.

A small offline neural model listens to the microphone continuously and fires
the instant it hears the wake word -- no cloud speech-to-text round trip, so
the HUD can light up immediately. Falls back gracefully: if `openwakeword`
isn't installed (or the model can't be loaded), `WakeDetector.available`
stays False and Orion uses phrase-based wake detection instead.
"""

import os

import config

# openWakeWord expects 16 kHz mono 16-bit PCM; 1280 samples = 80 ms per frame.
SAMPLE_RATE = 16000
FRAME_SAMPLES = 1280

# Pretrained models that openWakeWord can download on demand. There is no
# built-in "orion" model -- a custom one must be trained and supplied as a
# .onnx file path via config.WAKE_MODEL.
_BUILTIN_MODELS = {
    "hey_jarvis", "alexa", "hey_mycroft", "hey_rhasspy", "timer", "weather",
}


class WakeDetector:
    """Continuous offline wake-word listener.

    `available` is True only when the model loaded successfully; callers must
    check it and fall back to phrase detection when it is False.
    """

    def __init__(self):
        self.available = False
        self._model = None
        self._pa = None
        self._format = None

        try:
            import openwakeword
            from openwakeword.model import Model
            import pyaudio
        except ImportError:
            return

        name = config.WAKE_MODEL
        try:
            if name in _BUILTIN_MODELS:
                # Pretrained models download once into the package cache.
                openwakeword.utils.download_models([name])
                self._model = Model(wakeword_models=[name],
                                    inference_framework="onnx")
            else:
                if not os.path.exists(name):
                    return
                self._model = Model(wakeword_models=[name],
                                    inference_framework="onnx")
        except Exception:
            return

        self._pa = pyaudio.PyAudio()
        self._format = pyaudio.paInt16
        self.available = True

    def wait_for_wake(self, should_continue):
        """Block until the wake word is heard.

        Returns True the instant the model fires. Returns False if
        `should_continue()` turns false (Orion shutting down or muted), so the
        microphone stream is always released for the command capture that
        follows.
        """
        import numpy as np

        try:
            stream = self._pa.open(
                rate=SAMPLE_RATE, channels=1, format=self._format,
                input=True, frames_per_buffer=FRAME_SAMPLES,
            )
        except Exception:
            return False

        self._model.reset()  # clear any audio buffered from a previous wake
        try:
            while should_continue():
                data = stream.read(FRAME_SAMPLES, exception_on_overflow=False)
                audio = np.frombuffer(data, dtype=np.int16)
                scores = self._model.predict(audio)
                if any(s >= config.WAKE_THRESHOLD for s in scores.values()):
                    return True
            return False
        except Exception:
            return False
        finally:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
