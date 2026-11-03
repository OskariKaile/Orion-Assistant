"""Orion configuration. Edit these values to tune behavior."""

# --- Wake word ---
WAKE_WORD = "orion"          # spoken trigger (lowercase)
FOLLOWUP_PROMPT = "Yes, Sir"  # said when only the wake word is heard

# --- Voice output ---
TTS_RATE = 195               # words per minute (pyttsx3 engine only)
TTS_VOLUME = 0.5             # 0.0 - 1.0 (keep >= 0.3; lower values add noise)
# Kept below 1.0 on purpose: the Piper voice peaks near full scale, so at 1.0
# it has no headroom and the OS mixer clips ("crispy" distortion) when music
# or game audio is added on top. 0.5 leaves room for both to coexist cleanly.
# Substring used to pick a deep male voice; first matching voice is used.
# (pyttsx3 engine only.)
PREFERRED_VOICE_HINTS = ["david", "male", "mark", "guy"]

# Output sample rate (Hz). Orion's voice model is 22050 Hz; the player
# resamples it up to the audio device's native rate so Windows never does a
# cheap, aliasing resample of its own ("crispy" distortion). Normally the
# device rate is detected automatically -- leave this None for that. Set it
# explicitly (e.g. 44100 or 48000) to FORCE the output rate, useful when the
# detected rate disagrees with the Windows shared-mode mixer rate. Match it
# to Windows Sound settings -> your device -> Advanced -> Default Format.
OUTPUT_SAMPLE_RATE = None

# --- Text-to-speech engine ---
# "piper"   = local neural TTS: natural, human-sounding, fully offline, runs
#             on the CPU only (no GPU/VRAM use, so games are unaffected).
# "pyttsx3" = the built-in robotic Windows voice.
# With "piper", Orion falls back to pyttsx3 automatically if the engine or
# the voice model isn't available.
TTS_ENGINE = "piper"
# Piper voice model name (without file extension). The .onnx and .onnx.json
# files live in PIPER_VOICE_DIR. Download a voice with:
#     python download_voice.py
# British male (JARVIS-style, calm and measured): en_GB-alan-medium,
# en_GB-northern_english_male-medium.
# American male: en_US-ryan-high (clear), en_US-hfc_male-medium, en_US-joe-medium.
# "high" voices sound best but synthesize a touch slower. After changing this,
# re-run `python download_voice.py` to fetch the new voice's files.
PIPER_VOICE = "en_US-ryan-medium"
PIPER_VOICE_DIR = "voices"
# Prosody tuning for the Piper voice. All optional -- leave at the defaults
# below for the voice's natural sound, or nudge them to taste:
#   PIPER_LENGTH_SCALE  -- pacing. 1.0 = normal; >1.0 speaks slower and more
#                          deliberate (calm, JARVIS-like); <1.0 is faster.
#   PIPER_NOISE_SCALE   -- expressiveness of the voice. Higher = more varied
#                          intonation; lower = flatter, steadier.
#   PIPER_NOISE_W_SCALE -- variation in phoneme timing/cadence. Higher sounds
#                          a touch more human; too high gets unsteady.
# None means "use the voice model's own default" for that parameter.
# 0.85: noticeably snappier than the 1.0 default, but Piper's autoregressive
# attention stays stable. Earlier value 0.75 caused intermittent (~10%)
# mid-sentence garbling -- the model would drift and emit nonsense phonemes
# for the rest of the chunk (e.g. "you are currently vie... bla bla bla...").
PIPER_LENGTH_SCALE = 0.85
PIPER_NOISE_SCALE = None
PIPER_NOISE_W_SCALE = None

# --- Speech recognition ---
# STT engine that turns your microphone audio into text:
#   "whisper" = local faster-whisper model: offline, free, no quota, and more
#               accurate. CPU-only -- it never touches the GPU, so games keep
#               all of the VRAM. Needs the `faster-whisper` package; if it
#               isn't installed Orion falls back to Google automatically.
#   "google"  = Google's free web speech API (needs internet, has no formal
#               quota but can throttle).
STT_ENGINE = "whisper"
# Whisper model size. "small.en" is the sweet spot: the English-only model is
# noticeably more accurate on English than the multilingual "small" at the
# same size and speed (~460 MB, transcribes a short phrase in well under a
# second on a modern CPU). Smaller/faster: "tiny.en", "base.en". More
# accurate/slower: "medium.en" or "distil-medium.en" (both 3-4x slower on the
# CPU). Keep this English-only ".en" variant unless you speak other languages
# to Orion.
WHISPER_MODEL = "distil-medium.en"  # legacy; unused since the Moonshine swap
# Moonshine model name. "base" (~61M params, ~3.6% WER) is the sweet spot for
# personal-assistant use. "tiny" (~27M) is faster but drops accuracy on longer
# or vocabulary-rich queries.
MOONSHINE_MODEL = "base"
# "float" (fp32) is the working precision. "quantized" (int8) is faster on
# paper but currently crashes onnxruntime on Windows -- revisit if upstream
# fixes the QDQ MatMul bug. Even at fp32, Moonshine-base runs in ~50ms on
# this hardware, so quantization isn't a meaningful win for now.
MOONSHINE_PRECISION = "float"
# "cpu" keeps the GPU free for games. "cuda" is faster but uses VRAM.
WHISPER_DEVICE = "cpu"
# "int8" is fast and low-memory on the CPU. Use "float16" with a CUDA device.
WHISPER_COMPUTE_TYPE = "int8"
# Spoken language (ISO code). None lets Whisper auto-detect.
WHISPER_LANGUAGE = "en"
# Decoding beam size. 1 is fastest; higher is marginally more accurate.
WHISPER_BEAM_SIZE = 1
# CPU threads CT2 may use for inference. 0 lets it pick (often suboptimal);
# None below means "use os.cpu_count()". Override to a specific number if
# you want to leave headroom for other apps.
WHISPER_CPU_THREADS = None
# A short, natural-sounding hint of the vocabulary Orion expects to hear. It
# is NOT spoken or transcribed -- Whisper just uses it to bias word choices,
# so names it would otherwise mangle ("Orion" -> "oh Ryan", app names) come
# out right. Costs nothing and doesn't slow transcription. Keep it natural
# prose, not a word list, and add words/app names you use often.
WHISPER_INITIAL_PROMPT = (
    "Hey Orion. Orion, open Spotify, YouTube, Chrome, Discord and GitHub. "
    "Set a timer, take a screenshot, what song is this, lock the PC, "
    "turn on the lights, what time is it., open work tabs, find the file,work on my project, look something up for me,work"
)

