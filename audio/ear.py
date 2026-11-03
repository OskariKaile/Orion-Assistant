"""Always-on ear: continuous listening while Orion thinks and talks.

Orion's normal capture path (`Orion._listen`) is strictly turn-based. It opens
the microphone, records one phrase, closes the microphone, and nothing listens
again until the whole reply has finished playing. That leaves two dead zones:

  * The **thinking** gap. You finish a sentence, pause, then add to it two
    seconds later ("change the song" ... "and turn it down a bit"). The
    addition lands while Orion is already working and is lost.
  * The **speaking** window. Interrupting mid-reply requires the wake word,
    because an open microphone during playback would otherwise hear Orion's
    own voice through the speakers and interrupt itself forever.

This module fixes both by running the microphone continuously through those
phases, with an echo guard that keeps Orion from hearing itself.

# How the echo guard works

Three layers, cheapest first. Each one alone is unreliable; together they are
what makes an open microphone safe on loudspeakers.

1. **A self-calibrating loudness bar.** While Orion is playing audio, the bar
   for "this is a human, not the speakers" is raised above the loudness
   Orion's own voice measures at this device and volume. That level is
   *learned*: whenever a higher layer proves a phrase was Orion hearing
   itself, the level is fed back here via `note_self` and the bar climbs. On
   headphones nothing leaks, the level never rises, and the bar stays at the
   ambient floor.
2. **A transcript match.** Orion knows the exact words it is playing, so a
   candidate whose transcript overlaps that text is echo, not speech. Layer 2
   is also the teacher for layer 1.
3. **Speaker verification.** With a voice profile enrolled, Orion's synthetic
   voice never matches the owner's. Applied by the caller via `voice_id`.

Layers 2 and 3 live on the caller's side (they need transcription); this
module owns layer 1 and exposes `note_self` so the others can teach it.
"""

import collections
import threading
import time

import numpy as np

import config

# 16 kHz mono 16-bit, in 30 ms frames -- small enough that speech onset is
# noticed within a frame or two, large enough to keep the loop cheap.
SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000


class Phrase:
    """One captured utterance plus the context needed to judge it."""

    def __init__(self, audio, t_start, t_end, rms, during_playback):
        self.audio = audio                      # sr.AudioData, ready for STT
        self.t_start = t_start                  # monotonic, speech began
        self.t_end = t_end                      # monotonic, speech ended
        self.rms = rms                          # peak loudness of the phrase
        self.during_playback = during_playback  # Orion was talking over it


