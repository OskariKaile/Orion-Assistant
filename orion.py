"""Orion - a JARVIS-style voice assistant powered by a cloud LLM.

Listens for the wake word, sends the query to the model chosen by
config.LLM_BACKEND (see llm_router.py), and reads the reply aloud.
"""

import datetime
import json
import os
import random
import re
import sys
import threading
import time
import urllib.parse
import webbrowser

import speech_recognition as sr
from PIL import Image, ImageDraw
import pystray

import _paths  # noqa: F401  -- puts audio/, llm/, features/, ui/, setup/ on sys.path
import config

# `ai` is the brain Orion talks to. The router picks a backend per request
# from config.LLM_BACKEND: DeepSeek for text and tool calls, Gemini for the
# two things DeepSeek can't do (images, web-grounded questions).
import llm_router as ai

import brain
import briefing
try:
    import hub_server
    import hub_launcher
except Exception:  # hub is optional; never break Orion if its files are missing
    hub_server = None
    hub_launcher = None
import code_assistant
import commands
import ear as ear_mod
import presence
import memory
import monitor
import reminders as reminders_mod
import semantic
import song
import tools
import transcribe
import replay_debug
import telemetry
import vision
import voice_id
from tts import Speaker

try:
    from hud import HUD
except Exception:  # tkinter may be unavailable in some environments
    HUD = None


STATE_COLORS = {
    "idle": (59, 130, 246),           # blue
    "wake": (236, 72, 153),           # pink: wake word just fired
    "listening": (34, 197, 94),       # bright green: your turn to talk
    "transcribing": (250, 204, 21),   # yellow: turning audio into text
    "thinking": (249, 115, 22),       # orange: model is working
    "synthesizing": (168, 85, 247),   # purple: rendering Orion's voice
    "speaking": (6, 182, 212),        # cyan: Orion is talking
    "muted": (107, 114, 128),         # gray
}

_WHITE = (255, 255, 255, 255)


def _draw_mic(draw, fill):
    """Draw a microphone glyph -- the shared base of the listening/muted icons."""
    draw.rounded_rectangle((26, 14, 38, 36), radius=6, fill=fill)
    draw.arc((20, 18, 44, 42), start=25, end=155, fill=fill, width=4)
    draw.line((32, 42, 32, 50), fill=fill, width=4)
    draw.line((24, 50, 40, 50), fill=fill, width=4)


_LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "images", "orion-logo-icon-bg.png")
try:
    _LOGO_BASE = Image.open(_LOGO_PATH).convert("RGBA").resize((256, 256), Image.LANCZOS)
except Exception:
    _LOGO_BASE = None


def _make_icon(state):
    """Tray icon: the Orion logo with a small state-coloured dot in the corner
    so you can still tell at a glance whether it's listening / thinking / etc."""
    color = STATE_COLORS.get(state, STATE_COLORS["idle"])
    if _LOGO_BASE is not None:
        img = _LOGO_BASE.copy()
        draw = ImageDraw.Draw(img)
        draw.ellipse((136, 136, 248, 248), fill=color + (255,), outline=_WHITE, width=10)
        return img
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((4, 4, 60, 60), radius=16, fill=color)

    if state == "listening":
        # Microphone -- your turn to speak.
        _draw_mic(draw, _WHITE)
    elif state == "transcribing":
        # Microphone + ellipsis: "got your audio, turning it into words".
        _draw_mic(draw, _WHITE)
        for cx in (44, 50, 56):
            draw.ellipse((cx - 2, 53, cx + 2, 57), fill=_WHITE)
    elif state == "muted":
        # Microphone with a slash -- voice off.
        _draw_mic(draw, _WHITE)
        draw.line((16, 16, 48, 50), fill=color, width=10)
        draw.line((16, 16, 48, 50), fill=_WHITE, width=4)
    elif state == "synthesizing":
        # Outlined sound bars -- preparing to speak (voice not playing yet).
        for cx, half in ((20, 8), (32, 18), (44, 11)):
            draw.rounded_rectangle((cx - 4, 32 - half, cx + 4, 32 + half),
                                   radius=4, outline=_WHITE, width=3)
    elif state == "speaking":
        # Sound bars -- Orion is talking.
        for cx, half in ((20, 8), (32, 18), (44, 11)):
            draw.rounded_rectangle((cx - 4, 32 - half, cx + 4, 32 + half),
                                   radius=4, fill=_WHITE)
    elif state == "thinking":
        # Three dots -- working on it.
        for cx in (20, 32, 44):
            draw.ellipse((cx - 5, 27, cx + 5, 37), fill=_WHITE)
    else:
        # idle / wake -- a calm ring.
        draw.ellipse((20, 20, 44, 44), outline=_WHITE, width=6)

    return img