# Per-turn telemetry. One JSONL row per handled command. Render
# the dashboard with `python orion_stats.py`. Owner phrases only --
# non-owner turns are never logged.
TELEMETRY = True
TELEMETRY_FILE = "stats.jsonl"

# Rolling capture of recent voice phrases for debugging STT. Each
# captured phrase is saved as a WAV + JSONL entry under REPLAY_DEBUG_DIR.
# Replay with `python replay_debug.py list` / `play <idx>`.
REPLAY_DEBUG = True
REPLAY_DEBUG_DIR = "debug_audio"
REPLAY_DEBUG_KEEP = 50       # max entries retained (oldest pruned)
REPLAY_DEBUG_MAX_MB = 50     # total size cap; oldest pruned past this

# Rolling capture of TTS output WAVs for debugging playback bugs (the
# intermittent "speaks too fast / garbled" issue). Each synthesized
# sentence is copied to TTS_DEBUG_DIR before the temp WAV is deleted.
# Replay with `python tts_debug.py list` / `play <idx>`.
TTS_DEBUG = True
TTS_DEBUG_DIR = "tts_debug"
TTS_DEBUG_KEEP = 100
TTS_DEBUG_MAX_MB = 100

LANGUAGE = "en-US"           # recognition language passed to Google STT
ENERGY_THRESHOLD = 300       # ambient noise floor; auto-adjusted on start
PAUSE_THRESHOLD = 0.7        # seconds of silence that ends a phrase. Kept
                             # short so Orion starts working the moment you
                             # finish speaking instead of waiting a full
                             # second. Too short clips you mid-word and Whisper
                             # hallucinates from the truncated clip -- 0.7 is a
                             # safe balance. Raise toward 1.0 if it cuts you off.
NON_SPEAKING_DURATION = 0.7  # audio kept on each side of a captured phrase.
                             # This is also the PRE-ROLL: the amount of audio
                             # before speech is detected that gets prepended, so
                             # a soft sentence start ('s', 't', 'wh', 'h' onsets
                             # that don't cross the energy bar) isn't clipped.
                             # Raised 0.5 -> 0.7 to widen that front cushion (and
                             # keep more trailing low-energy end consonants too).
                             # Must be <= PAUSE_THRESHOLD.
PHRASE_TIME_LIMIT = 15       # max seconds captured per phrase
FOLLOWUP_TIMEOUT = 6         # seconds to wait for a follow-up question
WAKE_COMMAND_TIMEOUT = 2     # after the wake word fires, seconds to wait for a
                             # command spoken in the same breath ("Orion, play
                             # music") before falling back to the "Yes, Sir"
                             # prompt -- so a one-breath command isn't lost
FOLLOWUP_LISTEN = True       # after Orion answers, automatically hold the mic
                             # open for a few seconds so you can reply without
                             # saying the wake word again -- a natural
                             # back-and-forth. The HUD shows LISTENING (green)
                             # during the window; say nothing and Orion just
                             # goes back to waiting for the wake word.
FOLLOWUP_WINDOW = 5          # seconds the follow-up mic stays open
WAKE_FUZZY_THRESHOLD = 0.55  # how close a heard word must sound to the wake word
                             # (lowered so the wake word still fires when it's
                             # not pronounced perfectly; raise toward 0.7 if
                             # Orion starts waking up by mistake)
WAKE_LISTEN_TIMEOUT = 8      # fallback wake loop: seconds before a listen cycle ticks
RECALIBRATE_SECONDS = 30     # re-measure ambient noise at least every N seconds

# Mishearing corrections: words the speech engine commonly gets wrong (often
# because a sound is hard to pronounce) are swapped for what was meant before
# Orion matches a command. Keys are the WRONG word as heard, values are the
# RIGHT word. Matching is whole-word and case-insensitive. Add your own pairs
# here -- e.g. if "w" is hard for you and "work" keeps coming out as "fork".
MISHEARD_WORDS = {
    "fork": "work",
}

# --- Local wake-word detection (openWakeWord) ---
# When True, a small offline model listens continuously for the wake word and
# fires the instant it hears it -- the HUD reacts immediately, before any
# cloud speech-to-text. Needs the `openwakeword` package; if it isn't
# installed Orion falls back to phrase-based wake detection.
USE_LOCAL_WAKE = True
# Wake model: a built-in openWakeWord name ("hey_jarvis", "alexa",
# "hey_mycroft", "hey_rhasspy") or a path to a custom-trained .onnx model.
# openWakeWord has NO built-in "orion" model -- to trigger on the name "Orion"
# you must train one and point this at the resulting .onnx file.
# Train one in ~1 hour with the official Colab notebook (no GPU needed locally):
#   https://github.com/dscripka/openWakeWord  ->  "Training new models"
# Set the wake phrase to "hey orion" / "orion", download the .onnx file it
# produces, and drop it at the path below.
# Until that file exists, WakeDetector.available stays False and Orion falls
# back to phrase-based wake detection automatically -- so this is safe to
# enable now.
WAKE_MODEL = "models/o.onnx"
# Detection sensitivity 0..1. Higher = fewer false triggers but more misses.
WAKE_THRESHOLD = 0.2
# Barge-in: when True, saying the wake word while Orion is talking cuts the
# reply off instantly and Orion listens for your next command -- so you can
# interrupt and redirect mid-sentence like a real conversation. Needs a local
# wake model (USE_LOCAL_WAKE); has no effect until one is loaded.
BARGE_IN = True

