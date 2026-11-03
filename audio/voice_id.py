"""Speaker verification for Orion.

Two things in one module:

1. **Voice identification** -- compare a captured microphone phrase against an
   enrolled voice fingerprint and decide whether the speaker is the owner or
   someone else. Uses `resemblyzer` (a small ~25 MB CPU-only model). The model
   is loaded lazily on first use and stays out of the GPU.

2. **Lock state** -- a tiny on-disk flag (open vs. locked) toggled by spoken
   commands like "only listen to me" / "anyone can talk to you". Locked mode
   ignores anything that isn't the owner's voice; the toggle commands
   themselves are always owner-gated.

When the optional `resemblyzer` package is not installed, `available()` is
False and Orion treats every speaker as the owner -- the privacy gate degrades
gracefully so the rest of the assistant keeps working.
"""
import io
import json
import os
import re
import threading
import wave

import _paths
import config

_HERE = _paths.ROOT
_PROFILE_PATH = os.path.join(_HERE, getattr(config, "VOICE_PROFILE_FILE",
                                            "voice_profile.json"))
_STATE_PATH = os.path.join(_HERE, getattr(config, "VOICE_STATE_FILE",
                                          "voice_state.json"))

_encoder = None
_encoder_failed = False
_encoder_lock = threading.Lock()

_profile_vec = None        # numpy float32 256-d, or None if not enrolled
_profile_loaded = False


def _load_encoder():
    """Build the resemblyzer encoder once. Returns it or None."""
    global _encoder, _encoder_failed
    if _encoder is not None or _encoder_failed:
        return _encoder
    with _encoder_lock:
        if _encoder is not None or _encoder_failed:
            return _encoder
        try:
            from resemblyzer import VoiceEncoder
            _encoder = VoiceEncoder(device="cpu", verbose=False)
        except Exception:
            _encoder_failed = True
    return _encoder


def available():
    """True if resemblyzer is installed and an encoder can be built."""
    return _load_encoder() is not None


