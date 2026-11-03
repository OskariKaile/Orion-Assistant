"""Standalone WAV player for Orion.

Orion runs this in its own process so audio playback gets a private GIL and
its own OS scheduling slot. In-process playback shares the GIL with the HUD,
wake detector and listener threads, which starve it and make speech crackle.
A separate process is immune to that contention.

Two run modes
-------------
* **Server mode (`python play_wav.py --server`)** -- the default Orion now
  uses. Opens a single PyAudio output stream and keeps it open for the life
  of the process; the audio device is warmed up exactly once, then stays
  warm. WAV paths are read from stdin, one per line; "DONE" is written to
  stdout after each finishes. This eliminates the cold-start glitch ("crispy"
  first words, random clicks before speech) that came from opening a fresh
  stream for every sentence.
* **One-shot mode (`python play_wav.py <path>`)** -- legacy. Opens a stream,
  plays a single WAV, exits. Kept as a fallback for the warm-up step and for
  callers that want the simple shape.

Why the resampling step matters
-------------------------------
The Piper voice is 22050 Hz, but Windows output devices almost always run at
48000 Hz. If we hand the OS a 22050 Hz stream it resamples it with a cheap,
low-quality converter -- that produces harsh high-frequency aliasing, the
continuous "crispy" distortion heard on every sentence.

So instead this player resamples the speech up to the device's native rate
itself, using SciPy's polyphase resampler (high quality, no aliasing), and
opens the output stream at that exact rate. The OS then has nothing to
resample and the speech comes out clean.

Fallbacks: if PyAudio/SciPy aren't available the one-shot path plays via
winsound, and if even that is missing (non-Windows) it does a plain PyAudio
playback.
"""

import sys
import wave

try:
    import config
except Exception:
    config = None


# --- shared helpers ---
def _device_rate(pa):
    """Pick the output stream rate: forced override if set, else device default."""
    forced = getattr(config, "OUTPUT_SAMPLE_RATE", None) if config else None
    if forced:
        return int(forced)
    try:
        info = pa.get_default_output_device_info()
        return int(info["defaultSampleRate"])
    except Exception:
        return 22050  # safe fallback for Piper's native rate


def _read_wav(path):
    """Load a WAV. Returns (audio_int16, src_rate, channels) or None."""
    with wave.open(path, "rb") as wav:
        src_rate = wav.getframerate()
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        frames = wav.readframes(wav.getnframes())
    if width != 2:
        return None  # only 16-bit PCM is handled here
    import numpy as np
    return np.frombuffer(frames, dtype=np.int16), src_rate, channels


