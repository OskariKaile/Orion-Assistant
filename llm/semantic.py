"""Semantic intent matching for Orion.

Turns a spoken phrase into one of Orion's built-in intents by *meaning*
instead of by matching exact words. "I want to look something up" lands on
the same intent as "search the web" even though they share no keywords.

Runs entirely on the CPU (ONNX runtime + all-MiniLM-L6-v2, ~90 MB on disk).
One classification takes a few milliseconds and never touches the GPU, so a
game keeps every megabyte of the RTX 4050's VRAM while Orion stays on.

If the optional `fastembed` package is not installed, every function here
degrades gracefully to "no opinion" and Orion falls back to its difflib
brain. The model file is downloaded once on first use (needs internet that
one time only); afterwards it loads from a local cache.
"""

import threading

import config

_model = None
_model_lock = threading.Lock()
_load_failed = False

# intent name -> list of unit-normalized example embedding vectors.
_intent_vectors = {}
_ready = False
_build_lock = threading.Lock()


def _load_model():
    """Lazily construct the embedding model. Returns the model or None."""
    global _model, _load_failed
    if _model is not None or _load_failed:
        return _model
    with _model_lock:
        if _model is not None or _load_failed:
            return _model
        try:
            from fastembed import TextEmbedding
            _model = TextEmbedding(model_name=config.EMBED_MODEL)
        except Exception:
            # fastembed missing, no internet for the first download, etc.
            _load_failed = True
    return _model


def _embed(texts):
    """Embed a list of strings. Returns a list of vectors, or None."""
    model = _load_model()
    if model is None:
        return None
    try:
        return list(model.embed(list(texts)))
    except Exception:
        return None


def embed_texts(texts):
    """Public wrapper: embed strings using the same local model the semantic
    brain uses. Returns a list of numpy vectors, or None if fastembed isn't
    available. Reused by memory.py for conversation archive search so we don't
    load a second copy of the embedding model."""
    return _embed(texts)


def _build():
    """Embed every intent's example phrases once and cache the result."""
    global _ready
    if _ready or _load_failed:
        return
    with _build_lock:
        if _ready or _load_failed:
            return
        # Imported here (not at module top) to avoid a circular import:
        # brain imports semantic, and this reuses brain's phrase lists.
        import brain
        import numpy as np

        built = {}
        for intent, phrases in brain._INTENTS.items():
            vecs = _embed(phrases)
            if not vecs:
                return
            unit = []
            for v in vecs:
                norm = float(np.linalg.norm(v)) or 1.0
                unit.append(v / norm)
            built[intent] = unit
        _intent_vectors.update(built)
        _ready = bool(_intent_vectors)


def warmup():
    """Load and embed everything on a background thread.

    Called once at startup so the first real command isn't slowed down by
    the model load. Safe to call when fastembed isn't installed.
    """
    threading.Thread(target=_build, daemon=True).start()


def classify(text):
    """Return (intent, confidence) for `text` by semantic similarity.

    confidence is a cosine similarity in 0..1. Returns (None, 0.0) if the
    semantic brain is unavailable or `text` is empty.
    """
    if not text or not text.strip():
        return None, 0.0
    _build()
    if not _ready:
        return None, 0.0

    import numpy as np

    q = _embed([text])
    if not q:
        return None, 0.0
    qv = q[0]
    qv = qv / (float(np.linalg.norm(qv)) or 1.0)

    best_intent, best = None, 0.0
    for intent, vecs in _intent_vectors.items():
        for v in vecs:
            score = float(np.dot(qv, v))
            if score > best:
                best_intent, best = intent, score
    return best_intent, best