# Ask before shutting down. The exit intent matches ordinary words ("bye",
# "close", "turn off"), and a shutdown is the one action with no way back --
# once the process is gone there is nothing left to correct. Set False to go
# back to shutting down on the first word.
CONFIRM_EXIT = True
CONFIRM_EXIT_WINDOW = 20  # seconds the "shut down?" question stays open

# --- Open mic: interrupt and add-on without the wake word (ear.py) ---
# With this on, the microphone stays open for the whole turn instead of
# closing the moment you stop talking. Two things become possible:
#
#   * Adding to a command you already finished. Say "change the song",
#     pause, then "and turn it down" -- the second half arrives while Orion
#     is still working, the in-flight answer is thrown away, and both halves
#     are handled together as one request.
#   * Interrupting a reply with plain speech, no wake word needed.
#
# Both need Orion to not hear itself. On headphones that is free. On
# loudspeakers the echo guard in ear.py handles it: a self-calibrating
# loudness bar, a check of the transcript against the words Orion is
# currently speaking, and (when enrolled) speaker verification. Set this to
# False to go back to the old strictly-turn-based listening.
OPEN_MIC = True
# Seconds after you stop speaking during which more speech is treated as a
# CONTINUATION of the same command rather than a new one. Covers a normal
# mid-thought pause; past this an interruption starts a fresh request.
MERGE_WINDOW = 4.0
# Cut Orion's reply off the instant speech is detected, without waiting for
# the sentence to finish and be transcribed. Snappier, but on loudspeakers a
# stray noise can clip the reply -- set False to only stop once a phrase has
# been captured and cleared the echo guard.
INTERRUPT_ON_ONSET = True
# --- echo guard tuning (only matters on loudspeakers) ---
EAR_PRE_ROLL = 0.5        # seconds of audio kept from before speech is
                          # detected, so soft sentence starts aren't clipped
EAR_ONSET_MS = 90         # sustained loudness needed to call it speech
EAR_MIN_BAR = 120         # absolute floor for the speech bar (RMS, 16-bit)
EAR_AMBIENT_MARGIN = 2.2  # how far above the measured room noise the bar sits
EAR_ECHO_MARGIN = 1.8     # how far above Orion's own measured leakage the bar
                          # sits while it is speaking. Raise if Orion keeps
                          # interrupting itself on speakers; lower if it
                          # ignores you while it talks.
EAR_ECHO_SIMILARITY = 0.6  # word overlap with the sentence being spoken above
                           # which a phrase is judged to be Orion's own echo
EAR_CALIBRATE_MS = 450    # at the start of every reply, the first moment of
                          # audio is measured as Orion's own leak into the
                          # mic. This is what makes speakers work with no
                          # setup and keeps up with the volume knob. You
                          # cannot interrupt during this window, so keep it
                          # short.

# --- Presence: is the user actually at the computer? (presence.py) ---
# Reminders that fire at an empty desk are missed reminders. When this is on,
# Orion holds a due reminder back until you are actually there, then delivers
# it as a "while you were away" summary.
#
# Idle mouse/keyboard alone is NOT treated as absence -- watching a video or
# listening to music looks exactly like an empty chair to Windows. Presence
# resolves to three states from four signals (input, lock, audio output,
# fullscreen), and only the clear cases count as away.
PRESENCE_ENABLED = True
PRESENCE_ACTIVE_SECONDS = 180   # input within this long = definitely here
PRESENCE_AWAY_SECONDS = 600     # no input this long, with nothing playing and
                                # nothing fullscreen, = probably not here
PRESENCE_VOICE_WINDOW = 180     # hearing you speak counts as present this long
PRESENCE_AUDIO_FLOOR = 0.005    # output meter peak above which sound counts
                                # as playing (0..1)
# The output meter is a COM object, and COM objects cannot live safely in
# Orion's process: Python's cyclic collector frees them from an arbitrary
# thread, which is an access violation that kills the process outright. So the
# meter runs in a separate process (features/audio_probe.py) that Orion reads
# lines from. These settings control that process, not the check itself.
PRESENCE_AUDIO_POLL = 2.0       # seconds between meter readings
PRESENCE_AUDIO_IDLE = 300       # stop the probe after this long with no
                                # presence check asking for it
HOLD_REMINDERS_WHEN_AWAY = True
REMINDER_RETURN_DELAY = 4       # seconds to let you settle after coming back
                                # before speaking held reminders
REMINDER_MAX_HOLD_MINUTES = 240  # give up holding after this long and speak
                                 # anyway, so nothing is lost forever

# --- Semantic intent (local CPU model) ---
# When True, Orion understands reworded commands by *meaning*, not just by
# matching exact words ("look something up for me" -> a web request). It uses
# a tiny embedding model that runs on the CPU only -- it never touches the
# GPU, so games keep all of the VRAM. Needs the `fastembed` package; if that
# isn't installed Orion silently falls back to plain difflib matching.
SEMANTIC_INTENT = True
EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
# Confidence bands for a semantic match (cosine similarity, 0..1):
#   >= SEMANTIC_HIGH     -> act on the command immediately
#   >= SEMANTIC_CLARIFY  -> ask the user to confirm before acting
#   below that           -> treat it as a normal question for the AI
# Raise SEMANTIC_HIGH if Orion acts when it shouldn't; lower it if it asks
# too often.
SEMANTIC_HIGH = 0.60
SEMANTIC_CLARIFY = 0.40

