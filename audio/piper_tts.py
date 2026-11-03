"""Local neural text-to-speech for Orion using Piper.

Piper is a fast, fully offline neural TTS engine. It runs on the CPU only --
it never touches the GPU, so a game keeps every megabyte of the RTX 4050's
VRAM while Orion talks. A few-sentence reply synthesizes in well under a
second.

The voice is two files -- a `.onnx` model and a matching `.onnx.json` config
-- living in config.PIPER_VOICE_DIR. Download them once with:

    python download_voice.py

If Piper or the voice model is missing, the Speaker falls back to the
built-in pyttsx3 voice automatically, so Orion always has a voice.
"""

import os
import re
import threading

import _paths
import config

_voice = None
_load_failed = False
_lock = threading.Lock()

# Splits a reply into sentences so they can be synthesized and played one at
# a time -- the first sentence starts speaking while the rest are still being
# synthesized, cutting the start-of-speech delay. Keeps the terminator with
# the sentence it ends.
_SENTENCE_RE = re.compile(r".+?(?:[.!?]+(?:\s|$)|$)", re.DOTALL)

# Long sentences are also broken on commas / semicolons / colons so each
# Piper synth call gets a shorter input. Piper's autoregressive attention
# occasionally drifts on long chunks and emits garbled phonemes for the
# rest of the call; shorter chunks make drift rarer AND limit how much
# audio a single drift can ruin.
_SUBSENT_RE = re.compile(r".+?(?:[,;:](?:\s|$)|$)", re.DOTALL)
_LONG_CHUNK_CHARS = 80


def split_sentences(text):
    """Break `text` into chunks small enough for stable Piper synthesis."""
    parts = [m.group().strip() for m in _SENTENCE_RE.finditer(text)]
    out = []
    for p in parts:
        if not p:
            continue
        if len(p) <= _LONG_CHUNK_CHARS:
            out.append(p)
            continue
        # Sentence is long -- split further on commas/semicolons/colons.
        subs = [m.group().strip() for m in _SUBSENT_RE.finditer(p)]
        subs = [s for s in subs if s]
        out.extend(subs or [p])
    return out


def _syn_config():
    """Build a Piper SynthesisConfig from the prosody knobs in config.py."""
    try:
        from piper import SynthesisConfig
    except Exception:
        return None
    # normalize_audio is off on purpose: Piper scales every chunk to full
    # scale on its own, which clips the peaks and gives each sentence a
    # different loudness -- heard as crackle and level jumps. Volume is
    # applied once in synth() instead (see _GAIN).
    return SynthesisConfig(
        length_scale=getattr(config, "PIPER_LENGTH_SCALE", None),
        noise_scale=getattr(config, "PIPER_NOISE_SCALE", None),
        noise_w_scale=getattr(config, "PIPER_NOISE_W_SCALE", None),
        normalize_audio=False,
        volume=1.0,
    )


# Raw (un-normalised) Piper output peaks around 0.55 of full scale, so this
# keeps TTS_VOLUME meaning roughly what it did when Piper normalised.
_GAIN = 1.8
_FADE_S = 0.005   # tiny fade on each chunk edge so joins can't click


def _to_pcm(chunks):
    """Join Piper chunks into 16-bit PCM with one fixed gain, faded chunk
    edges and a peak guard (only ever turns down, never clips)."""
    import numpy as np
    parts = []
    for c in chunks:
        x = c.audio_int16_array.astype(np.float32) / 32768.0
        f = min(len(x) // 2, int(c.sample_rate * _FADE_S))
        if f:
            x[:f] *= np.linspace(0.0, 1.0, f, dtype=np.float32)
            x[-f:] *= np.linspace(1.0, 0.0, f, dtype=np.float32)
        parts.append(x)
    x = np.concatenate(parts) * (_GAIN * getattr(config, "TTS_VOLUME", 1.0))
    peak = float(np.abs(x).max()) if len(x) else 0.0
    if peak > 0.97:
        x *= 0.97 / peak
    return (x * 32767).astype(np.int16).tobytes()


def _voice_paths():
    base = os.path.join(_paths.ROOT, config.PIPER_VOICE_DIR)
    onnx = os.path.join(base, config.PIPER_VOICE + ".onnx")
    return onnx, onnx + ".json"


def _load():
    """Lazily load the Piper voice. Returns the voice object or None."""
    global _voice, _load_failed
    if _voice is not None or _load_failed:
        return _voice
    with _lock:
        if _voice is not None or _load_failed:
            return _voice
        try:
            from piper import PiperVoice
            onnx, cfg = _voice_paths()
            if not (os.path.exists(onnx) and os.path.exists(cfg)):
                _load_failed = True  # voice not downloaded yet
                return None
            _voice = PiperVoice.load(onnx, config_path=cfg)
        except Exception:
            # piper-tts not installed, corrupt model, etc.
            _load_failed = True
    return _voice


def available():
    """True if Piper and its voice model are ready to use."""
    return _load() is not None


def synth(text):
    """Synthesize `text` into raw PCM audio.

    Returns (pcm_bytes, sample_rate, sample_width, channels), or None if
    synthesis failed so the caller can fall back to another engine. The
    audio is 16-bit signed PCM, already scaled by config.TTS_VOLUME and
    shaped by the PIPER_* prosody knobs in config.py.
    """
    voice = _load()
    if voice is None or not text.strip():
        return None
    try:
        # Piper streams the utterance as one or more audio chunks.
        syn = _syn_config()
        chunks = list(voice.synthesize(text, syn_config=syn) if syn
                      else voice.synthesize(text))
        if not chunks:
            return None
        raw = _to_pcm(chunks)
        first = chunks[0]
        return (raw, first.sample_rate,
                first.sample_width, first.sample_channels)
    except Exception:
        return None