def _resample(audio, src_rate, dst_rate, channels):
    """Resample int16 audio from src_rate to dst_rate (no-op when equal).

    Returning audio at the wrong rate would make the player play it at the
    stream's rate, so a 22050 Hz buffer in a 48000 Hz stream would sound
    sped up ~2.18x and garbled. So if SciPy isn't available or its call
    raises, we fall back to a pure-numpy linear-interpolation resampler --
    lower quality, but always at the correct rate.
    """
    if src_rate == dst_rate:
        return audio
    import numpy as np
    if channels > 1:
        audio = audio.reshape(-1, channels)
    try:
        from math import gcd
        from scipy.signal import resample_poly
        g = gcd(dst_rate, src_rate)
        resampled = resample_poly(audio.astype(np.float32),
                                  dst_rate // g, src_rate // g)
    except Exception as exc:
        # Log so we can tell if scipy is the source of the speed-up bug.
        sys.stderr.write(f"[play_wav] scipy resample failed ({exc!r}); "
                         f"using numpy fallback\n")
        sys.stderr.flush()
        src = audio.astype(np.float32)
        n_out = int(round(len(src) * dst_rate / src_rate))
        if n_out <= 0:
            return np.zeros(0, dtype=np.int16)
        # Linear interpolation over the source samples.
        x_old = np.arange(len(src), dtype=np.float64)
        x_new = np.linspace(0, len(src) - 1, n_out, dtype=np.float64)
        if src.ndim == 2:
            resampled = np.stack(
                [np.interp(x_new, x_old, src[:, c]) for c in range(src.shape[1])],
                axis=1,
            )
        else:
            resampled = np.interp(x_new, x_old, src)
    return np.clip(resampled, -32768, 32767).astype(np.int16)


def _write_to_stream(stream, audio, channels):
    data = audio.tobytes()
    step = 4096 * 2 * channels
    for i in range(0, len(data), step):
        stream.write(data[i:i + step])


# --- server mode (persistent stream) ---
def _server():
    """Keep one stream open for the life of the process; play WAVs from stdin.

    Reads paths line by line. After each file finishes, prints DONE so the
    parent process knows playback completed. Exits cleanly on EOF (parent
    closed stdin) or the literal "QUIT" line.
    """
    try:
        import pyaudio
    except ImportError:
        # Without PyAudio we cannot keep a stream open. Fall back to one-shot
        # winsound playback per line so server mode still does something.
        for line in sys.stdin:
            path = line.strip()
            if not path or path == "QUIT":
                break
            _play_winsound(path)
            print("DONE", flush=True)
        return

    pa = pyaudio.PyAudio()
    out_rate = _device_rate(pa)
    # Piper voices are mono; if a future voice changes that, we still open
    # mono here and let the resampler/mixdown be a future problem.
    stream = pa.open(format=pyaudio.paInt16, channels=1,
                     rate=out_rate, output=True)
    try:
        # Brief one-time silence prime so the first few samples of real
        # audio aren't dropped while the stream is still ramping up. The
        # DAC may continue settling for several more seconds beyond this
        # ("sand"-like static on some laptop audio chips), but we can't
        # shorten that hardware behaviour from software -- the prime here
        # just protects the start of the audio buffer. Speaker spawns this
        # process lazily, so the settle overlaps with the actual speech
        # rather than playing as standalone static at boot.
        step = 4096 * 2
        quiet = b"\x00" * step
        for _ in range(5):  # ~200 ms at 48 kHz mono 16-bit
            stream.write(quiet)

        while True:
            line = sys.stdin.readline()
            if not line:
                break
            path = line.strip()
            if not path or path == "QUIT":
                break
            try:
                loaded = _read_wav(path)
                if loaded is not None:
                    audio, src_rate, channels = loaded
                    audio = _resample(audio, src_rate, out_rate, channels)
                    _write_to_stream(stream, audio, 1)
            except Exception as exc:
                # Surface failures instead of silently swallowing them --
                # silent breakage here was the source of the "speaks too
                # fast / garbled / partial words" bug.
                sys.stderr.write(f"[play_wav] playback failed for {path!r}: "
                                 f"{exc!r}\n")
                sys.stderr.flush()
            print("DONE", flush=True)
    finally:
        try:
            stream.stop_stream()
            stream.close()
        except Exception:
            pass
        pa.terminate()


# --- one-shot fallbacks ---
def _play_winsound(path):
    """Play a WAV through the Windows system mixer. Returns True on success."""
    try:
        import winsound
    except ImportError:
        return False
    winsound.PlaySound(path, winsound.SND_FILENAME)
    return True


def _play_resampled(path):
    """Resample to the device's native rate and play. Returns True on success."""
    try:
        import pyaudio
    except ImportError:
        return False

    loaded = _read_wav(path)
    if loaded is None:
        return False
    audio, src_rate, channels = loaded

    pa = pyaudio.PyAudio()
    try:
        out_rate = _device_rate(pa)
        audio = _resample(audio, src_rate, out_rate, channels)
        stream = pa.open(format=pyaudio.paInt16, channels=channels,
                         rate=out_rate, output=True)
        try:
            step = 4096 * 2 * channels
            # Prime the freshly opened stream with silence (per-stream warm-up).
            quiet = b"\x00" * step
            for _ in range(5):
                stream.write(quiet)
            _write_to_stream(stream, audio, channels)
        finally:
            stream.stop_stream()
            stream.close()
    finally:
        pa.terminate()
    return True


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "--server":
        _server()
        return
    if len(sys.argv) < 2:
        return
    path = sys.argv[1]
    if _play_resampled(path):
        return
    _play_winsound(path)


if __name__ == "__main__":
    main()