# --- AI brain ---
# Which model does Orion's thinking. One switch for the lot (llm_router.py):
#   "deepseek" -- DeepSeek over its OpenAI-compatible API. Cheapest option
#                 and good at tool routing, but text-only and with no web
#                 search, so images and real-time questions get handed to
#                 LLM_IMAGE_BACKEND / LLM_SEARCH_BACKEND below.
#   "gemini"   -- Google Gemini for everything. Set this to go back to
#                 Gemini-only: it does images and search itself, so the
#                 hand-offs below become no-ops. Nothing else to change.
#   "grok"     -- xAI Grok for everything.
# Read live, so changing it in the Hub applies without restarting Orion.
LLM_BACKEND = "deepseek"

# Who covers what LLM_BACKEND can't do. Ignored when the main backend
# handles the job itself.
LLM_IMAGE_BACKEND = "gemini"   # screenshots and "look at this" questions
LLM_SEARCH_BACKEND = "gemini"  # real-time questions needing Google Search

# Second chance when the main backend errors out (missing key, API down,
# rate limit). Set to "" to switch the fallback off and let failures surface.
LLM_FALLBACK = "gemini"

# Free Gemini API key. Keep the real key out of this file: put it in
# secrets_local.py (git-ignored) or the GEMINI_API_KEY environment variable.
# Get a key at https://aistudio.google.com/apikey
GEMINI_API_KEY = ""
# Gemini model. gemini-3.1-flash-lite is the cheapest current Flash tier
# ($0.25 in / $1.50 out per 1M tokens) and low-latency -- good for voice.
GEMINI_MODEL = "gemini-3.1-flash-lite"

# DeepSeek API key. Keep the real key out of this file: put it in
# secrets_local.py (git-ignored) or the DEEPSEEK_API_KEY environment variable.
# Get a key at https://platform.deepseek.com
DEEPSEEK_API_KEY = ""
# DeepSeek model. deepseek-chat is the non-thinking V3 tier -- fast enough
# for voice and far cheaper than the others. deepseek-reasoner thinks first,
# which is smarter but too slow for a spoken back-and-forth.
DEEPSEEK_MODEL = "deepseek-chat"
# DeepSeek's API is OpenAI-compatible; this is its base URL.
DEEPSEEK_BASE_URL = "https://api.deepseek.com"

# xAI Grok API key. Keep the real key out of this file: put it in
# secrets_local.py (git-ignored) or the GROK_API_KEY environment variable.
# Get a key at https://console.x.ai
GROK_API_KEY = ""
# Grok model. grok-4.1-fast is cheap, fast, and strong at tool routing.
GROK_MODEL = "grok-4.1-fast"
# xAI's API is OpenAI-compatible; this is its base URL.
GROK_BASE_URL = "https://api.x.ai/v1"

# --- Function calling ---
# When True, anything not caught by the instant local fast-paths is sent to
# the model WITH Orion's tools attached (see tools.py). The model reads the
# request in natural language and decides which tool to run -- so Orion
# understands reworded commands instead of needing every phrasing spelled
# out. If it picks no tool, it just answers the question normally.
FUNCTION_CALLING = True

# AI-first routing. True: nearly every request goes to the model, which
# reads the whole sentence and picks the tool from what you meant. Only
# instant controls stay local -- stop / mute / unmute, pause / resume /
# skip / previous track, volume, show / hide the HUD, "open the hub". This
# trades ~1 s of model time for not having half-heard sentences grabbed by
# a local phrase list and sent to the wrong tool.
# False: the old local fast-paths (email, agenda, reminders, music, time,
# song ID, timers, ...) answer straight away without asking the model.
AI_FIRST = True

# Show each tool the model calls as a chip above the HUD pill
# ("fn  set_timer(minutes=20)"), with a spinner until it finishes.
HUD_TOOL_CHIPS = True

# When True, tool results are fed BACK to the model so it can take another
# step -- look something up, then act on what it found. Without this the
# model gets exactly one turn: it fires tools blind and Orion speaks whatever
# they returned, so "cancel both reminders at 8" can only ever guess at one.
#
# The extra round trip is not paid on every command. A round of pure ACTION
# tools (lights on, set a reminder) ends the turn immediately, exactly as
# before -- Orion only goes back to the model when a QUERY tool ran and its
# result is something the model needs to read (see tools.QUERY_TOOLS).
AGENTIC_TOOLS = True
# Hard ceiling on model round trips per spoken command. Each step is one API
# call, so this bounds both latency and cost if the model ever loops. 4 is
# plenty: look up -> act -> confirm.
MAX_TOOL_STEPS = 4

# --- MCP (Model Context Protocol) ------------------------------------------
# Orion can act as an MCP *client*: at startup it launches the MCP servers
# listed below, speaks the Model Context Protocol to them over stdio, discovers
# the tools each one exposes, and offers those tools to the LLM right alongside
# Orion's own built-in tools (see mcp_client.py + tools.py). MCP is an open
# standard, so any third-party MCP server works without code changes.
#
# Set MCP_ENABLED = False to switch the whole thing off (Orion then runs with
# only its built-in tools, exactly as before).
MCP_ENABLED = True

# Each server entry is launched as a subprocess and talked to over stdio.
# Two ways to point at a server:
#   - "module": a Python script in this project, run with Orion's own venv.
#   - "command" (+ optional "args"): any executable, e.g. a published server
#       {"name": "filesystem", "command": "npx",
#        "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:\\Notes"]}
# Tools from a server show up to the LLM as  mcp__<name>__<toolname>.
# API keys for the key-gated MCP servers below. Kept out of git: put the real
# values in secrets_local.py (or as environment variables) -- never here.
import os as _os
import sys as _sys
try:
    import secrets_local as _sl
except Exception:
    _sl = None


def _secret(name):
    val = getattr(_sl, name, "") if _sl else ""
    return (val or _os.environ.get(name, "")).strip()


