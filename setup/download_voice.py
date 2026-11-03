"""Download the Piper voice model Orion speaks with.

Run once:

    python download_voice.py

It fetches the two files (`.onnx` and `.onnx.json`) for the voice named in
config.PIPER_VOICE and puts them in config.PIPER_VOICE_DIR. Re-run it after
changing PIPER_VOICE in config.py.
"""

import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _paths
import config

_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main/"

# Where each supported voice lives inside the rhasspy/piper-voices repo.
_VOICE_PATHS = {
    "en_US-ryan-high": "en/en_US/ryan/high/en_US-ryan-high",
    "en_US-ryan-medium": "en/en_US/ryan/medium/en_US-ryan-medium",
    "en_US-hfc_male-medium": "en/en_US/hfc_male/medium/en_US-hfc_male-medium",
    "en_US-joe-medium": "en/en_US/joe/medium/en_US-joe-medium",
    "en_US-lessac-medium": "en/en_US/lessac/medium/en_US-lessac-medium",
    # British male voices -- the calm, measured JARVIS tone.
    "en_GB-alan-medium": "en/en_GB/alan/medium/en_GB-alan-medium",
    "en_GB-alan-low": "en/en_GB/alan/low/en_GB-alan-low",
    "en_GB-northern_english_male-medium":
        "en/en_GB/northern_english_male/medium/"
        "en_GB-northern_english_male-medium",
}


def _download(url, dest):
    print(f"  downloading {os.path.basename(dest)} ...")
    urllib.request.urlretrieve(url, dest)


def main():
    voice = config.PIPER_VOICE
    rel = _VOICE_PATHS.get(voice)
    if rel is None:
        print(f"Unknown voice '{voice}'.")
        print(f"Known voices: {', '.join(sorted(_VOICE_PATHS))}")
        sys.exit(1)

    out_dir = os.path.join(_paths.ROOT, config.PIPER_VOICE_DIR)
    os.makedirs(out_dir, exist_ok=True)
    onnx = os.path.join(out_dir, voice + ".onnx")
    cfg = onnx + ".json"

    print(f"Downloading Piper voice '{voice}' into {out_dir}")
    _download(_BASE + rel + ".onnx", onnx)
    _download(_BASE + rel + ".onnx.json", cfg)
    print("Done. Orion will use this voice the next time it starts.")


if __name__ == "__main__":
    main()
