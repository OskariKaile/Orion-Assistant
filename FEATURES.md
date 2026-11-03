# Orion — Features

A complete catalogue of what Orion can do, grouped by area. For setup and
install instructions see [README.md](README.md).

---

## Voice & Wake

- **Local wake word** — offline openWakeWord ONNX model listens for "Orion" with millisecond latency. ([wake.py](audio/wake.py))
- **Phrase-based fallback** — if no wake model is trained, Orion still
  triggers on the spoken phrase. ([wake.py](audio/wake.py))
- **Barge-in** — talk over Orion to cut it off mid-sentence. No wake word
  needed; saying "Orion" still works. ([ear.py](audio/ear.py))
- **Add to a command you already finished** — pause after "change the song",
  then add "and something calmer" a couple of seconds later. The answer
  already being worked on is thrown away and both halves run as one request
  instead of two contradictory ones. Speech arriving later than
  `MERGE_WINDOW`, or after the first command already acted, starts a fresh
  turn instead. ([ear.py](audio/ear.py))
- **Echo guard** — the open mic works on loudspeakers, not just headphones.
  Three layers keep Orion from hearing itself: a loudness bar that
  re-measures Orion's own leak into the mic at the start of every reply and
  so follows the volume knob, a check of what was heard against the words
  being spoken, and speaker verification when a voice profile is enrolled.
  ([ear.py](audio/ear.py))
- **Push-to-talk** — global hotkey (`ctrl+alt+space` by default) as an
  alternative to the wake word.
- **Follow-up listen** — after answering, Orion keeps the mic open briefly
  so you can reply without re-saying the wake word.
- **Thinking fillers** — says "on it" / "let me check that" instead of going
  silent, with the filler matched to the kind of request (search / command /
  open question). Pre-rendered at startup so they play instantly.

## Speech I/O

- **Local STT** — Moonshine ONNX (`moonshine-base`) transcribes on the CPU in
  ~100 ms per command; the GPU stays free for games.
  ([transcribe.py](audio/transcribe.py))
- **Neural TTS** — Piper produces a natural voice locally, synthesised
  sentence-by-sentence so the first sentence plays while the rest renders.
  One fixed gain per sentence (no per-chunk normalising), so no clipping
  crackle or volume jumps.
  ([piper_tts.py](audio/piper_tts.py), [tts.py](audio/tts.py))
- **Isolated audio playback** — playback runs in a separate persistent server
  process so native audio crashes can't kill Orion and the output stream
  stays warm. ([play_wav.py](audio/play_wav.py))
- **Spoken clock times** — times are spoken naturally ("half past ten",
  "quarter to eleven"), never digit-by-digit.
  ([clock_words.py](features/clock_words.py))

## Brain (LLM)

- **DeepSeek brain** — DeepSeek answers questions and drives function calling.
  ([brain.py](brain.py), [deepseek_bridge.py](llm/deepseek_bridge.py))
- **Model routing** — one switch (`LLM_BACKEND`) picks the brain. DeepSeek is
  text-only with no web search, so screenshots and real-time questions are
  handed to Gemini automatically; set `LLM_BACKEND = "gemini"` to put
  everything back on Gemini. ([llm_router.py](llm/llm_router.py))
- **Function calling** — the model picks the right built-in tool from natural
  phrasing — no hardcoded phrase lists. ([tools.py](tools.py))
- **Multi-step tool use** — tool results are fed back to the model so it can
  take another step: look up the reminders, then cancel the two you meant.
  A round of pure *action* tools ends the turn immediately, so "lights on"
  still costs one API call; only a *query* tool (`tools.QUERY_TOOLS`) sends
  it back for another pass. Capped by `MAX_TOOL_STEPS`; switch off with
  `AGENTIC_TOOLS = False`. ([orion.py](orion.py) `_ask_ai_tools`)
- **Live state in the prompt** — pending reminders (with their ids), the
  to-do list and the current date and time ride along on every tool-enabled
  prompt, so the model acts on the real item instead of guessing a keyword.
  Local JSON only — no API call. `LIVE_STATE_IN_PROMPT`.
  ([memory.py](memory.py) `_live_state_block`)
- **AI-first routing** — nearly every request goes to the model, which reads
  the whole sentence and picks the tool, so half-heard phrases no longer
  trigger the wrong local command. Only instant controls stay local: stop /
  mute / unmute (exact phrases), pause / resume / skip / previous track,
  volume, show / hide the HUD. `AI_FIRST = False` brings back the old local
  fast-paths (time, agenda, email check, music, song ID, diagnostics…).
  ([orion.py](orion.py), [commands.py](commands.py), [brain.py](brain.py))