GOOGLE_MAPS_API_KEY = _secret("GOOGLE_MAPS_API_KEY")

MCP_SERVERS = [
    # Filesystem (official, runs via npx; Node required). Read-only: `deny`
    # hides the mutating tools, so Orion can browse/read files under the listed
    # folder(s) but never write/move/delete. It can ONLY touch what's listed.
    {"name": "files", "command": "npx",
     "args": ["-y", "@modelcontextprotocol/server-filesystem",
              _os.path.join(_os.path.expanduser("~"), "Desktop")],
     "deny": ["write_file", "edit_file", "create_directory", "move_file"]},

    # Fetch (official, Python -- installed in Orion's venv). Lets Orion pull a
    # web page and hand back clean text: "summarize this page". No key needed.
    {"name": "fetch", "command": _sys.executable,
     "args": ["-m", "mcp_server_fetch"]},
]

# Google Maps -- directions, places, geocoding. Only loaded once a key is set.
# Key: Google Cloud Console -> enable Maps APIs  ->  GOOGLE_MAPS_API_KEY in secrets_local.py
if GOOGLE_MAPS_API_KEY:
    MCP_SERVERS.append(
        {"name": "maps", "command": "npx",
         "args": ["-y", "@modelcontextprotocol/server-google-maps"],
         "env": {"GOOGLE_MAPS_API_KEY": GOOGLE_MAPS_API_KEY}})

# Pull secrets out of secrets_local.py if it exists (overrides the blanks
# above). This keeps API keys from being committed to git.
try:
    import secrets_local as _secrets
    GEMINI_API_KEY = getattr(_secrets, "GEMINI_API_KEY", "") or GEMINI_API_KEY
    GROK_API_KEY = getattr(_secrets, "GROK_API_KEY", "") or GROK_API_KEY
    DEEPSEEK_API_KEY = (
        getattr(_secrets, "DEEPSEEK_API_KEY", "") or DEEPSEEK_API_KEY
    )
except ImportError:
    pass

# --- OCR (optional, used by screen vision) ---
USE_OCR = False
TESSERACT_CMD = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# --- Screen vision ---
# Language that "Orion translate this" translates screen text into. If the
# screen text is already in this language, the AI will note that instead.
TRANSLATE_TARGET_LANGUAGE = "English"

# --- HUD ---
SHOW_HUD = True              # small always-on-top status window

# --- Song recognition ---
# Seconds of audio Orion captures when asked to identify a song. Shazam can
# match from about 5 seconds; 8 gives a reliable result without a long wait.
# Capture and lookup only happen on demand, so this adds no idle CPU load.
SONG_LISTEN_SECONDS = 8

# --- Web search ---
# When True, Orion detects real-time questions ("latest news", "weather
# today", stock prices, scores, etc.) and asks the AI to look them up with
# its web search tool before answering. With Gemini this uses Google Search.
WEB_SEARCH = True

# --- Push-to-talk ---
# Optional global system hotkey that opens the mic immediately, skipping the
# wake word. Useful in noisy games or calls where the wake detector misses,
# or when saying the wake word out loud isn't convenient. The wake word
# still works in parallel -- this is purely an additional trigger.
# Hotkey syntax is parsed by the `keyboard` package (e.g. "ctrl+alt+space",
# "right alt", "f24"). Pick something Windows itself doesn't use:
# "ctrl+alt+space" is a safe default.
PUSH_TO_TALK = True
PUSH_TO_TALK_HOTKEY = "ctrl+alt+space"

# --- Thinking fillers ---
# When True, Orion says a short natural filler ("hmm", "let me check that",
# "one second") the moment it starts thinking, so there is no dead silence
# while it waits for the AI. The filler is queued first, then the real reply.
THINKING_FILLERS = True
# Categorised so Orion picks a filler that fits the kind of request, not a
# random one (a "let me check" sounded wrong when you asked it to open
# Spotify). Buckets:
#   "search" -- a web lookup is needed (real-time/news/weather queries)
#   "tool"   -- an imperative command ("open spotify", "set a timer")
#   "think"  -- an open question, chat, anything else
# If a bucket is empty, "think" is used as the fallback. A plain flat list
# is still accepted for back-compat and is treated as the "think" bucket.
THINKING_FILLER_PHRASES = {
    "search": [
        "Searching now.",
        "Looking that up.",
        "Checking the web.",
        "One moment, Sir.",
    ],
    "tool": [
        "On it.",
        "Doing that now.",
        "One second.",
        "Right away, Sir.",
    ],
    "think": [
        "Hmm.",
        "Let me think.",
        "Let me see.",
        "Give me a moment.",
        "One second.",
    ],
}

# --- Proactive alerts ---
# When True, Orion speaks up on its own about things worth knowing -- a CPU
# that's been running hot, a low battery, or the weather turning -- without
# being asked. Alerts are held back while Orion is muted or mid-conversation,
# and each kind has a cooldown so it never nags.
PROACTIVE_ALERTS = True
PROACTIVE_INTERVAL = 20          # seconds between background checks
PROACTIVE_COOLDOWN = 900         # min seconds before the same alert repeats
CPU_ALERT_PERCENT = 90           # CPU load (%) that counts as "running hot"
CPU_ALERT_SUSTAIN = 60           # seconds it must stay hot before Orion speaks
BATTERY_ALERT_PERCENT = 20       # battery level (%) that triggers a low alert
PROACTIVE_PERIPHERALS = True     # also watch wireless mouse/keyboard/headset
                                 # battery -- any Bluetooth device that reports
                                 # a level to Windows ("Sir, your mouse battery
                                 # is low")