def _load_profile():
    """Load the saved owner fingerprint into memory (once)."""
    global _profile_vec, _profile_loaded
    if _profile_loaded:
        return _profile_vec
    _profile_loaded = True
    try:
        import numpy as np
        with open(_PROFILE_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        vec = data.get("embedding")
        if isinstance(vec, list) and vec:
            _profile_vec = np.asarray(vec, dtype="float32")
    except (OSError, ValueError):
        pass
    return _profile_vec


def is_enrolled():
    """True if a voice profile has been saved."""
    return _load_profile() is not None


def _audio_to_samples(audio_data):
    """Convert an sr.AudioData phrase to a 16kHz mono float32 numpy array."""
    import numpy as np
    wav_bytes = audio_data.get_wav_data(convert_rate=16000, convert_width=2)
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        raw = wf.readframes(wf.getnframes())
    samples = np.frombuffer(raw, dtype=np.int16).astype("float32") / 32768.0
    return samples


_warmed = False


def warmup():
    """Load the encoder AND run one dummy inference on a background thread.

    Without this, the FIRST spoken command after boot stalls for ~4 s in
    `_listen` while resemblyzer lazily loads its model and JITs the first
    forward pass — the orange "thinking" state would sit silent for that
    whole time before Orion's filler word played. Safe to call when
    resemblyzer isn't installed; the inner load just fails silently.
    """
    def _run():
        global _warmed
        if _warmed:
            return
        encoder = _load_encoder()
        if encoder is None:
            _warmed = True
            return
        try:
            import numpy as np
            # ~1.5 s of silence at 16 kHz mono float32 -- enough for
            # resemblyzer's preprocessing to slice into frames and run one
            # forward pass through the network, warming its CUDA/CPU
            # kernels so the first real identify() call is instant.
            silence = np.zeros(16000 * 2, dtype=np.float32)
            _samples_to_embedding(silence)
        except Exception:
            pass
        _warmed = True
    threading.Thread(target=_run, daemon=True).start()


def _samples_to_embedding(samples):
    """Run the encoder on float32 samples. Returns a 256-d vector or None."""
    encoder = _load_encoder()
    if encoder is None:
        return None
    try:
        from resemblyzer import preprocess_wav
        wav = preprocess_wav(samples, source_sr=16000)
        return encoder.embed_utterance(wav)
    except Exception:
        return None


def enroll(audio_data):
    """Save the owner's voice fingerprint from one or more recordings.

    `audio_data` may be a single sr.AudioData or a list of them. Multiple
    samples are averaged into a single embedding (more robust to noise).
    Returns True on success.
    """
    import numpy as np
    samples_list = audio_data if isinstance(audio_data, (list, tuple)) else [audio_data]
    embeddings = []
    for ad in samples_list:
        samples = _audio_to_samples(ad)
        vec = _samples_to_embedding(samples)
        if vec is not None:
            embeddings.append(vec)
    if not embeddings:
        return False
    avg = np.mean(np.stack(embeddings, axis=0), axis=0)
    # Normalise so cosine similarity = dot product at compare time.
    norm = float(np.linalg.norm(avg)) or 1.0
    avg = avg / norm
    try:
        with open(_PROFILE_PATH, "w", encoding="utf-8") as fh:
            json.dump({"embedding": [float(x) for x in avg]}, fh)
    except OSError:
        return False
    global _profile_vec, _profile_loaded
    _profile_vec = avg.astype("float32")
    _profile_loaded = True
    return True


def identify(audio_data):
    """Return "owner" or "other" for the captured phrase.

    Falls back to "owner" (the safe default for a single-user system) when:
      - voice verification is disabled in config,
      - resemblyzer is not installed,
      - no voice profile has been enrolled yet,
      - the audio is unusable.
    The OWNER privacy gates in memory.py rely on this returning "other" only
    when we are CONFIDENT the speaker is someone else.
    """
    if not getattr(config, "VOICE_VERIFICATION", False):
        return "owner"
    if not available() or not is_enrolled():
        return "owner"
    try:
        import numpy as np
        samples = _audio_to_samples(audio_data)
        vec = _samples_to_embedding(samples)
        if vec is None:
            return "owner"
        norm = float(np.linalg.norm(vec)) or 1.0
        vec = vec / norm
        score = float(np.dot(_profile_vec, vec))
    except Exception:
        return "owner"
    threshold = getattr(config, "VOICE_THRESHOLD", 0.70)
    return "owner" if score >= threshold else "other"


# --- lock-mode persistence ---
def _load_state():
    try:
        with open(_STATE_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        mode = str(data.get("mode", "open")).lower()
        return "locked" if mode == "locked" else "open"
    except (OSError, ValueError):
        return "open"


def _save_state(mode):
    try:
        with open(_STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump({"mode": mode}, fh)
    except OSError:
        pass


def get_mode():
    """Return the current mode: "open" (default) or "locked"."""
    return _load_state()


def set_mode(mode):
    mode = "locked" if mode == "locked" else "open"
    _save_state(mode)
    return mode


# --- spoken toggle phrases ---
_LOCK_RES = [
    re.compile(r"\bonly\s+listen\s+to\s+me\b", re.I),
    re.compile(r"\block\s*down\b", re.I),
    re.compile(r"\blockdown\s+mode\b", re.I),
    re.compile(r"\bprivate\s+mode\b", re.I),
    re.compile(r"\bonly\s+(obey|answer)\s+me\b", re.I),
]
_UNLOCK_RES = [
    re.compile(r"\banyone\s+can\s+(talk|speak)\b", re.I),
    re.compile(r"\beveryone\s+can\s+(talk|speak)\b", re.I),
    re.compile(r"\bopen\s+up\b", re.I),
    re.compile(r"\bopen\s+mode\b", re.I),
    re.compile(r"\bpublic\s+mode\b", re.I),
    re.compile(r"\bunlock\b", re.I),
]


def is_lock_phrase(text):
    return bool(text) and any(p.search(text) for p in _LOCK_RES)


def is_unlock_phrase(text):
    return bool(text) and any(p.search(text) for p in _UNLOCK_RES)