- **Semantic intent matching** — local ONNX embedding model (MiniLM)
  matches utterances to intents by meaning, not keywords.
  ([semantic.py](llm/semantic.py))
- **Self-knowledge** — "what model are you", "how do you hear me", "what can
  you do" are answered from a single source of truth pulled live from config,
  so the model can't hallucinate its own stack. ([about.py](features/about.py))
- **MCP client** — at startup Orion launches the MCP servers listed in
  `config.MCP_SERVERS` (read-only filesystem, web fetch, optional Google
  Maps), discovers their tools and offers them to the model next to its own.
  Any standard MCP server plugs in without code changes.
  ([mcp_client.py](mcp_client.py))
- **Gemini and Grok bridges** — alternate model backends.
  ([gemini_bridge.py](llm/gemini_bridge.py),
  [grok_bridge.py](llm/grok_bridge.py))

## Memory

- **Persistent facts** — auto-learns durable facts about you and personalises
  the startup greeting. ([memory.py](memory.py), `memory.json`)
- **Session transcript** — full current conversation is prepended to every
  query.
- **Conversation recap** — ~150-word rolling gist of past sessions sent on
  every turn for cross-session continuity.
- **Searchable archive** — older sessions stored in
  `conversation_archive.jsonl`; local embedding search pulls only the
  relevant past summary when you reference something from weeks ago.
- **Manual "remember that..."** — explicit fact saving via voice.
- **"Forget everything"** — wipes saved facts on command.
- **Hub editing** — facts and the recap are viewable and editable in the
  Hub's Memory tab.

## Privacy & Speaker Verification

- **Voice enrolment** — record 3 clips to create a local voice fingerprint.
  ([enroll_voice.py](setup/enroll_voice.py), [voice_id.py](audio/voice_id.py))
- **Lock mode** — "only listen to me" / "open up" toggles owner-only mode.
  Toggle commands are always owner-gated.
- **Always-on privacy gate** — when a non-owner speaks, personal facts,
  recap, archive recall, active-window title, and earlier private session
  turns are stripped from the prompt before the model sees it, so there is
  nothing personal for it to leak.
- **Local-only data** — facts, recap, archive, agenda, voice profile, and API
  key are all git-ignored and never leave your machine.

## Mood

- **Passive mood detection** — picks up frustration cues in transcribed
  speech (swearing, "ugh", ALL CAPS). ([mood.py](llm/mood.py))
- **Adaptive tone** — drops flourishes for a short, direct reply
  when you sound frustrated. Never comments on your mood.

## Reminders & Agenda

- **Persistent reminders** — survive restarts; anything missed while Orion
  was off is announced once at boot. ([reminders.py](features/reminders.py))
- **Natural time parsing** — "in 20 minutes", "tomorrow morning", "tonight
  at 9", "friday at 5pm" are parsed locally; odd phrasings fall back to a
  one-shot model conversion.
- **Recurring reminders** — "every weekday at 8am", "every 30 minutes".
- **Held until you're back** — a reminder that comes due at an empty desk
  waits instead of being announced to the room, then arrives as one summary
  a few seconds after you return. An idle mouse alone does not count as
  absence: presence is read from four signals (input, workstation lock,
  whether anything is playing through the speakers, whether a window is
  fullscreen), so watching a film or listening to an album still counts as
  being there. Anything unreadable counts as present, and a hold gives up
  after `REMINDER_MAX_HOLD_MINUTES` so nothing is ever lost.
  ([presence.py](features/presence.py))
- **Snooze / list / cancel / clear** — all by voice, all owner-gated.
- **Cancel exactly what you meant** — reminders are cancelled by id, which
  the model reads off the prompt, so "remove both reminders at ten" takes
  out both even when their wording overlaps. Cancelling by spoken text still
  works and refuses to fire when a too-short match would sweep up several.
- **Unified agenda** — calendar events + reminders + todos presented as one
  system: "what's on for today / tomorrow / this week".
  ([agenda.py](features/agenda.py))
- **Google Calendar sync** — events created and rescheduled by voice land in
  your real Google Calendar (OAuth, phone sync included).
  ([calendar_sync.py](features/calendar_sync.py),
  [setup_calendar.py](setup/setup_calendar.py))