PERIPHERAL_ALERT_PERCENT = 20    # peripheral battery level (%) that alerts
PERIPHERAL_POLL_SECONDS = 600    # how often to check peripherals (10 min)
PROACTIVE_WEATHER = True         # also announce when the weather changes
WEATHER_POLL_SECONDS = 1800      # how often to check the weather (30 min)

# --- Email reader (multi-account, read-only IMAP) ---
# Names of the email accounts Orion should read from. Each name MUST have a
# matching entry in secrets_local.EMAIL_CREDENTIALS with address, password
# (Gmail "app password" -- not your normal login password) and imap_host.
# Both of the user's accounts are Gmail / Google Workspace so imap.gmail.com
# is the right host for each. Leave empty (or remove the secret) to disable.
EMAIL_ACCOUNTS = ["personal", "work"]
# Max characters per email body fed to Gemini for summarising. Keeps long
# marketing emails from blowing up the prompt. 4000 covers any reasonable
# human-written message.
EMAIL_BODY_CAP = 4000
# When True, the daily briefing checks unread counts and offers to
# summarise: "You also have N unread, Sir -- would you like me to go
# through them?" -- the user answers yes/no via voice. (No proactive
# summarising without consent.)
EMAIL_IN_BRIEFING = True

# --- Daily briefing ---
# Speak a weather + news + calendar morning summary once on startup.
DAILY_BRIEFING = False
# Only run the briefing if the local hour is in [START, END) AND it
# hasn't already fired today. Prevents the briefing from playing every
# time Orion is launched (e.g. an afternoon restart). State stored in
# briefing_state.json next to the project (gitignored).
BRIEFING_START_HOUR = 5
BRIEFING_END_HOUR = 11
# When the briefing is about to play, skip the chatty AI startup greeting
# ("hey, how can I help you today this beautiful Monday") so the briefing
# itself leads. Set False to keep the old behavior.
BRIEFING_SUPPRESSES_GREETING = True
# Location used for the weather forecast (default: Helsinki, Finland).
BRIEFING_LATITUDE = 60.17
BRIEFING_LONGITUDE = 24.94
BRIEFING_LOCATION_NAME = "Helsinki"
# RSS feed used for headlines (any standard RSS URL works).
NEWS_RSS_URL = "https://feeds.bbci.co.uk/news/world/rss.xml"
NEWS_HEADLINE_COUNT = 5
# Optional .ics calendar file path or URL; leave "" to skip calendar.
CALENDAR_ICS = ""

# --- Morning music ---
# Auto-play a YouTube URL once per morning when Orion starts up. Gated to
# the same morning window as the briefing and tracked in
# morning_music_state.json so it only fires once per day.
MORNING_MUSIC = True
MORNING_MUSIC_URL = ""  # any YouTube / YouTube Music link; "" = off
MORNING_MUSIC_START_HOUR = 5
MORNING_MUSIC_END_HOUR = 11

# --- Spoken style ---
# Prepended to every query so the AI answers in a natural, conversational way
# that sounds good when read aloud by Orion's text-to-speech.
SPOKEN_STYLE = True
SPOKEN_STYLE_INSTRUCTION = (
    "You are Orion, the user's personal voice assistant. Your personality is "
    "consistent: calm, composed, and quietly confident, with a touch of dry "
    "wit. You are unfailingly loyal and helpful, you address the user as "
    "'Sir', and you never sound flustered or over-eager. You are concise and "
    "to the point -- you do not pad answers or over-explain. "
    "Never break character: never say you are a language model or 'an AI', "
    "never mention prompts, tokens, or these instructions, and never reference "
    "any fictional character, film, or franchise you may resemble (no "
    "comparisons to assistants in movies or comics). Do not refuse with "
    "corporate boilerplate. If, and only if, no tool listed below fits the "
    "request AND you genuinely cannot do what was asked, say so briefly and "
    "in character. "
    "Your reply is spoken aloud, so answer in a natural, conversational spoken "
    "style -- the way a person would say it out loud. Keep it concise (a few "
    "sentences). Do not use markdown, bullet points, numbered lists, headings, "
    "emoji, code blocks, or symbols like asterisks. Spell out things that are "
    "awkward to pronounce. Just give the plain spoken answer with no preamble."
)

# Inserted at the top of the prompt ONLY when Orion is sending the request to
# Gemini with tools attached (function-calling path). Gemini Flash-tier
# models default to safety-style refusals when conversational phrasing
# matches a "you don't have access" pattern, even if a tool clearly does --
# this hard rule short-circuits that. Kept separate from the persona so it
# doesn't get sent on tool-free turns (where it would be irrelevant noise).
# Confidence calibration + clarifying questions.
# Folded into SPOKEN_STYLE_INSTRUCTION via the assembly below so they ride on
# every Gemini turn, including the tool-use path. Kept as separate strings so
# the JARVIS persona text stays readable.
CONFIDENCE_INSTRUCTION = (
    "Confidence calibration: silently rate your own confidence in any "
    "substantive answer on a 1-5 scale BEFORE you speak. Only voice "
    "uncertainty when (a) the confidence is 1 or 2 AND (b) the answer is "
    "substantive (a real claim, a recommendation, a factual statement). "
    "Skip hedging entirely on local/factual trivia (time, date, weather, "
    "device state, tool results). NEVER pad with 'I'm not sure' or 'I "
    "think' on confident answers. If you ARE genuinely uncertain on "
    "something substantive, say so briefly and in character -- one short "
    "qualifier like 'I'm not fully sure, Sir, but...' -- then give your "
    "best answer. Never refuse just because you aren't 100% sure."
)
CLARIFY_INSTRUCTION = (
    "Clarifying questions: if a request is GENUINELY ambiguous in a way "
    "that would change your answer or action (different meanings, two "
    "different people/places with the same name, missing a piece of info "
    "you actually need), ask exactly ONE short clarifying question -- "
    "ten words or fewer, in character, addressing the user as Sir -- "
    "BEFORE answering or calling a tool. Examples: 'The city or the band, "
    "Sir?', 'Which Sam, Sir?', 'Today or tomorrow, Sir?'. Do NOT ask "
    "clarifiers when (a) you can make a reasonable default choice, (b) "
    "the ambiguity wouldn't change the result, or (c) the user clearly "
    "just wants you to act. Default to acting; clarify only when you "
    "would otherwise guess wrong."
)
# Glued on to SPOKEN_STYLE_INSTRUCTION at import time so they ride every turn.
SPOKEN_STYLE_INSTRUCTION = (
    SPOKEN_STYLE_INSTRUCTION + " " + CONFIDENCE_INSTRUCTION
    + " " + CLARIFY_INSTRUCTION
)