class ContinuousEar:
    """A microphone that stays open across a whole conversational turn.

    Start it when a turn begins, stop it when the turn cycle ends. While it
    runs it emits a `Phrase` for every stretch of speech loud enough to clear
    the echo guard, and fires `on_speech_start` the instant one begins so the
    caller can cut playback off without waiting for the sentence to finish.
    """

    def __init__(self, on_speech_start=None):
        self._on_speech_start = on_speech_start
        self._phrases = collections.deque()
        self._new = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._running = False
        self._pa = None

        # Ambient floor, tracked continuously from the quietest recent frames
        # so the bar follows the room rather than a fixed constant.
        self._ambient = float(getattr(config, "ENERGY_THRESHOLD", 300))
        self._quiet = collections.deque(maxlen=200)  # ~6 s of frames

        # Learned loudness of Orion's own voice as heard by this microphone.
        # Zero means "never yet heard itself", i.e. headphones.
        self._self_level = 0.0

        self._playing = False
        self._playing_text = ""

        # Self-calibration window. For a moment at the start of every reply
        # the microphone is measuring one thing only: how loudly Orion's own
        # voice comes back into it on this device at this volume. See
        # `set_playing`.
        self._calib_until = 0.0
        self._calib_peak = 0.0

    # --- lifecycle ---------------------------------------------------------

    def start(self):
        """Open the microphone and begin listening. Idempotent.

        Returns False rather than opening a second stream if the previous
        capture thread has not let go of the device yet. Stacking streams on
        one audio backend is how you get a crash with no Python traceback, and
        the caller has a turn-based fallback to use instead.
        """
        if self._running:
            return True
        previous = self._thread
        if previous is not None and previous.is_alive():
            previous.join(timeout=2.0)
            if previous.is_alive():
                return False
        self._thread = None
        try:
            import pyaudio
        except ImportError:
            return False
        if self._pa is None:
            try:
                self._pa = pyaudio.PyAudio()
            except Exception:
                return False
        self._running = True
        self.drain()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="ear")
        self._thread.start()
        return True

    def stop(self):
        """Release the microphone. Safe to call when already stopped.

        A thread that misses the join keeps its reference, so the next
        `start` can see it is still holding the device rather than opening a
        second stream behind it.
        """
        self._running = False
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=1.5)
            if t.is_alive():
                self.set_playing(False)
                return
        self._thread = None
        self.set_playing(False)

    @property
    def running(self):
        return self._running

    def shutdown(self):
        self.stop()
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None

    # --- caller-facing state ----------------------------------------------

    def set_playing(self, playing, text=""):
        """Tell the ear whether Orion is currently making sound.

        `text` is what is being spoken, so the caller can run the transcript
        layer of the echo guard against it.

        Starting playback also opens a short calibration window. In the first
        fraction of a second of a reply the user has not had time to react, so
        whatever the microphone hears then is Orion itself: on headphones,
        near silence; on loudspeakers, the leak. Measuring it every single
        reply is what lets the guard work on speakers with no setup, and lets
        it follow the volume knob instead of being tuned once and going stale.
        """
        with self._lock:
            self._playing = bool(playing)
            self._playing_text = text if playing else ""
            if playing:
                self._calib_until = time.monotonic() + float(
                    getattr(config, "EAR_CALIBRATE_MS", 450)) / 1000.0
                self._calib_peak = 0.0
            else:
                self._calib_until = 0.0

    @property
    def playing_text(self):
        with self._lock:
            return self._playing_text

    def note_self(self, rms):
        """Teach the guard that a phrase this loud was Orion hearing itself.

        Called by the higher layers when they catch an echo that slipped past
        the loudness bar. The level only ever climbs during a session (it is a
        leak ceiling, not an average), so one proven echo is enough to shut the
        door on every quieter one after it.
        """
        with self._lock:
            self._self_level = max(self._self_level, float(rms))

    @property
    def self_level(self):
        with self._lock:
            return self._self_level

    def bar(self):
        """The loudness a frame must beat right now to count as speech."""
        with self._lock:
            ambient = self._ambient
            playing = self._playing
            leak = self._self_level
        floor = max(ambient * float(getattr(config, "EAR_AMBIENT_MARGIN", 2.2)),
                    float(getattr(config, "EAR_MIN_BAR", 120)))
        if not playing:
            return floor
        # Orion is talking: clear its own leakage by a healthy margin.
        return max(floor, leak * float(getattr(config, "EAR_ECHO_MARGIN", 1.8)))

    # --- queue -------------------------------------------------------------

    def get(self, timeout=0.0):
        """Pop the oldest captured phrase, or None if none arrived in time."""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            with self._lock:
                if self._phrases:
                    return self._phrases.popleft()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self._new.wait(timeout=min(remaining, 0.1))
            self._new.clear()

    def drain(self):
        """Discard anything captured but not yet read."""
        with self._lock:
            self._phrases.clear()
        self._new.clear()

    # --- capture loop ------------------------------------------------------

    def _run(self):
        import pyaudio
        try:
            stream = self._pa.open(
                rate=SAMPLE_RATE, channels=1, format=pyaudio.paInt16,
                input=True, frames_per_buffer=FRAME_SAMPLES,
            )
        except Exception:
            self._running = False
            return

        pre_roll = collections.deque(
            maxlen=max(1, int(float(getattr(config, "EAR_PRE_ROLL", 0.5))
                              * 1000 / FRAME_MS)))
        hang_frames = max(1, int(float(getattr(config, "PAUSE_THRESHOLD", 0.7))
                                 * 1000 / FRAME_MS))
        onset_frames = max(1, int(float(getattr(config, "EAR_ONSET_MS", 90))
                                  / FRAME_MS))
        max_frames = int(float(getattr(config, "PHRASE_TIME_LIMIT", 15))
                         * 1000 / FRAME_MS)

        in_speech = False
        loud_run = 0
        quiet_run = 0
        frames = []
        peak = 0.0
        t_start = 0.0

        try:
            while self._running:
                try:
                    raw = stream.read(FRAME_SAMPLES,
                                      exception_on_overflow=False)
                except Exception:
                    break
                pcm = np.frombuffer(raw, dtype=np.int16)
                if pcm.size == 0:
                    continue
                rms = float(np.sqrt(np.mean(pcm.astype(np.float32) ** 2)))

                if self._calibrating(rms):
                    # Measuring Orion's own leak; nothing here is speech.
                    if in_speech:
                        self._emit(frames, t_start, peak)
                        in_speech = False
                        frames = []
                    loud_run = 0
                    pre_roll.append(raw)
                    continue

                bar = self.bar()

                if not in_speech:
                    pre_roll.append(raw)
                    self._track_ambient(rms, bar)
                    if rms > bar:
                        loud_run += 1
                        if loud_run >= onset_frames:
                            in_speech = True
                            quiet_run = 0
                            frames = list(pre_roll)
                            pre_roll.clear()
                            peak = rms
                            t_start = time.monotonic()
                            self._fire_onset(rms)
                    else:
                        loud_run = 0
                    continue

                frames.append(raw)
                peak = max(peak, rms)
                if rms > bar:
                    quiet_run = 0
                else:
                    quiet_run += 1
                if quiet_run >= hang_frames or len(frames) >= max_frames:
                    self._emit(frames, t_start, peak)
                    in_speech = False
                    loud_run = 0
                    frames = []
                    pre_roll.clear()
        finally:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
            self._running = False
            # Let go of our own reference so the next start() doesn't mistake
            # a finished thread for one still holding the device.
            if self._thread is threading.current_thread():
                self._thread = None

    def _calibrating(self, rms):
        """Feed the leak measurement while the calibration window is open.

        Returns True for as long as the window is open, so the caller skips
        speech detection entirely. Closing the window commits the measurement:
        it takes effect at once if the leak got louder, and decays gently if
        it got quieter, so an unlucky sample (you happened to speak in that
        first half-second) heals over the next few replies instead of leaving
        Orion deaf to you for the rest of the session.
        """
        now = time.monotonic()
        with self._lock:
            if not self._calib_until:
                return False
            if now < self._calib_until:
                self._calib_peak = max(self._calib_peak, rms)
                return True
            measured = self._calib_peak
            self._calib_until = 0.0
            self._calib_peak = 0.0
            self._self_level = max(measured, self._self_level * 0.9)
        return False

    def _track_ambient(self, rms, bar):
        """Follow the room's noise floor using frames that aren't speech."""
        if rms >= bar:
            return
        self._quiet.append(rms)
        if len(self._quiet) >= 20:
            # A low percentile, so an occasional keyboard clack or a breath
            # doesn't drag the floor up and deafen the ear.
            floor = float(np.percentile(np.array(self._quiet), 20))
            with self._lock:
                self._ambient = max(20.0, floor)

    def _fire_onset(self, rms):
        cb = self._on_speech_start
        if cb is None:
            return
        try:
            cb(rms)
        except Exception:
            pass

    def _emit(self, frames, t_start, peak):
        import speech_recognition as sr
        with self._lock:
            during = self._playing
        try:
            audio = sr.AudioData(b"".join(frames), SAMPLE_RATE, 2)
        except Exception:
            return
        phrase = Phrase(audio, t_start, time.monotonic(), peak, during)
        with self._lock:
            self._phrases.append(phrase)
        self._new.set()