class Orion:
    def __init__(self):
        self.speaker = Speaker()
        self.recognizer = sr.Recognizer()
        self.recognizer.energy_threshold = config.ENERGY_THRESHOLD
        self.recognizer.pause_threshold = config.PAUSE_THRESHOLD
        self.recognizer.non_speaking_duration = config.NON_SPEAKING_DURATION
        self.recognizer.dynamic_energy_threshold = True
        # Known-good speech/no-speech energy bar from the last ambient
        # calibration. dynamic_energy_threshold still adapts WITHIN a capture,
        # but it persists on the recognizer between turns and ratchets up after
        # loud audio (music, games, Orion's own voice) -- left unchecked it
        # inflates until the quiet start of the next command never crosses it
        # and gets swallowed. _listen resets to this baseline before each
        # capture so that drift can't carry over from one turn to the next.
        self._baseline_energy = config.ENERGY_THRESHOLD

        # Use the local Whisper model for speech-to-text when selected.
        # _transcribe falls back to Google automatically if Whisper can't
        # load or returns nothing, so this stays non-blocking at startup.
        self._stt_whisper = (config.STT_ENGINE == "whisper")

        memory.load()  # restore persistent facts about the user
        # Keep abandoned turns out of the conversation transcript: an answer
        # the user interrupted was never heard, so it must not become part of
        # what Orion remembers saying. See memory.set_write_gate.
        memory.set_write_gate(self._turn_alive)

        self._last_calibrate = 0.0  # monotonic time of the last ambient recalibration
        self.running = True
        self.state = "idle"
        self._barged_in = False  # set when the user interrupts a spoken reply

        # --- open-mic turn machinery (see ear.py and _converse) ---
        # The ear listens right through thinking and speaking, so a turn can
        # be abandoned half-way when the user adds to it or interrupts. A
        # turn that is abandoned may still be inside a model call that can't
        # be killed, so instead of stopping it we stop *listening* to it:
        # every turn carries a token, the live token is `_turn_token`, and
        # anything a stale turn tries to say or display is dropped. The token
        # lives in thread-local storage so calls from the monitor and the
        # reminder scheduler (which have no token) are never affected.
        # PyAudio is only created when the ear first starts, so constructing
        # it here costs nothing and can't trigger the device-enumeration
        # static described further down.
        self._ear = (ear_mod.ContinuousEar(on_speech_start=self._on_ear_onset)
                     if getattr(config, "OPEN_MIC", True) else None)
        self._turn_token = 0
        # Token of the last turn that carried out a local action (played
        # something, opened something, sent something). Once a turn has acted,
        # its command can no longer be merged into -- the world already
        # changed, so late speech has to start a new turn instead.
        self._acted_token = None
        self._turn_local = threading.local()
        self._loop_thread = None
        # Verified identity of whoever spoke the most recent phrase. "owner"
        # when speaker verification is off or no profile is enrolled (safe
        # single-user default); "other" only when we're confident otherwise.
        self._last_speaker = "owner"
        self._last_audio = None
        self._last_stt_ms = 0
        self._last_listen_ms = 0
        self._pending_reminder_when = None
        self._pending_reminder_task = None
        # Push-to-talk: a global hotkey can fire at any moment from a
        # background thread to request an immediate command capture. The
        # main loop and the wake-detector watchers check this event and
        # bail out of waiting so the listener can take over.
        self._ptt_event = threading.Event()
        self._ptt_registered = False
        # "Teach me a path" state for the smart app finder: set when Orion
        # couldn't find an app the user asked to open. The next utterance
        # from the owner is interpreted as a path, then we clear it.
        self._pending_app_teach = None
        # "Want me to summarise your unread email?" follow-up state, set by
        # the morning briefing. Captures the user's next yes/no so they
        # never get email content unless they ask for it.
        self._pending_email_offer = None
        # Set while waiting for the user to confirm a shutdown; see
        # _confirm_exit. Holds the monotonic time the offer expires.
        self._pending_exit = None
        self.hud = (HUD(start_hidden=True)
                    if (config.SHOW_HUD and HUD is not None) else None)
        self.tray = self._build_tray()

        # Proactive monitor: lets Orion speak up on its own about CPU load,
        # battery and weather. Started once the listen loop is running.
        self.monitor = monitor.ProactiveMonitor(self)

        # Local wake-word detector: fires the instant it hears the wake word.
        # If openwakeword isn't installed it stays None and Orion falls back
        # to phrase-based wake detection.
        # NOTE: deliberately NOT constructed here. WakeDetector.__init__ runs
        # `pyaudio.PyAudio()`, which enumerates every audio device on the
        # system. On Realtek/Conexant/Intel HD Audio chips that device
        # enumeration can produce 10-20 s of sustained "sand/static" noise
        # while the audio driver wakes up -- and at boot we'd be stacking it
        # on top of TWO other PyAudio inits (sr.Microphone() in _calibrate
        # and the player subprocess), which makes the noise much worse.
        # We instead build the wake detector LATER, in loop(), after the
        # startup greeting has fully played: that spaces the three audio-
        # subsystem initialisations several seconds apart, so the driver
        # has time to settle between each one and the static is dramatically
        # shorter (often gone entirely).
        self.wake = None

    # --- turn tokens ---
    def _begin_turn(self):
        """Retire whatever turn is in flight and start a new one.

        Returns the new turn's token. Any work still running under an older
        token keeps going (a model call can't be interrupted) but is now mute:
        `_turn_alive` will refuse every state change and every utterance it
        tries to make.
        """
        self._turn_token += 1
        return self._turn_token

    def _turn_alive(self):
        """True if the calling thread's turn is still the one being listened
        to. Threads with no turn of their own (the monitor, the reminder
        scheduler, the main loop) always pass."""
        token = getattr(self._turn_local, "token", None)
        return token is None or token == self._turn_token

    def _cancel_turn(self):
        """Abandon the in-flight turn and silence it immediately."""
        self._turn_token += 1
        if self.speaker.is_speaking():
            self.speaker.stop()
        if self._ear is not None:
            self._ear.set_playing(False)

    # --- status helpers ---
    def set_state(self, state):
        if not self._turn_alive():
            return
        self.state = state
        if self.hud is not None:
            self.hud.set_state(state)
        if self.tray is not None:
            self.tray.icon = _make_icon(state)
        if hub_server is not None:
            try:
                hub_server.publish("state", {"state": state})
            except Exception:
                pass

    def show_last(self, text):
        if self.hud is not None:
            self.hud.set_last(text)

    @staticmethod
    def _call_label(name, args):
        """'open(app="spotify")' -- how a tool call reads on the HUD chip."""
        parts = []
        for k, v in (args or {}).items():
            v = f'"{v}"' if isinstance(v, str) else str(v)
            parts.append(f"{k}={v}")
        return f"{name}({', '.join(parts)})"

    def show_tool_start(self, label):
        if self.hud is not None and getattr(config, "HUD_TOOL_CHIPS", True):
            self.hud.tool_start(label)

    def show_tool_done(self, label, ok=True):
        if self.hud is not None and getattr(config, "HUD_TOOL_CHIPS", True):
            self.hud.tool_done(label, ok)

    def show_heard(self, text):
        if self.hud is not None:
            self.hud.set_heard(text)
        if hub_server is not None and text:
            try:
                hub_server.publish("heard", {"text": text, "ts": time.time()})
            except Exception:
                pass

    def _speak(self, text):
        """Speak text while showing the SPEAKING state, then return to idle.

        Blocks until the whole utterance has finished playing, so callers
        never move on (and the mic never opens) while Orion is still talking.

        Splits timing into two telemetry stages:
          - synth: from queue-up to first audio sample (Piper rendering).
          - play:  from first audio to playback finished (Orion talking).

        Barge-in: while Orion is talking, the local wake model keeps
        listening. If it hears the wake word, the reply is cut off instantly
        and self._barged_in is set so the main loop captures the user's next
        command right away -- letting you interrupt and redirect mid-sentence.

        With the open mic running (config.OPEN_MIC) the ear covers that window
        instead, so no wake word is needed and the wake model is left alone --
        two capture streams on the same device at once is asking for trouble.
        The reply text is handed to the ear so its echo guard can recognise
        Orion's own voice coming back off the loudspeakers.
        """
        # A turn that has been superseded must not speak over its replacement.
        if not self._turn_alive():
            return
        self.set_state("synthesizing")
        done = threading.Event()
        started = threading.Event()
        _t_queue = time.monotonic()
        self.speaker.say(text, done=done, started=started)

        # Wait until the first sentence finishes synthesising and audio
        # actually starts playing. Bail out if `done` somehow beats `started`
        # (muted state, empty text).
        while not started.is_set() and not done.is_set() and self.running:
            started.wait(timeout=0.1)
        synth_ms = int((time.monotonic() - _t_queue) * 1000)
        telemetry.add("tts_synth_ms", synth_ms)

        _t_play = time.monotonic()
        self.set_state("speaking")

        # Orion's own voice is coming out of the speakers, so the presence
        # check must not read it as "the user is playing something".
        presence.suspend_audio_signal(True)
        ear = self._ear if (self._ear is not None and self._ear.running) else None
        if ear is not None:
            ear.set_playing(True, text)
        elif (config.BARGE_IN and self.wake is not None
                and threading.current_thread() is self._loop_thread):
            # Only the listen loop's own thread may drive the wake detector.
            # It owns a microphone stream and a model that are not safe to
            # use from two threads at once, and with the open mic running
            # this method is also called from turn workers.
            fired = self.wake.wait_for_wake(
                lambda: self.running and not done.is_set())
            if fired and not done.is_set():
                self.speaker.stop()
                self._barged_in = True

        try:
            # Never wait unconditionally. A turn that gets superseded while
            # this utterance is still queued would otherwise wait on a `done`
            # that nobody is left to set, hanging the worker forever and
            # leaving the ear convinced Orion is still talking -- which raises
            # its echo bar and makes Orion deaf from then on.
            while not done.wait(timeout=0.25):
                if not self.running:
                    break
                if not self._turn_alive():
                    self.speaker.stop()
                    break
        finally:
            presence.suspend_audio_signal(False)
            if ear is not None:
                ear.set_playing(False)
        play_ms = int((time.monotonic() - _t_play) * 1000)
        telemetry.add("tts_play_ms", play_ms)
        if self.state == "speaking":
            self.set_state("idle")

    # --- system tray ---
    def _build_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem("Show Orion", self._tray_show_hud, default=True),
            pystray.MenuItem("Hide Orion", self._tray_hide_hud),
            pystray.MenuItem(
                lambda item: "Unmute" if self.speaker.muted else "Mute",
                self._toggle_mute,
            ),
            pystray.MenuItem("New conversation", self._tray_clear),
            pystray.MenuItem("Open Hub", self._tray_open_hub),
            pystray.MenuItem("Restart Orion", self._tray_restart),
            pystray.MenuItem("Exit", self._tray_exit),
        )
        return pystray.Icon("Orion", _make_icon("idle"), "Orion", menu)

    def _tray_show_hud(self, *_):
        if self.hud is not None:
            self.hud.show()

    def _tray_hide_hud(self, *_):
        if self.hud is not None:
            self.hud.hide()

    def _toggle_mute(self, *_):
        self.speaker.set_muted(not self.speaker.muted)
        if self.speaker.muted:
            self.set_state("muted")
        else:
            self.set_state("idle")

    def _tray_clear(self, *_):
        ai.clear_chat()

    def _tray_open_hub(self, *_):
        if hub_launcher is not None:
            hub_launcher.open_hub()

    def _tray_restart(self, *_):
        # Run off the tray callback thread so pystray isn't blocked by the
        # shutdown sequence (end-of-session summarisation can take seconds).
        threading.Thread(target=self.restart, daemon=True).start()

    def _tray_exit(self, *_):
        self.shutdown(reason="tray menu: Exit")

    # --- speech ---
    def _listen(self, timeout=None, phrase_limit=None, source=None):
        """Capture one phrase from the microphone; return lowercase text or ''.

        `source` may be an already-open sr.Microphone whose audio device is
        already running (see _wake_local) -- reusing it avoids the cold-start
        gap where a command spoken in the same breath as the wake word loses
        its first syllable to a freshly-opened stream still spinning up. When
        None, a fresh microphone is opened for this capture as before.
        """
        _listen_t0 = time.monotonic()
        # Undo any cross-turn dynamic-energy drift before listening (see the
        # _baseline_energy note in __init__).
        self.recognizer.energy_threshold = self._baseline_energy
        try:
            if source is not None:
                audio = self.recognizer.listen(
                    source,
                    timeout=timeout,
                    phrase_time_limit=phrase_limit or config.PHRASE_TIME_LIMIT,
                )
            else:
                with sr.Microphone() as src:
                    audio = self.recognizer.listen(
                        src,
                        timeout=timeout,
                        phrase_time_limit=phrase_limit or config.PHRASE_TIME_LIMIT,
                    )
        except sr.WaitTimeoutError:
            return ""
        except OSError:
            return ""
        # recognizer.listen blocks while recording and returns once it detects
        # end-of-speech, so the elapsed up to here is pure mic-capture time.
        self._last_listen_ms = int((time.monotonic() - _listen_t0) * 1000)
        self._last_audio = audio
        # Flip the tray icon so the user sees that the gap between "stopped
        # speaking" and "Orion thinking" is Whisper transcribing, not Orion
        # being frozen. Restored to a steady state by the next set_state call.
        _prev_state = self.state
        self.set_state("transcribing")
        _stt_t0 = time.monotonic()
        text = self._transcribe(audio)
        self._last_stt_ms = int((time.monotonic() - _stt_t0) * 1000)
        # If nothing else has changed the state in the meantime, decide what
        # to show next. With text we'll be processing the command, so move
        # straight to "thinking" -- otherwise restore the pre-transcribe state
        # (typically "listening" or "wake"). This avoids a misleading flash
        # back to listening-green while the main loop spins up the handler.
        if self.state == "transcribing":
            self.set_state("thinking" if text else _prev_state)
        replay_debug.record(audio, text, source="listen")
        if text:
            # Hearing you speak is the one presence signal Windows knows
            # nothing about, and the most conclusive one there is.
            presence.note_voice()
        # Identify the speaker once per captured phrase so every downstream
        # gate (lock-mode + the memory privacy gate in build_prompt) reads
        # the same answer. Falls back to "owner" if anything fails.
        if text and getattr(config, "VOICE_VERIFICATION", False):
            try:
                self._last_speaker = voice_id.identify(audio)
            except Exception:
                self._last_speaker = "owner"
        else:
            self._last_speaker = "owner"
        return text

    def _transcribe(self, audio):
        """Turn a captured phrase into lowercase text.

        Prefers the local Whisper model; if it produces nothing (or isn't
        in use) it falls back to Google's web speech API.
        """
        if self._stt_whisper:
            text = transcribe.transcribe(audio)
            if text:
                return text.strip()
        try:
            text = self.recognizer.recognize_google(
                audio, language=config.LANGUAGE)
            return text.lower().strip()
        except (sr.UnknownValueError, sr.RequestError):
            return ""

    def _calibrate(self):
        try:
            with sr.Microphone() as source:
                self.recognizer.adjust_for_ambient_noise(source, duration=1.0)
            # Remember the freshly-measured floor as the per-capture baseline so
            # _listen can reset to it and undo any cross-turn dynamic drift.
            self._baseline_energy = self.recognizer.energy_threshold
        except OSError:
            pass

    # --- command handling ---
    def _receive_email_offer_reply(self, query):
        """Handle the user's answer to the briefing's "summarise email?" offer.

        Returns True if the reply was consumed (acted on or declined). Times
        out silently after 60 seconds so a stale offer doesn't intercept
        normal commands hours later.
        """
        pending = self._pending_email_offer
        if not pending:
            return False
        if pending.get("expires", 0) < time.monotonic():
            self._pending_email_offer = None
            return False

        low = query.lower().strip()
        yes_words = ("yes", "yeah", "yep", "sure", "please", "go ahead",
                     "do it", "summarise", "summarize", "ok", "okay",
                     "alright", "all right")
        no_words = ("no", "nope", "nah", "not now", "skip", "later",
                    "never mind", "don't", "dont")
        is_yes = any(w in low.split() or low.startswith(w + " ") or low == w
                     for w in yes_words)
        is_no = any(w in low.split() or low.startswith(w + " ") or low == w
                    for w in no_words)
        # "no" trumps "yes" when both are present ("no thanks").
        if is_no:
            self._pending_email_offer = None
            self._speak("As you wish, Sir.")
            return True
        if is_yes:
            self._pending_email_offer = None
            self.set_state("thinking")
            try:
                import tools
                reply = tools._summarize_email({}, self) or ""
            except Exception:
                reply = ""
            if reply:
                memory.add_orion(reply)
                self.show_last(reply)
                self._speak(reply)
            self.set_state("idle")
            return True
        # Anything else: drop the offer, let the utterance go through as a
        # normal command. No badgering.
        self._pending_email_offer = None
        return False

    def _voice_gate(self, query):
        """Decide whether the just-heard `query` may proceed.

        Returns one of:
          "pass"    -- normal dispatch may continue
          "handled" -- this was a lock/unlock toggle; nothing else to do
          "deny"    -- locked mode and the speaker isn't the owner; drop the
                       command silently (no reply, no leak that we even heard
                       them)
        """
        if not getattr(config, "VOICE_VERIFICATION", False):
            return "pass"
        speaker = getattr(self, "_last_speaker", "owner")

        if voice_id.is_lock_phrase(query):
            if speaker == "owner":
                voice_id.set_mode("locked")
                self._speak("Locked, Sir. I will only listen to you.")
            # A stranger trying to lock Orion is ignored without acknowledgement.
            return "handled"
        if voice_id.is_unlock_phrase(query):
            if speaker == "owner":
                voice_id.set_mode("open")
                self._speak("Opened. Anyone may speak with me.")
            return "handled"

        if voice_id.get_mode() == "locked" and speaker != "owner":
            return "deny"
        return "pass"

    def _confirm_exit(self):
        """Ask before shutting down, instead of just doing it.

        The exit intent fires on words as ordinary as "bye", "close" and
        "turn off", and shutting down is the one action with no way back: the
        process is gone, so there is nothing left to say "actually, no" to.
        With the microphone open across a whole turn there is more speech
        reaching the matcher than there used to be, so a single misheard
        phrase should not be able to end the session. Set
        config.CONFIRM_EXIT = False to shut down on the first word again.
        """
        if not getattr(config, "CONFIRM_EXIT", True):
            self.speaker.say_blocking("Yes sir, shutting down. Goodbye.")
            self.shutdown(reason="voice command: exit intent")
            return
        self._pending_exit = time.monotonic() + float(
            getattr(config, "CONFIRM_EXIT_WINDOW", 20))
        self._speak("Shut down? Say yes to confirm, Sir.")

    def _receive_exit_confirmation(self, query):
        """Consume the user's answer to "shut down?".

        Returns True if the answer was consumed. Anything that isn't a clear
        yes cancels the shutdown and falls through, so the phrase is still
        treated as an ordinary command rather than being swallowed.
        """
        expires = self._pending_exit
        self._pending_exit = None
        if expires is None or expires < time.monotonic():
            return False
        low = query.lower().strip().strip(".!?")
        yes_words = {"yes", "yeah", "yep", "yes please", "confirm", "do it",
                     "go ahead", "affirmative", "shut down", "shutdown",
                     "correct"}
        if low in yes_words:
            self.speaker.say_blocking("Yes sir, shutting down. Goodbye.")
            self.shutdown(reason=f"voice command: exit confirmed ({query!r})")
            return True
        no_words = {"no", "nope", "nah", "cancel", "never mind", "nevermind",
                    "stop", "don't", "dont"}
        if low in no_words:
            self._speak("Staying put, Sir.")
            return True
        return False

    def _handle_command(self, query):
        """Act on the text after the wake word using Orion's local intent brain.

        Returns True if it was a built-in command (already handled), so it
        understands natural phrasings like "can you mute yourself right now".
        """
        gate = self._voice_gate(query)
        if gate == "handled" or gate == "deny":
            return True

        # If we just asked the user where an app lives, this turn IS the
        # answer -- intercept before any other dispatch. Owner-only.
        if (self._pending_app_teach
                and getattr(self, "_last_speaker", "owner") == "owner"):
            import browser_control
            if browser_control.receive_taught_path(self, query):
                return True

        # Response to the morning email offer ("yes please" / "no thanks").
        if (self._pending_email_offer
                and getattr(self, "_last_speaker", "owner") == "owner"):
            if self._receive_email_offer_reply(query):
                return True

        # Answer to "shut down?" -- see _confirm_exit.
        if self._pending_exit is not None:
            if self._receive_exit_confirmation(query):
                return True

        # Memory commands are handled before intent matching so the fact text
        # ("remember that I take my coffee black") survives filler-stripping.
        if self._handle_memory_command(query):
            return True

        # AI-first routing: the model hears nearly everything and picks the
        # tool from what was actually meant. Only instant controls stay local.
        if getattr(config, "AI_FIRST", True):
            return self._handle_quick_command(query)

        # Song recognition is routed straight to the recognizer here -- before
        # web-search detection -- so phrasings like "what's playing right now"
        # reach it instead of being misrouted to web search (which would just
        # have the AI say it can't access the audio).
        if brain.is_song_query(query):
            telemetry.set_category("fast.song")
            self._identify_song()
            return True

        if brain.is_health_query(query):
            self._fast_health_check()
            return True

        # Self-knowledge fast-path. "What model are you?", "what can
        # you do?", etc. -- answered locally so Flash-Lite can't hallucinate
        # a different stack.
        self_topic = brain.self_query_match(query)
        if self_topic is not None:
            self._fast_describe_self(self_topic)
            return True

        if (brain.hub_open_match(query)
                and getattr(self, "_last_speaker", "owner") == "owner"):
            telemetry.set_category("fast.hub_open")
            self._fast_open_hub()
            return True

        # If we asked the user for the missing half of a reminder last turn,
        # this utterance IS the answer -- pair it with the stashed half and
        # commit. Done BEFORE the regular reminder matchers AND before
        # anything that could route the answer to the model.
        if getattr(self, "_pending_reminder_when", None):
            when = self._pending_reminder_when
            self._pending_reminder_when = None
            answer = self._strip_reminder_answer_prefix(query)
            self._fast_set_reminder(answer, when)
            return True
        if getattr(self, "_pending_reminder_task", None):
            task = self._pending_reminder_task
            self._pending_reminder_task = None
            answer = self._strip_reminder_answer_prefix(query)
            self._fast_set_reminder(task, answer)
            return True

        # End-of-day nudge skip -- "skip tonight's nudge".
        if brain.eod_skip_match(query):
            self._skip_eod_nudge()
            return True

        # Todo-list fast-path -- "what's on my todo list" specifically asks
        # for todos, NOT the merged agenda (events + reminders + todos).
        # Must run before agenda_query_match because "what's on" matches both.
        if brain.is_todo_query(query):
            self._fast_todos()
            return True

        # Agenda query fast-path -- "what's on for today/tomorrow/this week".
        agenda_when = brain.agenda_query_match(query)
        if agenda_when is not None:
            self._fast_agenda(agenda_when)
            return True

        # "Remind me ..." -- handle locally so the model can't mis-split.
        rset = brain.reminder_set_match(query)
        if rset is not None:
            task, when = rset
            self._fast_set_reminder(task, when)
            return True

        # Reminder management fast-paths (owner-only).
        if brain.reminder_list_match(query):
            self._fast_reminder("list")
            return True
        if brain.reminder_clear_match(query):
            self._fast_reminder("clear")
            return True
        target = brain.reminder_cancel_match(query)
        if target is not None:
            self._fast_reminder("cancel", target)
            return True
        snooze_mins = brain.reminder_snooze_match(query)
        if snooze_mins is not None:
            self._fast_reminder("snooze", snooze_mins)
            return True

        # The time/date is on the PC clock -- answer it locally. Routed before
        # web-search detection so "what's the date today" isn't misrouted to
        # web search by the "today" hint word.
        if brain.is_time_query(query):
            telemetry.set_category("fast.time")
            self._tell_time(query)
            return True

        # Fast-paths for tools the model sometimes refuses or hallucinates
        # ("hey can you check my work email" -> Flash claims it can't read
        # email, even with the tool right in front of it). When the
        # phrasing is unambiguous, skip the model entirely and call the tool.
        acc = brain.email_check_match(query)
        if acc is not None:
            self._fast_check_email(acc)
            return True
        acc = brain.email_summary_match(query)
        if acc is not None:
            self._fast_summarize_email(acc)
            return True
        music_q = brain.music_play_match(query)
        if music_q is not None:
            self._fast_play_music(music_q)
            return True

        # Multi-action requests ("play music and then open youtube") go
        # straight to the AI: it can return several tool calls and run them
        # all, while the single-shot dispatcher below only ever runs the
        # first action it matches and drops the rest.
        if commands.has_multiple_actions(query):
            self._ask_ai(query)
            return True

        # Group 6-11 commands carry an argument ("open spotify", "work mode",
        # "set a timer for 5 minutes"). Checked before intent matching.
        if commands.dispatch(query, self):
            telemetry.set_category("command.dispatch")
            self.set_state("idle")
            return True

        intent, confidence = brain.classify_intent_scored(query)
        if intent is None:
            return False

        # High confidence: act right away.
        if confidence >= config.SEMANTIC_HIGH:
            self._run_intent(intent)
            return True

        # Middling confidence: Orion has a guess but isn't sure. Rather than
        # interrupt with a "did you want me to ...?" clarifier, hand the query
        # straight to the AI -- it can answer the question or pick the right
        # tool itself, which is far less jarring than guessing at a prompt.
        if confidence >= config.SEMANTIC_CLARIFY:
            return False  # -> _ask_ai

        # Low confidence: not a command.
        return False

    # Built-in intents that stay local under AI_FIRST: instant voice controls,
    # plus the code-assistant commands, which have no model tool.
    _QUICK_INTENTS = {"stop", "mute", "unmute"}

    def _handle_quick_command(self, query):
        """AI_FIRST routing. Returns True only for instant controls:
        stop / mute / unmute, media pause/resume/skip/previous, volume, HUD
        show/hide, plus two phrases with no model tool (open the hub, skip
        tonight's nudge). Everything else returns False -> _ask_ai, where the
        model reads the whole request and picks the right tool -- the local
        phrase lists used to grab half-heard sentences and run the wrong one.
        """
        if (brain.hub_open_match(query)
                and getattr(self, "_last_speaker", "owner") == "owner"):
            telemetry.set_category("fast.hub_open")
            self._fast_open_hub()
            return True
        if brain.eod_skip_match(query):
            self._skip_eod_nudge()
            return True
        if commands.quick_dispatch(query, self):
            telemetry.set_category("command.quick")
            self.set_state("idle")
            return True
        # Exact phrases only for stop / mute / unmute: fuzzy matching turned
        # "stop the timer" and "cancel my reminder" into "stop talking".
        intent = brain.exact_intent(query, self._QUICK_INTENTS)
        if intent is None:
            # Code-assistant commands have no model tool, so they keep the
            # difflib matcher (still no semantic guessing).
            intent = brain.classify_intent(query)
            if intent not in code_assistant.INSTRUCTIONS:
                intent = None
        if intent is not None:
            self._run_intent(intent)
            return True
        return False

    def _skip_eod_nudge(self):
        self.monitor.skip_tonight()
        reply = "No nudge tonight, Sir."
        memory.add_user("[fast-path] skip eod",
                        speaker=getattr(self, "_last_speaker", "owner"))
        memory.add_orion(reply)
        self.show_last(reply)
        self._speak(reply)

    def confirm_action(self, description):
        """Ask the user to approve a destructive action before running it.

        Spoken before Orion deletes data or closes apps/windows, so a
        misheard command can't wipe things or close the user's apps without
        an explicit yes. Returns True only on a clear affirmative.
        """
        self.speaker.say_blocking(
            f"Did you want me to {description}? Say yes to confirm.")
        self.set_state("listening")
        reply = self._listen(timeout=config.FOLLOWUP_TIMEOUT)
        if not reply:
            return False
        low = reply.lower().strip()
        return any(low == w or low.startswith(w + " ")
                   for w in ("yes", "yeah", "yep", "yup", "sure", "correct",
                             "right", "do it", "go ahead", "ok", "okay",
                             "please do", "affirmative", "confirm"))

    def _run_intent(self, intent):
        """Carry out a recognized built-in intent."""
        telemetry.set_category(f"intent.{intent}")
        if intent == "stop":
            self.speaker.stop()
        elif intent == "mute":
            self.speaker.set_muted(True)
            self.set_state("muted")
        elif intent == "unmute":
            self.speaker.set_muted(False)
            self.set_state("idle")
            self.speaker.say("Voice on.")
        elif intent == "clear":
            ai.clear_chat()
            memory.clear_session()
            self.speaker.say("Started a new conversation.")
        elif intent == "exit":
            self._confirm_exit()
        elif intent == "explain":
            self._explain_screen()
        elif intent == "read":
            self._read_screen()
        elif intent == "summarize":
            self._ocr_to_ai(
                "Summarize the following text from my screen. Keep it brief "
                "and to the point."
            )
        elif intent == "translate":
            self._ocr_to_ai(
                f"Translate the following text into "
                f"{config.TRANSLATE_TARGET_LANGUAGE}. Give just the "
                f"translation. If it is already in that language, say so."
            )
        elif intent == "screenshot":
            self._take_screenshot()
        elif intent in code_assistant.INSTRUCTIONS:
            self._code_assist(intent)

    def _handle_memory_command(self, query):
        """Handle 'remember ...' and 'forget ...' voice commands.

        Returns True if the phrase was a memory command.
        """
        low = query.lower().strip()

        # Only the owner can write to or wipe persistent memory. A stranger
        # saying "remember X" or "forget everything" is consumed silently
        # without doing anything -- their facts don't enter your memory.json.
        if getattr(self, "_last_speaker", "owner") == "other" and (
                low.startswith("forget")
                or low.startswith("remember that ")
                or low.startswith("remember ")):
            return True

        if low.startswith("forget"):
            if any(w in low for w in ("everything", "all", "what you know",
                                      "about me")):
                memory.forget_all()
                self.speaker.say("Forgotten. I no longer remember anything "
                                 "about you.")
                return True
            return False

        for lead in ("remember that ", "remember "):
            if low.startswith(lead):
                fact = query.strip()[len(lead):].strip()
                if fact:
                    if memory.remember(fact):
                        self.speaker.say("Got it. I'll remember that.")
                    else:
                        self.speaker.say("I already had that noted.")
                    return True
        return False

    def _fast_check_email(self, account):
        """Local fast-path for 'any new email' phrasings. Bypasses the model."""
        telemetry.set_category("fast.email_check")
        self.set_state("thinking")
        self._say_filler("tool")
        try:
            import tools
            args = {"account": account} if account else {}
            reply = tools._check_email(args, self) or ""
        except Exception:
            reply = "I couldn't reach your email, Sir."
        memory.add_user(query_text := f"[fast-path] check email"
                        f"{' on ' + account if account else ''}",
                        speaker=getattr(self, "_last_speaker", "owner"))
        if reply:
            memory.add_orion(reply)
            self.show_last(reply)
            self._speak(reply)
        self.set_state("idle")

    def _fast_summarize_email(self, account):
        """Local fast-path for 'summarise my email' phrasings."""
        telemetry.set_category("fast.email_summary")
        self.set_state("thinking")
        self._say_filler("tool")
        try:
            import tools
            args = {"account": account} if account else {}
            reply = tools._summarize_email(args, self) or ""
        except Exception:
            reply = "I couldn't read your email just now, Sir."
        memory.add_user(f"[fast-path] summarize email"
                        f"{' on ' + account if account else ''}",
                        speaker=getattr(self, "_last_speaker", "owner"))
        if reply:
            memory.add_orion(reply)
            self.show_last(reply)
            self._speak(reply)
        self.set_state("idle")

    _ANSWER_PREFIX_RE = re.compile(
        r"^(?:to\s+|that\s+|about\s+|the\s+answer\s+is\s+|"
        r"its\s+|it'?s\s+|remind\s+me\s+(?:to\s+|about\s+|that\s+)?)",
        re.IGNORECASE)

    def _strip_reminder_answer_prefix(self, text):
        """Clean up the user's follow-up answer ('to watch the launch' ->
        'watch the launch', 'remind me about dinner' -> 'dinner')."""
        if not text:
            return ""
        cleaned = self._ANSWER_PREFIX_RE.sub("", text.strip()).strip()
        return cleaned or text.strip()

    def _fast_todos(self):
        """Local fast-path for 'what's on my todo list' / 'read my todos'."""
        if getattr(self, "_last_speaker", "owner") != "owner":
            return
        telemetry.set_category("fast.todos")
        self.set_state("thinking")
        try:
            import agenda
            items = agenda.todos_list()
            if not items:
                reply = "Your to-do list is empty, Sir."
            else:
                n = len(items)
                word = "item" if n == 1 else "items"
                listed = "; ".join(f"{i}, {t}"
                                   for i, t in enumerate(items, 1))
                reply = f"You have {n} {word} on your to-do list: {listed}."
        except Exception:
            reply = "I couldn't reach your to-do list, Sir."
        memory.add_user("[fast-path] read todos",
                        speaker=getattr(self, "_last_speaker", "owner"))
        memory.add_orion(reply)
        self.show_last(reply)
        self._speak(reply)
        self.set_state("idle")

    def _fast_agenda(self, when):
        """Local fast-path for 'what's on for today/tomorrow/this week'."""
        if getattr(self, "_last_speaker", "owner") != "owner":
            return
        telemetry.set_category("fast.agenda")
        self.set_state("thinking")
        try:
            import agenda
            reply = agenda.spoken_summary(when)
        except Exception:
            reply = "I couldn't reach your agenda, Sir."
        memory.add_user(f"[fast-path] agenda {when}",
                        speaker=getattr(self, "_last_speaker", "owner"))
        memory.add_orion(reply)
        self.show_last(reply)
        self._speak(reply)
        self.set_state("idle")

    def _fast_set_reminder(self, task, when):
        """Local fast-path for 'remind me ...'. Bypasses model extraction
        (which kept misclassifying time words as the task)."""
        if getattr(self, "_last_speaker", "owner") != "owner":
            return
        telemetry.set_category("fast.reminder_set")
        self.set_state("thinking")
        import reminders as r
        task = (task or "").strip()
        when = (when or "").strip()
        # Missing pieces -> ask, and stash the half we DO have so the next
        # utterance can just be the answer.
        if when and not task:
            self._pending_reminder_when = when
            reply = "What should I remind you of, Sir?"
        elif task and not when:
            self._pending_reminder_task = task
            reply = "When should I remind you, Sir?"
        elif not task and not when:
            reply = "What should I remind you of, and when, Sir?"
        else:
            reply, ok = r.add(task, when)
            # If add() rejected for a missing/garbled half, keep the good
            # half stashed so the follow-up commits cleanly.
            if not ok:
                low = reply.lower()
                if "what to remind you" in low:
                    self._pending_reminder_when = when
                elif "couldn't work out when" in low or "past" in low:
                    self._pending_reminder_task = task
        memory.add_user(f"[fast-path] set reminder",
                        speaker=getattr(self, "_last_speaker", "owner"))
        memory.add_orion(reply)
        self.show_last(reply)
        self._speak(reply)
        self.set_state("idle")

    def _fast_reminder(self, action, arg=None):
        """Local fast-path for reminder list/cancel/clear/snooze. Owner-only."""
        if getattr(self, "_last_speaker", "owner") != "owner":
            return
        telemetry.set_category(f"fast.reminder_{action}")
        self.set_state("thinking")
        try:
            import reminders as r
            if action == "list":
                reply = r.spoken_list()
            elif action == "clear":
                reply = r.clear_all()
            elif action == "cancel":
                reply = r.cancel_matching(arg or "")
            elif action == "snooze":
                reply = r.snooze(int(arg or 10))
            else:
                reply = ""
        except Exception:
            reply = "I couldn't reach your reminders, Sir."
        memory.add_user(f"[fast-path] reminder {action}",
                        speaker=getattr(self, "_last_speaker", "owner"))
        if reply:
            memory.add_orion(reply)
            self.show_last(reply)
            self._speak(reply)
        self.set_state("idle")

    def _fast_health_check(self):
        """Local fast-path for 'system check' phrasings. Bypasses the model."""
        telemetry.set_category("fast.health")
        self.set_state("thinking")
        self._say_filler("tool")
        try:
            import health_check
            reply = health_check.run_health_check()
        except Exception:
            reply = "I couldn't complete the system check, Sir."
        memory.add_user("[fast-path] system check",
                        speaker=getattr(self, "_last_speaker", "owner"))
        memory.add_orion(reply)
        self.show_last(reply)
        self._speak(reply)
        self.set_state("idle")

    def _fast_open_hub(self):
        """Owner-gated voice command to open the Universal Hub window."""
        self.set_state("thinking")
        ok = False
        if hub_launcher is not None:
            ok = hub_launcher.open_hub()
        reply = ("Opening the hub, Sir." if ok
                 else "The hub isn't available right now, Sir.")
        memory.add_user("[fast-path] open hub",
                        speaker=getattr(self, "_last_speaker", "owner"))
        memory.add_orion(reply)
        self.show_last(reply)
        self._speak(reply)
        self.set_state("idle")

    def _fast_describe_self(self, topic):
        """Local fast-path for self-knowledge questions. Bypasses the model."""
        telemetry.set_category(f"fast.self:{topic}")
        self.set_state("thinking")
        try:
            import about
            reply = (about.full_report() if topic == "general"
                     else about.describe(topic))
        except Exception:
            reply = "I couldn't pull my self-description, Sir."
        memory.add_user(f"[fast-path] self:{topic}",
                        speaker=getattr(self, "_last_speaker", "owner"))
        memory.add_orion(reply)
        self.show_last(reply)
        self._speak(reply)
        self.set_state("idle")

    def _fast_play_music(self, query):
        """Local fast-path for 'play music' phrasings. Bypasses the model."""
        telemetry.set_category("fast.music")
        self.set_state("thinking")
        self._say_filler("tool")
        try:
            import youtube_player
            reply = youtube_player.play(query) or ""
        except Exception:
            reply = "I couldn't reach YouTube, Sir."
        memory.add_user(f"[fast-path] play {query or 'music'}",
                        speaker=getattr(self, "_last_speaker", "owner"))
        if reply:
            memory.add_orion(reply)
            self.show_last(reply)
            self._speak(reply)
        self.set_state("idle")

    def _say_filler(self, category="think"):
        """Speak a short, natural filler matching the kind of request.

        `category` is one of "search" / "tool" / "think" (see
        brain.filler_category). Plays a pre-rendered WAV when available
        (synthesised once at startup) so the filler is INSTANT -- no synth
        delay between the user finishing their question and Orion's
        acknowledgement.
        """
        if not config.THINKING_FILLERS:
            return
        if not self._turn_alive():
            return  # this turn has been superseded; stay quiet
        if self.speaker.say_filler(category):
            return
        # No pre-rendered fillers (e.g. pyttsx3 fallback): synth on the fly.
        phrases = getattr(config, "THINKING_FILLER_PHRASES", None)
        if not phrases:
            return
        if isinstance(phrases, dict):
            bucket = (phrases.get(category)
                      or phrases.get("think") or [])
            if not bucket:
                for v in phrases.values():
                    if v:
                        bucket = v
                        break
        else:
            bucket = phrases
        if bucket:
            self.speaker.say(random.choice(bucket))

    def _startup_greeting(self):
        """Build Orion's spoken greeting for startup.

        With remembered facts and a reachable AI, the greeting is personal --
        time of day plus anything Orion remembers that's relevant right now.
        Otherwise it falls back to a plain time-of-day greeting that always
        works, even offline.
        """
        fallback = f"{memory.time_of_day_greeting()} Orion online and ready."
        if not (config.AUTO_MEMORY and memory.facts()
                and hasattr(ai, "ask_once")):
            return fallback
        try:
            greeting = ai.ask_once(memory.build_greeting_prompt())
        except Exception:
            greeting = ""
        return greeting or fallback

    def _learn_from(self, user_text):
        """Quietly grow Orion's persistent memory from what the user said.

        Runs the fact extraction on a background thread so it never delays
        the reply. Any durable facts the AI finds are saved to memory.json.
        """
        if not (config.AUTO_MEMORY and hasattr(ai, "ask_once")):
            return
        # Never grow persistent memory from a stranger's voice -- the facts
        # file is about the owner only.
        if getattr(self, "_last_speaker", "owner") == "other":
            return

        def worker():
            try:
                reply = ai.ask_once(memory.build_extraction_prompt(user_text))
            except Exception:
                return
            for line in reply.splitlines():
                fact = line.strip().lstrip("-*").strip()
                if fact and fact.rstrip(".!").upper() != "NONE":
                    memory.remember(fact)

        threading.Thread(target=worker, daemon=True).start()

    def _ask_ai(self, query):
        """Send a query to the AI and speak the response."""
        telemetry.set_category("ai.text")
        self.set_state("thinking")

        # Let Orion's memory grow on its own from this turn.
        self._learn_from(query)

        # Build the message with persistent facts + session transcript.
        # Real-time questions get routed through the AI's web search tool.
        web_search = config.WEB_SEARCH and brain.wants_web_search(
            query, tools_first=getattr(config, "AI_FIRST", True))

        # Pick a filler that matches the kind of request (web lookup,
        # imperative command, or open question) so the spoken
        # acknowledgement sounds right.
        self._say_filler(brain.filler_category(query, web_search=web_search))

        # Function-calling path: let the model pick a tool from the request
        # itself instead of relying on hand-written phrase lists. Skipped for
        # real-time questions, which need web-search grounding instead.
        if (config.FUNCTION_CALLING and not web_search
                and ai.supports_tools()):
            self._ask_ai_tools(query)
            return

        speaker = getattr(self, "_last_speaker", "owner")
        prompt = memory.build_prompt(query, web_search=web_search, speaker=speaker)
        if not ai.send_query(prompt):
            self.speaker.say("I couldn't reach my brain. Check the API key "
                             "for my model in the config.")
            self.set_state("idle")
            return
        response = ai.read_response(query)
        memory.add_user(query, speaker=speaker)
        if response:
            memory.add_orion(response)
            self.show_last(response)
            self._speak(response)
        else:
            self._speak("Sorry, I didn't get an answer back.")
        self.set_state("idle")

    def _ask_ai_tools(self, query):
        """Send `query` to the model with Orion's tools attached and run
        whatever it asks for, feeding each result back so it can take
        another step.

        A round of pure ACTION tools ends the turn on the spot -- their own
        spoken replies are already the answer, so "lights on" still costs
        exactly one API call. Orion only goes back to the model when a QUERY
        tool ran (tools.is_query_tool) and its result is something the model
        has to read before it can finish: look up the reminders, then cancel
        the two the user meant.
        """
        speaker = getattr(self, "_last_speaker", "owner")
        prompt = memory.build_prompt(query, speaker=speaker, with_tools=True)
        memory.add_user(query, speaker=speaker)

        decls = tools.declarations()
        history = [{"role": "user", "content": prompt}]
        max_steps = (getattr(config, "MAX_TOOL_STEPS", 4)
                     if getattr(config, "AGENTIC_TOOLS", True) else 1)
        spoken = []      # action-tool replies worth saying out loud
        lookups = []     # query-tool results, kept only as a fallback
        ran = []         # every tool name run this turn, for telemetry
        answer = ""      # the model's own closing words, if it wrote any

        for _step in range(max_steps):
            kind, payload = ai.send_with_tools(prompt, decls, history=history)

            if kind == "error":
                # Whatever already ran still gets reported -- going silent
                # after cancelling one of two reminders is the worst
                # outcome, since the user can't tell what landed.
                if not (spoken or lookups):
                    self._speak("I couldn't reach my brain. Check the API "
                                "key for my model in the config.")
                    self.set_state("idle")
                    return
                break

            if kind == "text":
                answer = payload or ""
                break

            # kind == "calls": run them in the order the model asked for.
            history.append({"role": "assistant", "content": "",
                            "calls": payload})
            # One spoken yes covers every call to the same destructive tool
            # in this round: "cancel both reminders" is two cancel_reminder
            # calls, and asking twice about one sentence is noise.
            approved, refused = tools.confirm_batch(payload, self)
            for name in refused:
                spoken.append("Okay, I'll leave it.")

            _action_t0 = time.monotonic()
            revisit = False
            for call in payload:
                name, args = call["name"], call.get("args") or {}
                if name in refused:
                    # Tell the model too, or it reports success it never had.
                    history.append({"role": "tool", "id": call["id"],
                                    "name": name,
                                    "content": "The user declined this."})
                    continue
                ran.append(name)
                label = self._call_label(name, args)
                self.show_tool_start(label)
                try:
                    result = tools.run_call(name, args, self, approved=approved)
                except Exception:
                    self.show_tool_done(label, ok=False)
                    raise
                self.show_tool_done(label, ok=True)
                if tools.is_query_tool(name):
                    # A lookup's own words are not the answer -- the model
                    # is about to use them to write one. Speaking them too
                    # would read the whole reminder list back first.
                    revisit = True
                    if result:
                        lookups.append(result)
                elif result:
                    spoken.append(result)
                # run_call returns None when the handler spoke for itself.
                # The model still needs to know the call went through, or it
                # reads the gap as a failure and tries again.
                history.append({"role": "tool", "id": call["id"],
                                "name": name,
                                "content": result or "Done."})
            telemetry.add("action_ms",
                          int((time.monotonic() - _action_t0) * 1000))

            if not revisit:
                break
        else:
            # Ran out of steps with the model still asking for tools.
            if not (spoken or lookups):
                spoken.append("I got partway through that, Sir, but it "
                              "needed more steps than I allow in one go.")

        if ran:
            telemetry.set_tool(ran[0])
            telemetry.set_category("ai.tool:" + ",".join(ran))

        # The model's closing text wins when it wrote any -- after a lookup
        # it is answering the actual question ("your next one is at 10").
        # Failing that, the tools' own replies are already well-phrased for
        # speech. `lookups` is the last resort: a query ran but the model
        # never summarised it, so read the raw list rather than say nothing.
        reply = answer or " ".join(spoken) or " ".join(lookups)
        if reply:
            memory.add_orion(reply)
            self.show_last(reply)
            self._speak(reply)
        elif not ran:
            # Nothing ran and nothing was said. Tools that returned None
            # have already spoken for themselves -- staying quiet there is
            # deliberate, not a dropped reply.
            self._speak("Sorry, I didn't get an answer back.")
        self.set_state("idle")

    # --- screen vision commands ---
    def _take_screenshot(self):
        """Save a timestamped screenshot and copy it to the clipboard."""
        image = vision.grab_screen()
        if image is None:
            self.speaker.say("Sorry, I couldn't capture the screen.")
            return
        path = vision.take_screenshot()
        copied = vision.copy_image_to_clipboard(image)
        if path and copied:
            self.speaker.say("Screenshot saved and copied to your clipboard. "
                             "Press control V to paste it.")
        elif path:
            self.speaker.say("Screenshot saved to your Screenshots folder.")
        else:
            self.speaker.say("Sorry, I couldn't save the screenshot.")

    def _identify_song(self):
        """Listen to whatever is playing and name the song.

        Announces it's listening first -- say_blocking makes sure Orion's own
        voice has finished so it doesn't end up in the capture -- then records
        and identifies the track via song.identify.
        """
        self.set_state("thinking")
        self.speaker.say_blocking("Listening now, Sir.")
        _song_t0 = time.monotonic()
        result = song.identify(self)
        telemetry.add("action_ms", int((time.monotonic() - _song_t0) * 1000))
        if result:
            self.show_last(result)
            self._speak(result)
        self.set_state("idle")

    def _tell_time(self, query=""):
        """Say the current time from the PC clock, plus the date if asked."""
        from clock_words import spoken_time
        now = datetime.datetime.now()
        time_str = spoken_time(now, period=True)
        result = f"It's {time_str}, Sir."
        if any(w in query.lower() for w in ("date", "day", "today")):
            date_str = now.strftime("%A, %B %d").replace(" 0", " ")
            result = f"It's {time_str} on {date_str}, Sir."
        self.show_last(result)
        self._speak(result)
        self.set_state("idle")
        return result

    def draft_email(self, arg):
        """Have the AI write an email, open it in the mail client, and copy
        the body to the clipboard so it can be pasted with Ctrl+V.
        """
        self.set_state("thinking")

        recipient, topic = "", arg.strip()
        m = re.match(r"to (.+?) about (.+)$", arg, re.IGNORECASE)
        if m:
            recipient, topic = m.group(1).strip(), m.group(2).strip()
        elif arg.lower().startswith("to "):
            recipient = arg[3:].strip()
            topic = ""
        elif arg.lower().startswith("about "):
            topic = arg[6:].strip()

        # Turn a spoken address ("john at gmail dot com") into a real one.
        spoken_addr = (recipient.lower()
                       .replace(" at ", "@").replace(" dot ", ".")
                       .replace(" ", ""))
        to_field = spoken_addr if "@" in spoken_addr else ""

        who = f" to {recipient}" if recipient else ""
        subject_hint = topic or "a general message"
        prompt = (
            f"Write a professional email{who} about {subject_hint}. "
            "Begin with one line in the form 'Subject: <subject>', then a "
            "blank line, then the email body. Use a normal greeting and "
            "sign-off. Plain text only -- no markdown, asterisks or bullet "
            "points."
        )
        if not ai.send_query(prompt):
            self._speak("I couldn't reach my brain to draft the email.")
            self.set_state("idle")
            return

        response = ai.read_response("draft email") or ""
        subject, body = "Draft from Orion", response.strip()
        sm = re.match(r"\s*subject:\s*(.+)", body, re.IGNORECASE)
        if sm:
            subject = sm.group(1).strip()
            body = body[sm.end():].lstrip("\n")

        # Copy the body so the user can paste it straight away with Ctrl+V.
        try:
            import pyperclip
            pyperclip.copy(body)
            copied = True
        except Exception:
            copied = False

        mailto = "mailto:" + urllib.parse.quote(to_field)
        mailto += "?" + urllib.parse.urlencode(
            {"subject": subject, "body": body}, quote_via=urllib.parse.quote
        )
        webbrowser.open(mailto)

        memory.add_orion(response)
        self.show_last(response)
        msg = f"I've drafted your email about {subject_hint} and opened it."
        if copied:
            msg += " The text is on your clipboard, press control V to paste it."
        self._speak(msg)
        self.set_state("idle")

    def _explain_screen(self):
        """Screenshot the screen, send the image to the AI, and speak its description."""
        self.set_state("thinking")
        image = vision.grab_screen()
        if image is None:
            self._speak("I couldn't capture the screen.")
            self.set_state("idle")
            return
        if not vision.copy_image_to_clipboard(image):
            self._speak("I couldn't attach the screenshot. The win32clipboard "
                        "package may be missing.")
            self.set_state("idle")
            return

        prompt = memory.build_prompt(
            "Look at this screenshot of my screen and describe what you see.",
            speaker=getattr(self, "_last_speaker", "owner"),
        )
        if not ai.send_image_query(prompt):
            self._speak("I couldn't send the screenshot to my brain.")
            self.set_state("idle")
            return

        response = ai.read_response("describe screenshot")
        if response:
            memory.add_orion(response)
            self.show_last(response)
            self._speak(response)
        else:
            self._speak("Sorry, I didn't get an answer back.")
        self.set_state("idle")

    def _read_screen(self):
        """OCR the screen and read the extracted text aloud."""
        self.set_state("thinking")
        text = vision.ocr_text()
        if not text:
            self._speak("I couldn't find any readable text on the screen.")
            self.set_state("idle")
            return
        self.show_last(text)
        self._speak(text)
        self.set_state("idle")

    def _ocr_to_ai(self, instruction):
        """Screenshot the screen, send the image to the AI with `instruction`, speak the reply."""
        self.set_state("thinking")
        image = vision.grab_screen()
        if image is None:
            self._speak("I couldn't capture the screen.")
            self.set_state("idle")
            return
        if not vision.copy_image_to_clipboard(image):
            self._speak("I couldn't attach the screenshot. The win32clipboard "
                        "package may be missing.")
            self.set_state("idle")
            return

        prompt = memory.build_prompt(
            instruction, speaker=getattr(self, "_last_speaker", "owner")
        )
        if not ai.send_image_query(prompt):
            self._speak("I couldn't reach my brain. Check my API key.")
            self.set_state("idle")
            return

        response = ai.read_response(instruction)
        if response:
            memory.add_orion(response)
            self.show_last(response)
            self._speak(response)
        else:
            self._speak("Sorry, I didn't get an answer back.")
        self.set_state("idle")

    def _code_assist(self, intent):
        """Read the file open in the editor and send it to the AI.

        `intent` is one of code_assistant.INSTRUCTIONS (review, fix, explain,
        refactor, tests, comments). The whole file is sent, not a screenshot.
        """
        self.set_state("thinking")
        path, source = code_assistant.read_open_file()
        if path is None:
            self._speak(source)  # `source` holds the error message here
            self.set_state("idle")
            return

        instruction = code_assistant.INSTRUCTIONS[intent]
        code_prompt = code_assistant.build_prompt(instruction, path, source)
        prompt = memory.build_prompt(
            code_prompt, speaker=getattr(self, "_last_speaker", "owner")
        )
        if not ai.send_query(prompt):
            self._speak("I couldn't reach my brain. Check my API key.")
            self.set_state("idle")
            return

        response = ai.read_response(intent)
        if response:
            memory.add_orion(response)
            self.show_last(response)
            self._speak(response)
        else:
            self._speak("Sorry, I didn't get an answer back.")
        self.set_state("idle")

    def _briefing_state_path(self):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "briefing_state.json")

    def _should_run_briefing(self):
        """True if today's briefing hasn't fired yet AND it's morning.

        Gates the briefing to [BRIEFING_START_HOUR, BRIEFING_END_HOUR) and
        records the last-fired date in briefing_state.json so a same-day
        restart doesn't replay it.
        """
        if not getattr(config, "DAILY_BRIEFING", False):
            return False
        now = datetime.datetime.now()
        start = getattr(config, "BRIEFING_START_HOUR", 5)
        end = getattr(config, "BRIEFING_END_HOUR", 11)
        if not (start <= now.hour < end):
            return False
        try:
            with open(self._briefing_state_path(), "r", encoding="utf-8") as fh:
                last = json.load(fh).get("last_date")
        except Exception:
            last = None
        return last != now.date().isoformat()

    def _mark_briefing_done(self):
        try:
            with open(self._briefing_state_path(), "w", encoding="utf-8") as fh:
                json.dump({"last_date": datetime.date.today().isoformat()}, fh)
        except Exception:
            pass

    def _morning_music_state_path(self):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "morning_music_state.json")

    def _should_play_morning_music(self):
        if not getattr(config, "MORNING_MUSIC", False):
            return False
        if not getattr(config, "MORNING_MUSIC_URL", ""):
            return False
        now = datetime.datetime.now()
        start = getattr(config, "MORNING_MUSIC_START_HOUR", 5)
        end = getattr(config, "MORNING_MUSIC_END_HOUR", 11)
        if not (start <= now.hour < end):
            return False
        try:
            with open(self._morning_music_state_path(), "r",
                      encoding="utf-8") as fh:
                last = json.load(fh).get("last_date")
        except Exception:
            last = None
        return last != now.date().isoformat()

    def _mark_morning_music_done(self):
        try:
            with open(self._morning_music_state_path(), "w",
                      encoding="utf-8") as fh:
                json.dump({"last_date": datetime.date.today().isoformat()}, fh)
        except Exception:
            pass

    def _play_morning_music(self):
        try:
            import youtube_player
            youtube_player.play_url(config.MORNING_MUSIC_URL)
        except Exception:
            pass

    def _daily_briefing(self):
        """Speak a morning summary built from live weather/news/calendar data.

        The raw facts are gathered locally, then the AI formats them into a
        natural spoken briefing. After the briefing, if any unread email is
        configured AND present, Orion ASKS whether to summarise -- we
        never dump email content automatically.
        """
        self.set_state("thinking")
        prompt = briefing.build_briefing_prompt()
        if not ai.send_query(prompt):
            self.set_state("idle")
            return
        response = ai.read_response("daily briefing")
        if response:
            memory.add_orion(response)
            self.show_last(response)
            self._speak(response)

        # Optional email offer. Owner-only path -- if a guest happens to be
        # speaking right after boot, skip silently. Check unread count off
        # the main thread is fine since IMAP fetches are quick; do it
        # inline so the offer flows naturally after the briefing.
        if (getattr(config, "EMAIL_IN_BRIEFING", False)
                and getattr(self, "_last_speaker", "owner") == "owner"):
            try:
                import email_reader
                if email_reader.available():
                    total = email_reader.total_unread_count()
                    if total > 0:
                        word = "email" if total == 1 else "emails"
                        self._pending_email_offer = {"expires": time.monotonic() + 60}
                        self._speak(
                            f"You also have {total} unread {word}, Sir. "
                            "Would you like me to go through them?")
            except Exception:
                pass

        self.set_state("idle")

    def _init_push_to_talk(self):
        """Register a global hotkey that triggers immediate listening.

        Optional. Falls back gracefully if the `keyboard` package isn't
        installed or the hotkey can't be bound (e.g. another app has it).
        Set the hotkey in config.PUSH_TO_TALK_HOTKEY.
        """
        if not getattr(config, "PUSH_TO_TALK", False) or self._ptt_registered:
            return
        try:
            import keyboard
        except ImportError:
            return
        hotkey = getattr(config, "PUSH_TO_TALK_HOTKEY", None)
        if not hotkey:
            return
        try:
            keyboard.add_hotkey(hotkey, self._on_push_to_talk)
            self._ptt_registered = True
        except Exception:
            # Another app may already own the hotkey, or the platform may
            # block it. Stay silent -- the wake word still works.
            pass

    def _on_push_to_talk(self):
        """Hotkey callback: cut current speech, mark a PTT request pending.

        Runs on the `keyboard` library's listener thread. We DON'T touch the
        mic or run any heavy logic here -- the main loop sees the event,
        breaks out of wake-waiting, and captures the command on its own
        thread (the same thread that always handles dispatch).
        """
        # Barge-in: if Orion is talking, stop now so the user can speak.
        try:
            self.speaker.stop()
        except Exception:
            pass
        self._ptt_event.set()

    def _init_wake_detector(self):
        """Build the local wake detector AFTER the greeting has played.

        Deliberately deferred from __init__: see the comment there. The wake
        detector creates a PyAudio instance which enumerates audio devices,
        and stacking that on top of the other two boot-time PyAudio inits
        (mic calibration + the speech player) was producing 10-20 s of
        "sand"-like static from the audio driver. Doing it here, after the
        greeting has finished playing, gives the driver a few seconds to
        settle between each audio-subsystem init.
        """
        if not config.USE_LOCAL_WAKE or self.wake is not None:
            return
        try:
            from wake import WakeDetector
            detector = WakeDetector()
            if detector.available:
                self.wake = detector
        except Exception:
            self.wake = None

    # --- wake detection ---
    def _wake_local(self):
        """Wait on the offline wake-word model, then capture the command.

        The HUD flips to LISTENING the instant the model fires -- before any
        cloud speech-to-text -- so Orion visibly reacts the moment it hears
        its name. Returns the spoken command text, or None.
        """
        # Don't listen for the wake word over Orion's own speech, and pause
        # entirely while muted.
        while self.running and self.state == "muted":
            time.sleep(0.2)
        if not self.running:
            return None

        def ready():
            return (self.running and self.state != "muted"
                    and not self.speaker.is_speaking()
                    and not self._ptt_event.is_set())

        if not self.wake.wait_for_wake(ready) or not self.running:
            return None

        # Wake word heard -- react immediately. Open the mic stream NOW, the
        # instant the wake word fires, so the audio device finishes spinning up
        # while we flip the HUD and cut off any current speech. The wake
        # detector's own stream has just closed, and a command spoken in the
        # same breath ("Orion, put the music on") arrives within a few hundred
        # ms; opening the capture stream cold here would lose its first
        # syllable to the start-up latency. Pre-opening lets the device buffer
        # that audio instead. Falls back to a per-capture mic if it can't open.
        mic = None
        try:
            mic = sr.Microphone()
            mic.__enter__()
        except Exception:
            mic = None

        self.set_state("listening")
        if self.speaker.is_speaking():
            self.speaker.stop()

        # Listen straight away: if the command came in the same breath capture
        # it without making the user wait for a prompt.
        try:
            command = self._listen(timeout=config.WAKE_COMMAND_TIMEOUT, source=mic)
        finally:
            if mic is not None:
                # Close before the "Yes, Sir" prompt so a fresh capture below
                # doesn't start with Orion's own prompt audio buffered in it.
                try:
                    mic.__exit__(None, None, None)
                except Exception:
                    pass

        if command:
            return command
        # Nothing heard in the same breath -- acknowledge and open a full
        # follow-up window (the user pauses for the prompt, so a fresh mic is
        # fine here).
        self.speaker.say_blocking(config.FOLLOWUP_PROMPT)
        return self._listen(timeout=config.FOLLOWUP_TIMEOUT) or None

    def _wake_phrase(self):
        """Fallback wake detection: transcribe each phrase and scan it for the
        wake word.

        The listen uses a timeout so this loop keeps ticking even when nobody
        speaks. Ambient-noise recalibration happens in the main loop.
        """
        heard = self._listen(timeout=config.WAKE_LISTEN_TIMEOUT)
        if not heard or not self.running:
            return None

        # Tolerant wake-word detection (handles speech-to-text mishears).
        after = brain.find_wake(heard)
        if after is None:
            return None

        if self.speaker.is_speaking():
            self.speaker.stop()
        self.set_state("listening")

        if not after:
            self.speaker.say_blocking(config.FOLLOWUP_PROMPT)
            after = self._listen(timeout=config.FOLLOWUP_TIMEOUT)
        return after or None

    # --- open-mic conversation ---
    def _on_ear_onset(self, rms):
        """Fired from the ear thread the moment speech begins.

        Cuts the reply off immediately rather than waiting for the sentence to
        end and be transcribed, which is the difference between an assistant
        that stops when you talk over it and one that finishes its paragraph
        first. The ear only fires this for audio that already cleared the echo
        bar, so on loudspeakers Orion's own voice does not land here.
        """
        if not getattr(config, "INTERRUPT_ON_ONSET", True):
            return
        if self.speaker.is_speaking():
            self.speaker.stop()

    def _judge_phrase(self, phrase):
        """Transcribe one captured phrase and decide whether it was the user.

        Returns the transcript, or "" if the phrase was Orion's own voice
        coming back off the speakers, someone else in the room, or noise. A
        phrase caught as an echo teaches the ear's loudness bar how loud the
        leak is, so the same mistake gets cheaper every time it happens.
        """
        _stt_t0 = time.monotonic()
        text = self._transcribe(phrase.audio)
        stt_ms = int((time.monotonic() - _stt_t0) * 1000)
        if not text:
            return ""

        if phrase.during_playback and self._ear is not None:
            if ear_mod.looks_like_echo(text, self._ear.playing_text):
                self._ear.note_self(phrase.rms)
                return ""

        speaker = "owner"
        if getattr(config, "VOICE_VERIFICATION", False) and voice_id.is_enrolled():
            try:
                speaker = voice_id.identify(phrase.audio)
            except Exception:
                speaker = "owner"
            if speaker != "owner":
                # Not the owner. During playback that is almost always Orion's
                # own synthetic voice, which is the strongest echo signal we
                # have, so hand it to the loudness bar as well.
                if phrase.during_playback and self._ear is not None:
                    self._ear.note_self(phrase.rms)
                return ""

        self._last_audio = phrase.audio
        self._last_speaker = speaker
        self._last_stt_ms = stt_ms
        self._last_listen_ms = int((phrase.t_end - phrase.t_start) * 1000)
        presence.note_voice()
        replay_debug.record(phrase.audio, text, source="ear")
        return text

    def _ear_listen(self, timeout):
        """Wait up to `timeout` for real speech on the open mic.

        Returns (text, end_time) or None. Used for the follow-up window, so
        the microphone never has to be reopened between turns.
        """
        deadline = time.monotonic() + timeout
        while self.running:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            phrase = self._ear.get(timeout=min(remaining, 0.25))
            if phrase is None:
                continue
            text = self._judge_phrase(phrase)
            if text:
                return text, phrase.t_end
        return None

    def _run_turn(self, text, token):
        """Handle one command. Runs on its own thread so the main thread stays
        free to listen while the model is thinking and the reply is playing."""
        self._turn_local.token = token
        try:
            if self._turn_alive():
                telemetry.start_turn(
                    text,
                    speaker=getattr(self, "_last_speaker", "owner"),
                    source="push-to-talk" if self._ptt_event.is_set() else "wake",
                    listen_ms=getattr(self, "_last_listen_ms", 0),
                    stt_ms=getattr(self, "_last_stt_ms", 0),
                )
            try:
                if self._handle_command(text):
                    # A local command ran and changed something. Record that,
                    # so speech arriving now can't be folded back into a
                    # request that has already been carried out.
                    self._acted_token = token
                    self.set_state("idle")
                else:
                    self._ask_ai(text)
            finally:
                if self._turn_alive():
                    telemetry.end_turn()
                    if hub_server is not None:
                        try:
                            hub_server.publish("turn", {})
                        except Exception:
                            pass
        except Exception:
            pass
        finally:
            self._turn_local.token = None

    def _watch_turn(self, worker, token, last_end):
        """Listen while `worker` handles the current command.

        Returns None if the turn ran to completion untouched. Otherwise
        returns (text, end_time, merged): `merged` is True when the new speech
        arrived soon enough after the previous utterance to be a continuation
        of it rather than a separate request.
        """
        merge_window = float(getattr(config, "MERGE_WINDOW", 4.0))
        while self.running:
            alive = worker.is_alive()
            phrase = self._ear.get(timeout=0.15 if alive else 0.0)
            if phrase is None:
                if not alive:
                    return None
                continue
            text = self._judge_phrase(phrase)
            if not text:
                continue
            merged = ((phrase.t_start - last_end) <= merge_window
                      and self._acted_token != token)
            return text, phrase.t_end, merged
        return None

    def _converse(self, first):
        """Run a whole conversational turn cycle with the microphone held open.

        This is the open-mic path. The command is handled on a worker thread
        while this one keeps listening, so speech arriving mid-turn can do one
        of two things:

          * **Continue** the command, when it lands within MERGE_WINDOW of the
            last thing you said. The in-flight answer is thrown away and both
            halves are re-run together as a single request -- "change the
            song" plus "and something calmer" becomes one instruction rather
            than two contradictory ones.
          * **Replace** it, when it lands later. The reply is cut off and the
            new phrase becomes a fresh command.

        Falls back to the old turn-based path if the microphone can't be held
        open (no PyAudio, device in use).
        """
        if self._ear is None or not self._ear.start():
            self._converse_classic(first)
            return
        presence.note_voice()
        try:
            text = first
            last_end = time.monotonic()
            while text and self.running:
                self.show_heard(text)
                self._barged_in = False
                token = self._begin_turn()
                worker = threading.Thread(
                    target=self._run_turn, args=(text, token),
                    daemon=True, name="turn")
                worker.start()

                interrupted = self._watch_turn(worker, token, last_end)
                if interrupted is not None:
                    new_text, last_end, merged = interrupted
                    self._cancel_turn()
                    text = (text + " " + new_text) if merged else new_text
                    continue

                # The turn finished on its own. Hold the mic open briefly so a
                # reply needs no wake word, exactly as the old path did.
                if (config.FOLLOWUP_LISTEN and self.running
                        and self.state != "muted"):
                    self.set_state("listening")
                    heard = self._ear_listen(config.FOLLOWUP_WINDOW)
                else:
                    heard = None
                if heard is None:
                    text = None
                else:
                    text, last_end = heard
        finally:
            self._ear.stop()

    def _converse_classic(self, after):
        """The original turn-based cycle: listen, answer, listen again.

        Kept for when config.OPEN_MIC is off or the microphone can't be held
        open. Interruptions here need the wake word.
        """
        while after and self.running:
            self.show_heard(after)
            self._barged_in = False

            telemetry.start_turn(
                after,
                speaker=getattr(self, "_last_speaker", "owner"),
                source="push-to-talk" if self._ptt_event.is_set() else "wake",
                listen_ms=getattr(self, "_last_listen_ms", 0),
                stt_ms=getattr(self, "_last_stt_ms", 0),
            )
            try:
                if self._handle_command(after):
                    self.set_state("idle")
                else:
                    self._ask_ai(after)
            finally:
                telemetry.end_turn()
                if hub_server is not None:
                    try:
                        hub_server.publish("turn", {})
                    except Exception:
                        pass

            # Keep the conversation flowing: after Orion answers, hold the mic
            # open briefly so the user can reply without saying the wake word
            # again. A barge-in already means they want to speak, so listen
            # then too. Skipped while muted.
            follow_up = self._barged_in or config.FOLLOWUP_LISTEN
            if follow_up and self.running and self.state != "muted":
                self.set_state("listening")
                after = self._listen(timeout=config.FOLLOWUP_WINDOW) or None
            else:
                after = None

    # --- main loop ---
    def loop(self):
        self._calibrate()
        self._last_calibrate = time.monotonic()
        # Block on the greeting so the player's PyAudio init has completed
        # before we touch a third audio subsystem (the wake detector below).
        # Three back-to-back audio inits cause the "sand"/static driver wake
        # noise on some HD-audio chips; spacing them out by the greeting's
        # duration is enough for the driver to settle between each one.
        will_brief = self._should_run_briefing()
        if will_brief and getattr(config, "BRIEFING_SUPPRESSES_GREETING", True):
            # Briefing will lead -- skip the chatty AI greeting so we don't
            # get "hey, how can I help you today this beautiful Monday" right
            # before the actual morning briefing.
            self.speaker.say_blocking(memory.time_of_day_greeting())
        else:
            self.speaker.say_blocking(self._startup_greeting())
        self._init_wake_detector()
        self._init_push_to_talk()
        self.set_state("idle")

        if will_brief:
            self._daily_briefing()
            self._mark_briefing_done()

        if self._should_play_morning_music():
            self._play_morning_music()
            self._mark_morning_music_done()

        # Warm the song-recognition helper in the background so the first
        # "what song is this" doesn't pay the ~30s ffmpeg/shazamio cold start.
        try:
            import song
            song.prewarm()
        except Exception:
            pass

        # Warm the model's tool-calling path so the FIRST spoken question after
        # boot doesn't sit in "thinking" for 5-10 s while the SDK does its
        # one-time TLS handshake + tool-config init. The startup
        # greeting only hits `ask_once`, which doesn't exercise the
        # function-calling code path — so without this warmup the very first
        # `_ask_ai_tools` call pays the full cold-start cost.
        def _warm_ai_tools():
            try:
                if (config.FUNCTION_CALLING
                        and hasattr(ai, "send_with_tools")):
                    ai.send_with_tools("ping", tools.declarations())
                elif hasattr(ai, "ask_once"):
                    ai.ask_once("ping")
            except Exception:
                pass
        threading.Thread(target=_warm_ai_tools, daemon=True).start()

        # Start watching for things worth speaking up about unprompted.
        self.monitor.start()

        # Persistent reminders: announce anything missed while Orion was off,
        # then start the background scheduler.
        reminders_mod.boot_catchup(self.speaker.say)
        reminders_mod.start(self.speaker.say)

        # Universal Hub: start the local dashboard server in the background.
        if hub_server is not None and getattr(config, "HUB_AUTOSTART", True):
            try:
                hub_server.attach_orion(self)
                hub_server.start()
                if getattr(config, "HUB_OPEN_ON_START", False) and hub_launcher is not None:
                    hub_launcher.open_hub()
            except Exception:
                pass

        while self.running:
            # Re-anchor the mic's ambient-noise floor on a wall-clock timer.
            # dynamic_energy_threshold drifts up after loud audio (e.g. game
            # sound) and never settles back down -- without this, Orion goes
            # deaf "after a while" and stops hearing both the wake word and
            # the command. Runs in every wake mode, including local-model.
            now = time.monotonic()
            if now - self._last_calibrate >= config.RECALIBRATE_SECONDS:
                self._calibrate()
                self._last_calibrate = now

            if self.state != "muted":
                self.set_state("wake")

            if self.wake is not None:
                after = self._wake_local()
            else:
                after = self._wake_phrase()

            # Push-to-talk: the hotkey fires from a background thread,
            # makes the wake-watcher bail out, and lands us here with no
            # `after` text yet. Capture the user's command on this thread
            # exactly like a normal wake-word trigger.
            if self._ptt_event.is_set():
                self._ptt_event.clear()
                if self.speaker.is_speaking():
                    self.speaker.stop()
                self.set_state("listening")
                after = self._listen(
                    timeout=config.WAKE_COMMAND_TIMEOUT) or None
                if not after:
                    # No same-breath command -- give the same "Yes, Sir"
                    # prompt the wake word does, so the user has a full
                    # window to speak.
                    self.speaker.say_blocking(config.FOLLOWUP_PROMPT)
                    after = self._listen(
                        timeout=config.FOLLOWUP_TIMEOUT) or None

            # Handle the command, then keep the conversation going: one wake
            # word turns into a flowing back-and-forth rather than a one-shot
            # exchange. With the open mic on, that includes speech arriving
            # while Orion is still thinking or still talking.
            if after and self.running:
                if getattr(config, "OPEN_MIC", True) and self._ear is not None:
                    self._converse(after)
                else:
                    self._converse_classic(after)

    def restart(self):
        """Relaunch Orion: spawn a fresh detached process, then shut this
        one down. Used by the Hub's Restart button and the tray menu. The
        new process spends its first seconds on imports and model loads,
        so the mic/audio devices are free again well before it needs them."""
        import subprocess
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "orion.py")
        exe = sys.executable
        # Prefer the windowless interpreter so the restarted Orion doesn't
        # drag a console window along (matches how the launcher runs it).
        if exe.lower().endswith("python.exe"):
            pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
            if os.path.exists(pyw):
                exe = pyw
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        try:
            subprocess.Popen([exe, script], cwd=os.path.dirname(script),
                             creationflags=flags)
        except Exception:
            return  # failed to spawn -- keep this instance alive
        self.shutdown(reason="restart: handing over to a fresh process")

    def shutdown(self, reason="unspecified"):
        """Stop Orion for good. `reason` is recorded first.

        Orion ends with os._exit, which writes nothing and unwinds nothing, so
        without this line an intentional shutdown and a crash look identical
        afterwards: the process is simply gone. Logging the reason (and who
        asked) before exiting is what makes "it closed itself again"
        answerable rather than a guess.
        """
        if not self.running:
            return
        try:
            import logbuf
            caller = "?"
            try:
                frame = sys._getframe(1)
                caller = f"{os.path.basename(frame.f_code.co_filename)}:" \
                         f"{frame.f_lineno} in {frame.f_code.co_name}"
            except Exception:
                pass
            logbuf.log(f"shutting down -- reason: {reason} (from {caller})")
        except Exception:
            pass
        self.running = False
        if self._ptt_registered:
            try:
                import keyboard
                keyboard.remove_all_hotkeys()
            except Exception:
                pass
            self._ptt_registered = False
        try:
            memory.end_session()
        except Exception:
            pass
        self.speaker.shutdown()
        if self.tray is not None:
            self.tray.stop()
        if self.hud is not None:
            self.hud.close()
        os._exit(0)

    def start(self):
        # Tray runs detached so it doesn't block the listener or the HUD.
        self.tray.run_detached()
        listener = threading.Thread(target=self.loop, daemon=True)
        # Remembered so code that must not run off this thread -- anything
        # touching the wake detector's microphone and model -- can check.
        self._loop_thread = listener
        listener.start()
        if self.hud is not None:
            # The overlay owns the main thread. If its loop ever ends -- an
            # exception, a window torn down by the desktop, a display change
            # -- returning from here would end main() and take the whole of
            # Orion with it, silently. That is exactly what "it closed itself
            # after a while" looks like from the outside. So the overlay is
            # allowed to die, and Orion keeps listening without it.
            try:
                self.hud.run()  # blocks on the main thread
            except Exception:
                try:
                    import logbuf
                    logbuf.log("HUD loop raised; continuing without it")
                    import traceback
                    logbuf.log(traceback.format_exc())
                except Exception:
                    pass
            if self.running:
                try:
                    import logbuf
                    logbuf.log("HUD loop ended while Orion is still running; "
                               "staying alive without the overlay")
                except Exception:
                    pass
                self.hud = None
        while self.running:
            time.sleep(0.5)