TOOL_USE_INSTRUCTION = (
    "[Tool use -- HARD RULE] You have tools listed in this request. If ANY "
    "listed tool can act on the user's request -- even partially, even if "
    "the phrasing is conversational -- you MUST call that tool. Never "
    "describe what you would do. Never claim you lack a capability that a "
    "listed tool provides. Never fabricate the result of an action you "
    "did not actually take. Only reply in plain text when NO listed tool "
    "fits. If two tools could fit, pick the closest; if unsure, prefer "
    "calling the most plausible tool over refusing."
)

# Added on top of TOOL_USE_INSTRUCTION when AGENTIC_TOOLS is on. Tells the
# model the two things that are only true in a multi-step loop: it will see
# what its tools returned, and one request can need several calls.
MULTI_STEP_INSTRUCTION = (
    "[Multi-step] You may call tools more than once. After a tool runs you "
    "will be shown its result and can then call another tool, so look "
    "something up first when you need to know exactly what to act on. When "
    "the user's request covers SEVERAL things -- 'cancel both reminders', "
    "'turn off the lights and lock the PC' -- issue one call per thing, all "
    "in the same reply. Never act on only the first of several. Stop calling "
    "tools and answer in plain text once the whole request is done."
)

# --- Voice / speaker verification ---
# When True, Orion identifies WHO is speaking on each captured phrase against a
# voice fingerprint you enrol once (run `python enroll_voice.py`). Two effects:
#   1. LOCK MODE -- a spoken toggle ("only listen to me" / "anyone can talk to
#      you") restricts commands to the owner only. The toggle itself is always
#      owner-gated, so a stranger can't unlock Orion just by saying the phrase.
#   2. PRIVACY GATE (always on) -- when a non-owner speaks, Orion's prompt to
#      Gemini OMITS the owner's persistent facts, the cross-session recap, and
#      the older portion of the live session transcript (everything before the
#      other person first joined this session). Nothing personal is sent, so
#      no prompt-injection can leak what isn't in the context.
# Falls back gracefully: if `resemblyzer` isn't installed or no voice profile
# has been enrolled, everyone is treated as the owner and Orion behaves as if
# this feature were off.
VOICE_VERIFICATION = True
# Cosine similarity required to accept a phrase as the owner's voice. Raise it
# if other people get through; lower it if your own voice fails to verify
# (after a cold, on a different mic, etc.).
VOICE_THRESHOLD = 0.70
# Files (relative to the Orion folder).
VOICE_PROFILE_FILE = "voice_profile.json"
VOICE_STATE_FILE = "voice_state.json"

# --- Mood awareness ---
# When True, Orion reads the user's transcribed speech for signs of stress or
# frustration (swears, "ugh", "come on", ALL CAPS, repeated "!!") and, if it
# spots them, ADAPTS its reply for that turn -- shorter, plainer, no JARVIS
# flourishes, no "Sir". Orion NEVER comments on the user's mood out loud. The
# detection is pure text heuristics: no audio analysis, no extra API call, no
# measurable cost. Set False to disable.
MOOD_AWARENESS = True

# --- Context awareness ---
# When True, Orion attaches the title of the window you currently have focused
# to each query, so it knows what you are doing without you having to explain.
# This is a single cheap local Win32 call -- no API call, no measurable CPU
# cost, nothing polled in the background.
CONTEXT_AWARENESS = True

# When True, the pending reminder list and today's agenda are attached to
# every tool-enabled prompt, each row carrying its id. The model can then act
# on exactly the right item -- "cancel both reminders at 8" becomes two
# cancel_reminder calls with two known ids instead of one guessed keyword.
# Both lists are read from local JSON, so this costs no API call; it only
# adds a few dozen prompt tokens. Owner-only, like every other personal block
# in the prompt.
LIVE_STATE_IN_PROMPT = True
# Cap on rows per list, so a long backlog can't crowd out the conversation.
LIVE_STATE_MAX_ITEMS = 10

# The user's message is a rough automatic transcript of speech and the user
# does not always pronounce words perfectly, so words may be misheard, run
# together, or slightly wrong. This note asks the AI to read for intent and
# answer the obvious meaning instead of getting tripped up by garbled words.
STT_TOLERANCE = True
STT_TOLERANCE_INSTRUCTION = (
    "The user's message is an automatic speech-to-text transcript and may "
    "contain misheard or mispronounced words, wrong homophones, or words run "
    "together. Interpret it by its most likely intended meaning rather than "
    "taking it literally. If the intent is reasonably clear, just answer it. "
    "Only ask the user to repeat themselves if the message is truly "
    "impossible to make sense of."
)

# --- Memory ---
# Session memory: prepend the running conversation transcript to every query
# so the AI keeps context across turns within a session.
SEND_SESSION_HISTORY = True
# Persistent memory: local file holding facts about the user, injected into
# every query. Relative to the Orion folder.
MEMORY_FILE = "memory.json"
# Auto-memory: after each conversational turn Orion quietly asks the AI to
# pull any durable facts about you out of what you said (your name, habits,
# preferences, important dates) and saves them to memory.json -- so its
# memory grows on its own without you ever saying "remember that". Those
# facts also feed a personalised spoken greeting on startup. Only used with
# Gemini; the extraction runs in the background so it never adds any delay.
AUTO_MEMORY = True