# --- transcript layer of the echo guard ------------------------------------

# Interruptions Orion must never dismiss as its own echo, even when the same
# word happens to appear in the sentence being spoken. Cutting the reply off
# is exactly what these mean.
_INTERRUPTS = {
    "stop", "wait", "no", "cancel", "nevermind", "never mind", "shut up",
    "quiet", "hold on", "enough", "forget it", "pause", "shush",
}


def _words(text):
    stripped = (w.strip(".,!?;:'\"") for w in text.split())
    return [w for w in stripped if w]


def looks_like_echo(heard, spoken, threshold=None):
    """True if `heard` is most likely Orion's own voice coming back.

    Compares the transcript of a captured phrase against the text Orion is
    playing right now. A high word overlap means the microphone picked up the
    loudspeaker. Single-word phrases are judged strictly, because one word in
    common proves nothing on its own.
    """
    heard = (heard or "").strip().lower()
    spoken = (spoken or "").strip().lower()
    if not heard or not spoken:
        return False
    if heard.strip(".!?,") in _INTERRUPTS:
        return False
    if threshold is None:
        threshold = float(getattr(config, "EAR_ECHO_SIMILARITY", 0.6))

    heard_words = _words(heard)
    spoken_words = set(_words(spoken))
    if not heard_words:
        return False
    overlap = sum(1 for w in heard_words if w in spoken_words) / len(heard_words)
    if len(heard_words) == 1:
        # One word matching one word of a long reply is a coincidence waiting
        # to happen, so a lone word counts as echo only when it is a content
        # word Orion actually just said.
        return overlap >= 1.0 and len(heard_words[0]) > 3
    return overlap >= threshold
