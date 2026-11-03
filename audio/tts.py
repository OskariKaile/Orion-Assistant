"""Text-to-speech engine for Orion with interruptible speech.

Two back ends, chosen by config.TTS_ENGINE:
  - "piper":   local neural TTS -- natural sounding, fully offline, CPU only.
  - "pyttsx3": the built-in robotic Windows voice.

Piper audio is played by a separate helper process (play_wav.py). That gives
playback its own GIL and CPU scheduling slot, so speech stays smooth even
while Orion's HUD, wake detector and listener threads are busy -- in-process
playback gets starved under that load and crackles.

When "piper" is selected but its engine or voice model is missing, the
Speaker falls back to pyttsx3 automatically so Orion always has a voice.
"""

import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import wave

import config
import piper_tts
try:
    import telemetry
except Exception:
    telemetry = None
try:
    import tts_debug
except Exception:
    tts_debug = None

_PLAYER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "play_wav.py")
# Launch the player without flashing a console window.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class Speaker:
    """Runs TTS on a dedicated thread so speech can be interrupted."""

    def __init__(self):
        self._queue = queue.Queue()
        self._muted = False
        self._speaking = threading.Event()
        self._interrupt = threading.Event()  # set by stop() to cut playback
        self._pyttsx3 = None        # lazily built fallback engine
        self._proc = None           # current playback subprocess
        self._use_piper = False     # decided on the worker thread
        # Pre-rendered filler WAVs: synthesised once at startup so the
        # thinking-filler ("hmm", "let me check") plays INSTANTLY without
        # any synthesis delay. Keyed by category ("search", "tool", "think")
        # so callers can pick a filler that matches the kind of request --
        # see brain.filler_category. Populated by _prerender_fillers after
        # the Speaker worker thread starts.
        self._filler_wavs = {}
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    # --- engine setup ---
    def _build_pyttsx3(self):
        import pyttsx3
        engine = pyttsx3.init()
        engine.setProperty("rate", config.TTS_RATE)
        engine.setProperty("volume", config.TTS_VOLUME)
        for voice in engine.getProperty("voices"):
            name = (voice.name or "").lower()
            if any(hint in name for hint in config.PREFERRED_VOICE_HINTS):
                engine.setProperty("voice", voice.id)
                break
        return engine

    def _ensure_player(self):
        """Make sure the persistent player subprocess is running.

        Spawned on first use and kept alive for the life of Orion. The
        player keeps ONE PyAudio output stream open the whole time, which
        means the device cold-start glitch ("crispy" first words, random
        clicks before speech) happens exactly once -- inside the silence
        prime the player runs at startup -- never again per sentence.
        Respawned automatically if it ever dies or is killed by stop().
        """
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        try:
            self._proc = subprocess.Popen(
                [sys.executable, _PLAYER, "--server"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                creationflags=_NO_WINDOW,
                text=True,
                encoding="utf-8",
                bufsize=1,  # line buffered so DONE arrives promptly
            )
        except Exception:
            self._proc = None
        return self._proc

    def _warm_up(self):
        """No-op on purpose.

        Eagerly spawning the player at boot opened the audio stream while
        nothing was speaking, so the user heard ~10 s of DAC-init "sand"
        noise as a standalone burst before the startup greeting -- audible
        because there was no speech to mask it. We now defer the spawn
        until the first real utterance: the same settle period happens, but
        it overlaps with the greeting being synthesised/played, so the user
        hears the device come to life through the speech instead of as
        bare static. Every sentence AFTER the first remains crisp because
        the persistent player keeps the stream open.
        """
        return

    def _run(self):
        # Pick the back end once. Piper is preferred when configured and
        # ready; otherwise fall back to the always-available pyttsx3 voice.
        self._use_piper = (
            config.TTS_ENGINE == "piper" and piper_tts.available()
        )
        if not self._use_piper:
            self._pyttsx3 = self._build_pyttsx3()

        self._warm_up()

        # Pre-render the thinking fillers to disk so _say_filler is instant.
        # Runs on a SEPARATE background thread -- doing it on this worker
        # thread blocked the queue for ~5 s on first boot, so the first
        # user question would queue up behind the prerender and sit silent
        # in "thinking" until the prerender finally finished.
        if self._use_piper:
            threading.Thread(target=self._prerender_fillers,
                             daemon=True).start()

        while True:
            item = self._queue.get()
            if item is None:
                break
            # Backward-compat: old 3-tuples (text, done, prerendered) still
            # get accepted; `started` defaults to None.
            if len(item) == 4:
                text, done, prerendered, started = item
            else:
                text, done, prerendered = item
                started = None
            if not self._muted and (prerendered or text.strip()):
                self._interrupt.clear()
                self._speaking.set()
                try:
                    if prerendered:
                        # Filler WAV is already on disk -- play it directly.
                        # No synth, no temp file to clean up afterwards.
                        if started is not None:
                            started.set()
                        self._play_file(prerendered)
                    elif self._use_piper:
                        self._speak_piper(text, started=started)
                    else:
                        if started is not None:
                            started.set()
                        self._speak_pyttsx3(text)
                except Exception:
                    pass
                finally:
                    self._speaking.clear()
            else:
                # Muted / empty -- nothing will play. Unblock waiters so
                # callers don't hang on `started`.
                if started is not None:
                    started.set()
            # Signal any caller blocked in say_blocking() that this
            # utterance is finished (or was skipped while muted).
            if done is not None:
                done.set()

    # --- speaking ---
    def _speak_pyttsx3(self, text):
        if self._pyttsx3 is None:
            self._pyttsx3 = self._build_pyttsx3()
        try:
            self._pyttsx3.say(text)
            self._pyttsx3.runAndWait()
        except RuntimeError:
            # runAndWait can raise if stop() was called mid-utterance.
            pass

    def _result_to_wav(self, result, lead_s, tail_s):
        """Write a piper_tts.synth() result to a temp WAV. Returns the path.

        Pads the audio with silence. The lead-in absorbs the device spin-up
        crackle (Windows powers an idle output device down between
        utterances; the next sound spins it back up and the opening ~200 ms
        crackles). The tail keeps the playback stream from being closed
        before the final samples have drained out of the device -- that
        truncation clipped the last syllable off every sentence.
        """
        pcm, rate, width, channels = result
        lead = b"\x00" * (int(rate * lead_s) * width * channels)
        tail = b"\x00" * (int(rate * tail_s) * width * channels)
        pcm = lead + pcm + tail

        fd, path = tempfile.mkstemp(suffix=".wav", prefix="orion_tts_")
        os.close(fd)
        with wave.open(path, "wb") as w:
            w.setnchannels(channels)
            w.setsampwidth(width)
            w.setframerate(rate)
            w.writeframes(pcm)
        return path

    def _prerender_fillers(self):
        """Synth every thinking-filler phrase once and keep the WAV paths.

        Stores results in a dict keyed by category so say_filler() can pick
        a matching bucket. Tolerates the legacy flat-list config shape (the
        whole list is treated as the "think" bucket). Best-effort -- any
        phrase that fails to synth is silently skipped.
        """
        phrases = getattr(config, "THINKING_FILLER_PHRASES", None) or {}
        if isinstance(phrases, (list, tuple)):
            phrases = {"think": list(phrases)}
        for category, items in phrases.items():
            bucket = []
            for phrase in items:
                try:
                    path = self._synth_to_wav(phrase)
                    if path:
                        bucket.append(path)
                except Exception:
                    continue
            if bucket:
                self._filler_wavs[category] = bucket

    def _synth_to_wav(self, sentence):
        """Synthesize one sentence and return the temp WAV path, or None."""
        result = piper_tts.synth(sentence)
        if result is None:
            return None
        # Only a tiny lead-in is needed now: play_wav.py primes the output
        # stream with silence on every open, which absorbs the cold-device
        # spin-up and the stale-buffer "ahh" glitch far more reliably than
        # silence baked into the WAV ever did.
        return self._result_to_wav(result, 0.06, 0.30)

    def _speak_piper(self, text, started=None):
        # Speak the reply sentence by sentence, with PIPELINED synth: while
        # sentence N is playing through the helper process, sentence N+1 is
        # already being synthesised on this thread in the background. By the
        # time playback of N finishes, the next WAV is usually ready -- the
        # gap between sentences drops from "synth time" to ~0.
        #
        # The earlier design deliberately serialised synth and playback to
        # avoid CPU contention crackle. That crackle came from in-process
        # PyAudio playback fighting the synth thread for the GIL; the player
        # is a SEPARATE process now (play_wav.py), so playback has its own
        # scheduling slot and no longer competes with synth.
        sentences = piper_tts.split_sentences(text) or [text]
        # Pre-synth the first sentence before the loop so playback can start
        # the instant we enter it.
        next_path = self._synth_to_wav(sentences[0])
        for i, sentence in enumerate(sentences):
            if self._interrupt.is_set():
                return
            current_path = next_path
            next_path = None
            if current_path is None:
                # Synth failed for this sentence -- fall back to pyttsx3 so
                # the reply is still spoken. Still synth the next one ahead.
                if i + 1 < len(sentences):
                    next_path = self._synth_to_wav(sentences[i + 1])
                self._speak_pyttsx3(sentence)
                continue
            # Kick off synthesis of the NEXT sentence in the background so it
            # overlaps with playback of the current one.
            holder = [None]
            synth_thread = None
            if i + 1 < len(sentences):
                next_sentence = sentences[i + 1]
                synth_thread = threading.Thread(
                    target=lambda: holder.__setitem__(
                        0, self._synth_to_wav(next_sentence)),
                    daemon=True,
                )
                synth_thread.start()
            try:
                # Fire the started signal once -- the moment the FIRST
                # sentence is about to hit the speakers. Later sentences
                # don't re-fire it.
                if started is not None and not started.is_set():
                    started.set()
                self._play_file(current_path)
            finally:
                if tts_debug is not None:
                    try:
                        tts_debug.capture(current_path, sentence)
                    except Exception:
                        pass
                try:
                    os.remove(current_path)
                except OSError:
                    pass
            if synth_thread is not None:
                synth_thread.join()
                next_path = holder[0]

    def _play_file(self, path):
        """Play a WAV through the persistent player. Stoppable mid-utterance.

        Sends the path to the player's stdin and waits for DONE on stdout.
        On interrupt, the player process is terminated; the NEXT call will
        respawn a fresh one (which re-warms the device once).
        """
        proc = self._ensure_player()
        if proc is None or proc.stdin is None or proc.stdout is None:
            return
        try:
            proc.stdin.write(path + "\n")
            proc.stdin.flush()
        except (OSError, ValueError):
            # Pipe broke -- drop the process so the next call respawns it.
            self._kill_player()
            return

        # Wait for DONE on its own thread so the interrupt flag can cut us off.
        done_holder = [False]

        def waiter():
            try:
                line = proc.stdout.readline()
            except Exception:
                line = ""
            done_holder[0] = bool(line) and "DONE" in line

        t = threading.Thread(target=waiter, daemon=True)
        t.start()
        while t.is_alive():
            if self._interrupt.is_set():
                # Kill the player to cut audio NOW; next play respawns it.
                self._kill_player()
                break
            t.join(timeout=0.05)

    def _kill_player(self):
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=1)
        except Exception:
            pass

    # --- public API ---
    def say(self, text, done=None, started=None):
        """Queue text to be spoken.

        If `done` (a threading.Event) is given, it is set once the utterance
        has finished playing -- or immediately if it was skipped while muted.
        `started` (a threading.Event) is set the moment actual audio playback
        begins (after synthesis), so callers can split synth time from
        playback time. Fires immediately when muted (nothing to wait for).
        """
        if telemetry is not None:
            try:
                telemetry.append_reply(text)
            except Exception:
                pass
        self._queue.put((text, done, None, started))

    def say_filler(self, category="think"):
        """Queue a random pre-rendered thinking-filler from `category`.

        Falls back to the "think" bucket if `category` has no entries, and
        to any non-empty bucket if "think" itself is empty. Returns True if
        a filler was queued; False if nothing was pre-rendered (e.g. the
        pyttsx3 fallback engine), so the caller can fall back to plain say().
        """
        if not self._filler_wavs:
            return False
        bucket = (self._filler_wavs.get(category)
                  or self._filler_wavs.get("think"))
        if not bucket:
            for v in self._filler_wavs.values():
                if v:
                    bucket = v
                    break
        if not bucket:
            return False
        import random
        path = random.choice(bucket)
        self._queue.put(("", None, path, None))
        return True

    def say_blocking(self, text):
        """Speak `text` and block until it has finished playing.

        Use this before opening the microphone so Orion never records its
        own voice -- e.g. the "Yes, Boss?" prompt must finish before Orion
        starts listening for the follow-up.
        """
        if telemetry is not None:
            try:
                telemetry.append_reply(text)
            except Exception:
                pass
        done = threading.Event()
        self._queue.put((text, done, None))
        done.wait()

    def stop(self):
        """Interrupt current speech and drop anything queued."""
        while not self._queue.empty():
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            # Release any waiter so say_blocking() can't hang on a dropped item.
            if isinstance(item, tuple) and len(item) >= 2 and item[1] is not None:
                item[1].set()
        self._interrupt.set()
        proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
        if self._pyttsx3 is not None:
            self._pyttsx3.stop()

    def is_speaking(self):
        return self._speaking.is_set()

    def set_muted(self, muted):
        self._muted = muted
        if muted:
            self.stop()

    @property
    def muted(self):
        return self._muted

    def shutdown(self):
        self.stop()
        # Ask the persistent player to exit gracefully, then make sure it's
        # really gone (it might already be -- stop() may have killed it).
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                if proc.stdin is not None:
                    proc.stdin.write("QUIT\n")
                    proc.stdin.flush()
                    proc.stdin.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=1)
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass
        self._proc = None
        for bucket in self._filler_wavs.values():
            for path in bucket:
                try:
                    os.remove(path)
                except OSError:
                    pass
        self._filler_wavs = {}
        self._queue.put(None)