# --- Cross-session conversation memory ---
# Three-tier memory of past conversations on top of the persistent facts:
#   1. HOT RECAP -- a single ~150-word paragraph kept in conversation_recap.json
#      that summarises everything from past sessions. Injected into every
#      query so Orion has gist-level continuity. Tiny, so it costs nothing.
#   2. COLD ARCHIVE -- one entry per past session in conversation_archive.jsonl
#      (date + ~200-word summary + a local embedding vector). NEVER sent by
#      default; lives on disk only.
#   3. ON-DEMAND RECALL -- when the user refers to something older, a cheap
#      local embedding search picks the most relevant archived session and
#      slips just that one summary into the prompt for that turn.
# All Gemini work happens at session-end (one cheap summarisation call) or
# when a single session goes very long. The per-turn cost is the local
# embedding search (~10 ms, CPU only, shares the model used by SEMANTIC_INTENT).
CONVERSATION_RECAP = True
RECAP_FILE = "conversation_recap.json"
ARCHIVE_FILE = "conversation_archive.jsonl"
RECAP_WORD_BUDGET = 150           # max words in the rolling recap paragraph
ARCHIVE_SUMMARY_WORDS = 200       # target length of each archived session summary
RECAP_MAX_TURNS = 30              # user turns before mid-session compression
RECAP_KEEP_RECENT_TURNS = 6       # turns kept verbatim after a mid-session compress
ARCHIVE_RECALL_THRESHOLD = 0.55   # min cosine similarity to inject an old summary
RECAP_MIN_TURNS_TO_SAVE = 2       # skip session-end compression for trivial sessions

# --- Browser & app control (Group 6) ---
# JSON files (relative to the Orion folder) you customize yourself.
WORK_TABS_FILE = "work_tabs.json"   # list of URLs opened by "open work tabs"
APPS_FILE = "apps.json"             # {"app name": "C:\\path\\to\\app.exe"}
# Title substring used to find the Chrome window for tab commands.
CHROME_WINDOW_HINT = "chrome"

# --- PC control (Group 7) ---
# Roots crawled by "find the file" (in order). Drive letters are scanned too.
FILE_SEARCH_ROOTS = []              # empty = user profile + all fixed drives
FILE_SEARCH_TIME_BUDGET = 25        # max seconds spent crawling for a file
FILE_SEARCH_MAX_RESULTS = 5

# --- Productivity (Group 9) ---
TODO_FILE = "todo.json"             # local to-do list
REMINDERS_FILE = "reminders.json"   # persistent reminders
AGENDA_FILE = "agenda.json"         # todos + cached agenda state
GOOGLE_CALENDAR_ID = "primary"      # which Google calendar to use
# Mirror reminders onto Google Calendar so they reach your phone. Only ones
# landing on a LATER day are mirrored -- same-day reminders ("in 20 minutes")
# fire before you'd ever check a calendar. Recurring reminders stay local:
# calendar_sync has no RRULE support, so a series can't be represented.
# Orion still fires every reminder itself either way; this is only about
# whether a copy also shows up in Google Calendar.
REMINDER_CALENDAR_SYNC = True
END_OF_DAY_NUDGE = True             # speak up at night about early events
END_OF_DAY_HOUR = 21                # hour (24h) the nudge first fires
END_OF_DAY_EARLY_HOUR = 10          # events before this hour count as "early"
# "Sir" address used when drafting an email with no explicit recipient.
DEFAULT_EMAIL_RECIPIENT = ""

# --- Universal Hub ---
# Local web dashboard at http://localhost:HUB_PORT showing telemetry,
# agenda, health, debug audio, settings, and conversation history. Served
# by an in-process stdlib HTTP server (no extra deps). v1 is read-only;
# writes land in v2.
HUB_AUTOSTART = True           # launch the hub server at Orion boot
HUB_PORT = 8765                # localhost-only; pick anything free
HUB_OPEN_ON_START = False      # also pop the window open at boot
HUB_NATIVE_WINDOW = True       # use pywebview for a real native window
                               # (own taskbar entry, Orion icon). Falls
                               # back to the browser modes below if the
                               # pywebview import fails.
HUB_BROWSER_APP_MODE = True    # try Chrome/Edge --app= for a window
                               # without a URL bar; falls back to the
                               # default browser if neither is installed

# --- Smart home (Group 10) ---
# Home Assistant REST API. Leave URL blank to disable smart-home commands.
# Example URL: "http://homeassistant.local:8123"
HOME_ASSISTANT_URL = ""
HOME_ASSISTANT_TOKEN = ""           # long-lived access token
# Default entity ids targeted by generic "lights"/"plug"/"thermostat" commands.
HA_DEFAULT_LIGHT = "light.living_room"
HA_DEFAULT_SWITCH = "switch.smart_plug"
HA_DEFAULT_CLIMATE = "climate.thermostat"

# --- Scenes & routines (Group 11) ---
SCENES_FILE = "scenes.json"         # named routines, each a list of actions
# Window-title keywords closed instantly by "Orion panic".
PANIC_APPS = ["youtube", "netflix", "discord", "steam", "twitch", "reddit"]

# Pull smart-home secrets out of secrets_local.py if present.
try:
    import secrets_local as _secrets
    HOME_ASSISTANT_TOKEN = (
        getattr(_secrets, "HOME_ASSISTANT_TOKEN", "") or HOME_ASSISTANT_TOKEN
    )
except ImportError:
    pass

# Apply persisted Hub overrides (config_overrides.json). Always last, so
# user edits win over every default and every secrets_local fallback.
try:
    import config_overrides as _ov
    _ov.apply_to_module(__import__(__name__))
except Exception:
    pass
