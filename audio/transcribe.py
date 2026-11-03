"""Local speech-to-text via Moonshine ONNX.

Moonshine is a 2024 ASR model from Useful Sensors purpose-built for voice
assistants: same accuracy as Whisper-small on English speech but ~5x faster
on short utterances because there's no 30-second padding step. Runs entirely
on the CPU via onnxruntime, offline, no API key or quota.

If Moonshine isn't installed or the model fails to load, available() returns
False and Orion falls back to Google speech recognition.
"""

import io
import threading

import numpy as np

import config

_model = None
_tokenizer = None
_load_failed = False
_lock = threading.Lock()
_primed = False


def _get_model():
    """Return the cached MoonshineOnnxModel + tokenizer, loading once."""
    global _model, _tokenizer, _load_failed
    if _model is not None or _load_failed:
        return _model, _tokenizer
    with _lock:
        if _model is not None or _load_failed:
            return _model, _tokenizer
        try:
            from moonshine_onnx import MoonshineOnnxModel, load_tokenizer
            _model = MoonshineOnnxModel(
                model_name=config.MOONSHINE_MODEL,
                model_precision=config.MOONSHINE_PRECISION,
            )
            _tokenizer = load_tokenizer()
        except Exception:
            _load_failed = True
            _model = None
            _tokenizer = None
    return _model, _tokenizer


def available():
    """True if the local Moonshine model loaded successfully."""
    return _get_model()[0] is not None


def _prime(model):
    """Run a few inferences so onnxruntime primes its kernels.

    ONNX caches compiled kernels by input shape, so a single prime at 1s
    of audio only covers 1-second utterances -- the first real ~3-second
    command would re-JIT and stall the listener for several seconds before
    the user heard the thinking filler. Priming at several realistic
    durations covers the common range so the first real command lands on
    an already-warmed kernel set.
    """
    global _primed
    if _primed:
        return
    try:
        # Cover the typical voice-command range: short ("mute"), medium
        # ("what time is it"), and a longer sentence. 16 kHz mono.
        for seconds in (1, 2, 4):
            silence = np.zeros((1, 16000 * seconds), dtype=np.float32)
            model.generate(silence)
        _primed = True
    except Exception:
        _primed = True


def warmup():
    """Preload the model AND prime kernels in a background thread."""
    def _run():
        m, _ = _get_model()
        if m is not None:
            _prime(m)
    threading.Thread(target=_run, daemon=True).start()


def _audio_to_float32(audio_data):
    """Decode an sr.AudioData phrase to a [1, N] float32 array @ 16kHz."""
    raw = audio_data.get_raw_data(convert_rate=16000, convert_width=2)
    pcm = np.frombuffer(raw, dtype=np.int16)
    audio = pcm.astype(np.float32) / 32768.0
    return audio.reshape(1, -1)


def transcribe(audio_data):
    """Transcribe an sr.AudioData phrase to lowercase text.

    Returns "" on failure so the caller can fall back to another engine.
    """
    model, tokenizer = _get_model()
    if model is None or tokenizer is None:
        return ""
    try:
        _prime(model)
        audio = _audio_to_float32(audio_data)
        # Moonshine handles its own end-of-speech detection via the EOS token,
        # so no VAD pass needed here -- the model just decodes until EOS.
        tokens = model.generate(audio)
        text = tokenizer.decode_batch(tokens)[0].strip()
        if _is_hallucination(text):
            return ""
        return text.lower()
    except Exception:
        return ""


# Stock phrases ASR models invent out of noise or clipped audio. As a whole
# transcription none of these is ever a real Orion command, so they are
# discarded. Carried over from the Whisper era; Moonshine has different
# failure modes but the same "single short filler word" case still applies.
_HALLUCINATIONS = {
    "you", "thank you", "thank you.", "thanks for watching",
    "thanks for watching!", "thank you for watching",
    "thank you for watching.", "please subscribe", "ladies and gentlemen",
    "ladies and gentlemen,", "bye", "bye.", ".", "...", "i'm sorry",
}


def _is_hallucination(text):
    """True if `text` is one of the known phantom phrases."""
    cleaned = text.strip().strip(".!,").lower()
    return cleaned in _HALLUCINATIONS or not cleaned