def main():
    # Capture stdout/stderr/tracebacks into a ring buffer first, so the
    # Hub's Logs tab can show them. Under pythonw both streams are None
    # and everything written to them is otherwise lost.
    try:
        import logbuf
        logbuf.install(logfile=os.path.join(_paths.ROOT, "orion.log"))
        logbuf.log("Orion starting")
    except Exception:
        pass

    # Give the main thread a live COM apartment. Windows audio interfaces are
    # COM objects, and Python frees them from whichever thread happens to be
    # garbage-collecting -- which here is the thread running the overlay. This
    # is insurance, not the fix: code that creates such objects is responsible
    # for keeping them on one thread (see presence.py). It only means a stray
    # release lands somewhere valid instead of taking the process down.
    try:
        import comtypes
        comtypes.CoInitialize()
    except Exception:
        pass
    # Load the semantic intent model in the background so the first spoken
    # command isn't delayed by the one-time model load.
    if config.SEMANTIC_INTENT:
        semantic.warmup()
    # Preload the Whisper model in the background too, so the first spoken
    # command isn't delayed by the one-time model load.
    if config.STT_ENGINE == "whisper":
        transcribe.warmup()
    # Preload the speaker-verification encoder so the first command after
    # boot doesn't stall for ~4 s inside _listen while resemblyzer lazily
    # builds its model and JITs the first forward pass.
    if getattr(config, "VOICE_VERIFICATION", False):
        voice_id.warmup()
    orion = Orion()
    try:
        orion.start()
    except KeyboardInterrupt:
        orion.shutdown(reason="KeyboardInterrupt")


if __name__ == "__main__":
    main()