- **Reminders reach your phone** — a reminder set for a later day is mirrored
  onto Google Calendar, so it's not stranded on the PC. Same-day reminders
  stay local (they fire before you'd check a calendar) and recurring ones stay
  local too (no RRULE support yet). The agenda filters the mirrored copy back
  out, so nothing is listed twice. Off via `REMINDER_CALENDAR_SYNC`.
- **Smart classifier** — phrasing decides whether an item is an event, a
  reminder, or a todo ("coffee with Sam at 3" → event; "remind me to…" →
  reminder; no time → todo).
- **End-of-day nudge** — in the evening, if tomorrow starts early, Orion
  says so once ("dentist at 8am tomorrow — wind down").
  ([monitor.py](features/monitor.py))

## Proactive Monitor

- **CPU load alerts** — speaks up when CPU is pinned. ([monitor.py](features/monitor.py))
- **Battery warnings** — low laptop battery.
- **Peripheral battery** — low charge on wireless mice / keyboards /
  headsets via Windows PnP query.
- **Weather changes** — notable shifts announced unprompted.
- **Cooldowns + state-aware** — alerts held back while muted, listening,
  thinking, or speaking, so Orion never interrupts itself.

## Screen Vision

- **Explain screen** — describes what's on screen.
  ([vision.py](features/vision.py))
- **Read this** — OCR + speak.
- **Translate this** — on-screen translation.
- **Summarize this** — page/screen summarisation.
- **Screenshot** — saves a timestamped PNG to `Pictures\Screenshots` and
  copies it to the clipboard.

## Code Assistant

- **Read open editor file** — finds the file focused in VS Code / Cursor /
  PyCharm / Sublime / Notepad++ by parsing the title bar and sends the
  full source (not just visible portion) to the AI for review, fixes,
  explanation, refactoring, tests, or comments.
  ([code_assistant.py](llm/code_assistant.py))

## PC Control

- **Open folders** — Downloads, Documents, Desktop, Pictures, Music,
  Videos, plus any path. ([pc_control.py](features/pc_control.py))
- **Find files** — search across configured roots.
- **Lock PC**.
- **Empty Recycle Bin**.
- **Schedule shutdowns**.
- **Volume control** — system master volume.

## App Control

- **Open any app** — `apps.json` user-defined launch paths.
  ([browser_control.py](features/browser_control.py))
- **Smart app discovery** — when an app isn't in `apps.json`, searches:
  Start Menu .lnk shortcuts → registry uninstall keys → game launchers
  (Steam library across drives, Battle.net, Epic) → time-budgeted
  filesystem walk. Best fuzzy match is cached for next time.
  ([app_finder.py](features/app_finder.py))

## Browser

- **Open work tabs** — predefined tab bundle from `work_tabs.json`.
- **Google search** — by voice.
- **Navigate to site**.
- **Close current Chrome tab**.
- **Close all tabs**.
- **Save current tab**.

## Media

- **Play / pause / next / previous** — sends system media keys, works with
  any media app. ([media_control.py](features/media_control.py))
- **Open Spotify / YouTube / Netflix**.
- **Play YouTube by voice** — searches via `youtube-search-python`, opens
  a real watch URL in a background Chrome tab without stealing focus.
  ([youtube_player.py](features/youtube_player.py))
- **Song recognition** — captures system loopback + mic, identifies via
  Shazam in an isolated subprocess, copies "Artist - Title" to the clipboard.
  ([song.py](audio/song.py), [recognize_song.py](audio/recognize_song.py))
- **Morning music** — auto-plays your chosen YouTube playlist once per
  morning on startup.

## Productivity

- **Background timers** — "set a timer for 5 minutes".
  ([productivity_tools.py](features/productivity_tools.py))
- **To-do list** — add / read / complete items by voice, stored in
  `agenda.json` as part of the unified agenda.
- **Current time / date** — "what time is it?" answered locally without
  hitting the LLM.
- **Email reading** — multi-account IMAP, read-only by design (only FETCH
  / SEARCH, never STORE / DELETE / APPEND). Works with Gmail app passwords
  and any IMAP provider. ([email_reader.py](features/email_reader.py))
- **List unread senders** — "who emailed me?" reports recent unread senders.
- **Email summaries** — picks an email by sender/subject and reads a short
  summary aloud.
- **Email drafting** — composes a draft email by voice (send still manual).

## Daily Briefing

- **Weather** — Open-Meteo, no API key required. ([briefing.py](features/briefing.py))
- **News headlines** — any RSS feed (default BBC World).
- **Agenda** — today's events, reminders, and todo count, folded into one
  spoken summary.
- **Morning-gated** — fires once per day inside the configured morning
  window, not on every restart.
- **All fail-soft** — a missing source never blocks startup.

## Smart Home

- **Home Assistant integration** — REST API over local network.
  ([smart_home.py](features/smart_home.py))
- **Lights** — on / off / brightness / named colours (red, blue, warm,
  purple, etc.).
- **Smart plugs / switches**.
- **Thermostat** — set temperature.
- **All devices off**.

## Scenes & Routines

- **Named scene playback** — sequences of actions defined in `scenes.json`.
  Supported steps: open_app, open_url, open_work_tabs, google, lights_on,
  lights_off, brightness, switch, set_temperature, all_devices_off,
  volume, media, lock_pc, wait. ([scenes.py](features/scenes.py))
- **"Orion panic"** — instantly closes every window matching the
  configured panic-apps keyword list.

## Universal Hub (dashboard)

Local web dashboard at `http://localhost:8765/` — open it from the tray
("Open Hub") or by voice ("open the hub"). ([hub_server.py](ui/hub_server.py),
[hub/](hub/index.html))

- **Dashboard** — live state, quick actions (mute / lock voice / new
  conversation / **restart Orion**), today's stats, last turn.
- **Telemetry** — per-turn latency split into stages (listen / STT / LLM /
  action / synth / speaking), charts per day, token usage, filterable
  turns table. ([telemetry.py](ui/telemetry.py))
- **Agenda** — events, reminders, todos with add / complete / snooze /
  cancel controls.
- **Voice Debug** — replay captured phrases and TTS output inline.
  ([replay_debug.py](ui/replay_debug.py), [tts_debug.py](ui/tts_debug.py))
- **Health** — mic / speakers / wake model / voice profile / AI backend /
  email / calendar checks plus PC vitals, mic test, one-click voice
  enrolment and calendar reconnect. ([health_check.py](ui/health_check.py))
- **Skills** — enable/disable individual tools live.
- **Settings** — edit config keys from the browser; hot keys apply
  instantly, the rest after a restart.
- **Memory / Conversation** — view and edit facts, recap, and the session
  archive.
- **Logs** — everything written to stdout/stderr plus uncaught errors,
  captured in memory even under pythonw (where it's otherwise lost).
  ([logbuf.py](ui/logbuf.py))
- **Keyboard shortcuts** — `1`–`9`/`0` switch tabs, `/` jumps to the
  telemetry search box.
- **Live updates** — server-sent events push state changes to every open
  tab; the window closes itself when Orion exits and reloads itself after
  a restart.

## UI

- **System tray icon** — state-coloured dot, mute, new conversation, open
  hub, restart, exit.
- **Floating HUD** — glowing status pill: 🔵 Idle 🟢 Listening 🟡 Transcribing
  🟠 Thinking 🟣 Preparing 🔷 Speaking ⚪ Muted. Toggle with `SHOW_HUD`.
  Each tool the model calls pops up as a chip above the pill
  (`fn set_timer(minutes=20)`) with a spinner that turns into a ✓
  (`HUD_TOOL_CHIPS`). ([hud.py](ui/hud.py))

## Diagnostics

- **Health check by voice** — "system check" / "run diagnostics" reports
  mic, speakers, wake model, voice profile, AI backend reachability, email,
  and PC disk/RAM/CPU/battery. ([health_check.py](ui/health_check.py))
- **Telemetry** — one local JSONL row per command with 5-stage timings and
  token counts; rendered in the Hub or via `python ui/orion_stats.py`.
- **Replay debugger** — every captured phrase saved as WAV for STT
  debugging (`python ui/replay_debug.py list`).
- **TTS debugger** — every synthesised sentence saved as WAV for voice
  debugging (`python ui/tts_debug.py list`).
- **Offline test suite** — `python -m unittest discover -s tests` covers the
  local matchers, time parsing, agenda classification, mood detection, and
  spoken clock times. No mic, network, or models needed.

## Voice Commands

Say **"Orion"**, then your request — phrasing is flexible.

| Say | Action |
|-----|--------|
| `Orion, <question>` | Answer via the AI model |
| `Orion, stop` | Stop speaking |
| `Orion, mute` / `unmute` | Toggle voice output |
| `Orion, new conversation` | Clear conversation |
| `Orion, remember that ...` | Save a fact |
| `Orion, forget everything about me` | Wipe facts |
| `Orion, what's on for tomorrow?` | Unified agenda summary |
| `Orion, remind me to call mom at 3pm` | Set a reminder |
| `Orion, what are my reminders?` | List pending reminders |
| `Orion, add eggs to my list` | Add a todo |
| `Orion, what song is this?` | Identify the music playing |
| `Orion, play some lofi` | Play music via YouTube |
| `Orion, check my email` | Unread count by account |
| `Orion, explain my screen` | Describe screen |
| `Orion, read this` / `translate this` / `summarize this` | Screen vision |
| `Orion, take a screenshot` | Save + copy screenshot |
| `Orion, set a timer for 5 minutes` | Background timer |
| `Orion, open the hub` | Open the dashboard |
| `Orion, run diagnostics` | Orion + PC health check |
| `Orion, only listen to me` / `lock down` | Owner-only mode |
| `Orion, anyone can talk to you` / `open up` | Open mode |
| `Orion, what model are you?` | Describe its own stack |
| `Orion, exit` | Close Orion |
