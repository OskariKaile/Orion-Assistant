"""Helper process for Orion's song recognition.

Run as a subprocess by song.py so the audio capture and Shazam lookup -- which
use native code (PyAudioWPatch's WASAPI, shazamio-core's Rust, ffmpeg) that
can hard-crash the interpreter with no catchable Python exception -- are
isolated in their own process. A crash here only kills this helper; Orion
itself keeps running. This mirrors how play_wav.py isolates audio playback.

Captures system loopback audio first (games, video, calls), then the mic
(music in the room), and prints the result as a single JSON line to stdout:
  {"title": "...", "artist": "..."}   on a match
  {"title": null}                     when nothing was recognised

Every step is also written to song_debug.log next to this file, so a failed
recognition can be diagnosed even when the helper crashes mid-run.
"""

import array
import asyncio
import datetime
import json
import os
import sys
import traceback
import warnings
import wave

warnings.filterwarnings("ignore", category=RuntimeWarning)

_LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "song_debug.log")
_CHUNK = 1024


def _log(msg):
    """Append a timestamped line to song_debug.log, flushed immediately so a
    partial log survives even if the next step hard-crashes the process."""
    try:
        with open(_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}\n")
            fh.flush()
    except Exception:
        pass


def _ensure_ffmpeg():
    """Point pydub at the ffmpeg binary bundled by imageio-ffmpeg."""
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        from pydub import AudioSegment
        AudioSegment.converter = exe
        AudioSegment.ffmpeg = exe
        os.environ["PATH"] = (os.path.dirname(exe) + os.pathsep
                              + os.environ.get("PATH", ""))
        _log(f"ffmpeg wired: {exe}")
    except Exception:
        _log("ffmpeg wiring failed:\n" + traceback.format_exc())


def _capture(seconds, loopback):
    """Record `seconds` of audio with PyAudioWPatch.

    loopback=True records the speaker output via a WASAPI loopback device
    (catches games, video, calls); loopback=False records the microphone.
    Returns (pcm_bytes, sample_rate, channels).
    """
    import pyaudiowpatch as pyaudio

    tag = "loopback" if loopback else "mic"
    p = pyaudio.PyAudio()
    try:
        wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)

        if loopback:
            device = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
            # The default speaker isn't itself a capture device -- find the
            # matching WASAPI loopback device that mirrors its output.
            if not device.get("isLoopbackDevice", False):
                match = None
                for lb in p.get_loopback_device_info_generator():
                    if device["name"] in lb["name"]:
                        match = lb
                        break
                if match is None:
                    raise RuntimeError(
                        f"no loopback device found for speaker "
                        f"'{device['name']}'")
                device = match
        else:
            device = p.get_default_input_device_info()

        rate = int(device["defaultSampleRate"])
        channels = min(max(1, int(device["maxInputChannels"])), 2)
        _log(f"{tag}: device='{device['name']}' rate={rate} "
             f"channels={channels}")

        stream = p.open(format=pyaudio.paInt16, channels=channels, rate=rate,
                        frames_per_buffer=_CHUNK, input=True,
                        input_device_index=device["index"])
        frames = []
        try:
            for _ in range(int(rate / _CHUNK * seconds)):
                frames.append(stream.read(_CHUNK, exception_on_overflow=False))
        finally:
            stream.stop_stream()
            stream.close()
        return b"".join(frames), rate, channels
    finally:
        p.terminate()


def _amplitude(pcm):
    """Peak amplitude of a 16-bit PCM buffer, 0..1. -1 if it can't be read."""
    if not pcm:
        return -1.0
    try:
        samples = array.array("h")
        samples.frombytes(pcm[:len(pcm) - (len(pcm) % 2)])
        if not samples:
            return -1.0
        return max(abs(s) for s in samples) / 32768.0
    except Exception:
        return -1.0


def _write_wav(pcm, rate, channels, name):
    """Write a 16-bit PCM buffer to `name` next to this script. Returns the
    path. The file is kept (not deleted) so a failed recognition can be
    played back to check whether the capture itself is clean."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
    with wave.open(path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return path


async def _shazam_lookup(path):
    from shazamio import Shazam
    shazam = Shazam()
    recognize = (getattr(shazam, "recognize", None)
                 or getattr(shazam, "recognize_song"))
    return await recognize(path)


def _identify_file(path):
    """Identify the song in a WAV file. Returns (title, artist) or (None, '')."""
    try:
        result = asyncio.run(_shazam_lookup(path))
    except Exception:
        _log("shazam lookup raised:\n" + traceback.format_exc())
        return None, ""
    try:
        _log("shazam raw result: " + repr(result)[:1800])
    except Exception:
        _log("shazam raw result: <unprintable>")
    track = (result or {}).get("track") or {}
    title = track.get("title")
    artist = track.get("subtitle") or ""
    if title:
        _log(f"shazam matched: {artist} - {title}")
    else:
        _log("shazam returned no match for this clip")
    return (title, artist) if title else (None, "")


def _warmup():
    """Import the heavy native deps and trigger any one-time downloads
    (ffmpeg via imageio-ffmpeg, shazamio's rust binding) so the first
    real recognition doesn't pay that cost. Used by song.prewarm()."""
    _log("=== warmup start ===")
    _ensure_ffmpeg()
    try:
        import pyaudiowpatch  # noqa: F401
        import shazamio  # noqa: F401
        # Touch a Shazam instance so its lazy imports/network init happen now.
        shazamio.Shazam()
        _log("warmup: deps imported")
    except Exception:
        _log("warmup failed:\n" + traceback.format_exc())
    _log("=== warmup end ===")
    print(json.dumps({"warmup": True}))


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--warmup":
        _warmup()
        return
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    _log(f"=== run start (capture {seconds}s per source) ===")
    _ensure_ffmpeg()

    # System loopback catches games, videos and calls; the mic catches music
    # playing out loud in the room. Whichever has audio gets identified.
    for loopback in (True, False):
        source = "loopback (system audio)" if loopback else "microphone"
        try:
            _log(f"{source}: capturing...")
            pcm, rate, channels = _capture(seconds, loopback)
        except Exception:
            _log(f"{source}: capture raised:\n" + traceback.format_exc())
            continue

        amp = _amplitude(pcm)
        _log(f"{source}: captured {len(pcm)} bytes, peak amplitude = "
             f"{amp:.4f}")
        if amp < 0.01:
            _log(f"{source}: too quiet, skipping")
            continue

        name = ("song_last_capture.wav" if loopback
                else "song_last_capture_mic.wav")
        path = _write_wav(pcm, rate, channels, name)
        _log(f"{source}: saved capture to {name} (play it to check it)")
        title, artist = _identify_file(path)
        if title:
            _log("=== run end: MATCH ===")
            print(json.dumps({"title": title, "artist": artist}))
            return

    _log("=== run end: no match ===")
    print(json.dumps({"title": None}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        _log("helper crashed in main:\n" + traceback.format_exc())
        print(json.dumps({"title": None}))
