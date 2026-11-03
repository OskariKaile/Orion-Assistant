"""Orion's function-calling tool layer.

Instead of hand-written phrase lists, Orion's real capabilities are declared
here as Gemini function declarations. Gemini reads natural language and picks
the tool itself -- so "kill the lights and remind me about the call in ten"
just works without every phrasing being enumerated in advance.

Each TOOLS entry has:
  - name / description / parameters : the schema Gemini sees.
  - run(args, orion)               : carries the action out. Returns a string
                                     for Orion to speak, or None if the
                                     handler already spoke for itself.
"""

import browser_control
import email_reader
import health_check
import media_control
import pc_control
import productivity_tools
import scenes
import smart_home
import song
import youtube_player


# --- run handlers -----------------------------------------------------------
# Each takes the argument dict Gemini produced plus the Orion instance.

def _is_owner(orion):
    """True when the speaker passed Orion's voice-verification gate."""
    return getattr(orion, "_last_speaker", "owner") == "owner"


def _check_email(args, orion):
    """Report unread email counts. Owner-only."""
    if not _is_owner(orion):
        return "I can't share email details, Sir."
    if not email_reader.available():
        return ("No email accounts are set up yet. Add credentials in "
                "secrets_local.py first.")
    results, errors = email_reader.unread_summary()
    requested = (args.get("account") or "").strip().lower() or None

    if requested:
        match = next((k for k in results if k.lower() == requested), None)
        if match is None:
            return f"I don't have an email account called {requested}, Sir."
        msgs = results.get(match, [])
        if not msgs:
            return f"Nothing unread on {match}, Sir."
        n = len(msgs)
        word = "email" if n == 1 else "emails"
        return f"{n} unread {word} on {match}, Sir."

    total = sum(len(v) for v in results.values())
    if total == 0:
        return "Your inbox is clear, Sir. Nothing unread."
    per_acc = [f"{len(v)} on {k}" for k, v in results.items() if v]
    word = "email" if total == 1 else "emails"
    return f"{total} unread {word}, Sir: {', '.join(per_acc)}."


def _who_emailed(args, orion):
    """Read out senders/subjects of recent unread mail. Owner-only."""
    if not _is_owner(orion):
        return "I can't share email details, Sir."
    if not email_reader.available():
        return ("No email accounts are set up yet. Add credentials in "
                "secrets_local.py first.")
    results, _ = email_reader.unread_summary()
    limit = int(args.get("limit") or 5)
    requested = (args.get("account") or "").strip().lower() or None

    lines = []
    for acc, msgs in results.items():
        if requested and acc.lower() != requested:
            continue
        for env in msgs:
            lines.append((acc, env))
    if not lines:
        if requested:
            return f"Nothing unread on {requested}, Sir."
        return "Nothing unread, Sir."
    lines = lines[:limit]
    out = []
    for acc, env in lines:
        tag = f"on {acc}" if not requested else ""
        sender = env["sender"] or "Unknown sender"
        subj = env["subject"]
        out.append(f"From {sender}{', ' + tag if tag else ''}: {subj}")
    return ". ".join(out) + "."


def _summarize_email(args, orion):
    """Fetch unread email bodies and ask Gemini for a short spoken summary.
    Owner-only. Honors optional `account` and `sender` filters."""
    if not _is_owner(orion):
        return "I can't share email details, Sir."
    if not email_reader.available():
        return ("No email accounts are set up yet. Add credentials in "
                "secrets_local.py first.")
    requested_account = (args.get("account") or "").strip().lower() or None
    requested_sender = (args.get("sender") or "").strip() or None
    limit = max(1, min(int(args.get("limit") or 5), 10))

    # Pick the envelopes to summarise.
    envelopes = []  # list of (account, env)
    if requested_sender:
        for env in email_reader.find_by_sender(requested_sender, limit=limit):
            envelopes.append((env["account"], env))
    else:
        results, _ = email_reader.unread_summary()
        for acc, msgs in results.items():
            if requested_account and acc.lower() != requested_account:
                continue
            for env in msgs:
                envelopes.append((acc, env))
        envelopes = envelopes[:limit]

    if not envelopes:
        if requested_sender:
            return f"I couldn't find anything from {requested_sender}, Sir."
        return "Nothing to summarise, Sir."

    # Pull bodies (capped) and hand to Gemini.
    chunks = ["[Emails to summarise for the user]", ""]
    for acc, env in envelopes:
        body = email_reader.fetch_body(acc, env["uid"])
        chunks.append(f"--- {acc}: from {env['sender']} -- "
                       f"subject: {env['subject']} ---")
        chunks.append(body or "(no readable body)")
        chunks.append("")
    chunks.append(
        "You are Orion, a JARVIS-style voice assistant. Give a brief spoken "
        "summary of the emails above. Group similar ones if it helps. Stay "
        "under 120 words, natural and conversational, no markdown, no "
        "bullet points. Address the user as Sir. Skip any obvious "
        "marketing or automated mail.")
    prompt = "\n".join(chunks)

    try:
        import llm_router
        reply = llm_router.ask_once(prompt)
    except Exception:
        reply = ""
    return reply or "I read them but couldn't put a summary together, Sir."


def _open(args, orion):
    name = (args.get("name") or "").strip()
    if not name:
        return "What would you like me to open?"
    media = media_control.open_media(name)
    if media is not None:
        return media
    if "folder" in name.lower():
        return pc_control.open_folder(name)
    return browser_control.open_app(name, orion)


def _draft_email(args, orion):
    recipient = (args.get("recipient") or "").strip()
    topic = (args.get("topic") or "").strip()
    if recipient and topic:
        arg = f"to {recipient} about {topic}"
    elif topic:
        arg = f"about {topic}"
    elif recipient:
        arg = f"to {recipient}"
    else:
        arg = ""
    orion.draft_email(arg)  # speaks its own confirmation
    return None


def _timer(args, orion):
    return productivity_tools.start_timer(
        args.get("minutes"), args.get("label", ""), orion.speaker.say)


def _reminder(args, orion):
    if not _is_owner(orion):
        return "Only the owner can set reminders, Sir."
    return productivity_tools.set_reminder(
        minutes=args.get("minutes"),
        text=args.get("task", ""),
        speak=orion.speaker.say,
        when=args.get("when", ""))


def _list_reminders(args, orion):
    if not _is_owner(orion):
        return "Only the owner can see reminders, Sir."
    import reminders
    return reminders.spoken_list()


def _cancel_reminder(args, orion):
    if not _is_owner(orion):
        return "Only the owner can cancel reminders, Sir."
    import reminders
    return reminders.cancel_matching(args.get("text", ""),
                                     rid=args.get("id", ""))


def _clear_reminders(args, orion):
    if not _is_owner(orion):
        return "Only the owner can clear reminders, Sir."
    import reminders
    return reminders.clear_all()


def _snooze_reminder(args, orion):
    if not _is_owner(orion):
        return "Only the owner can snooze reminders, Sir."
    import reminders
    mins = args.get("minutes") or 10
    try:
        mins = int(float(mins))
    except (TypeError, ValueError):
        mins = 10
    return reminders.snooze(mins)


def _lights_on(args, orion):
    return smart_home.lights_on(
        brightness=args.get("brightness"), color=args.get("color"))


def _identify_song(args, orion):
    # Tell the user Orion is listening BEFORE recording starts -- say_blocking
    # makes sure Orion's own voice has finished so it doesn't end up in the
    # loopback or microphone capture.
    if orion is not None:
        orion.set_state("thinking")
        orion.speaker.say_blocking("Listening now, Sir.")
    return song.identify(orion)


def _get_time(args, orion):
    if orion is None:
        return None
    query = "date" if args.get("include_date") else ""
    orion._tell_time(query)  # speaks its own answer
    return None


def _read_todos():
    import agenda
    items = agenda.todos_list()
    if not items:
        return "Your to-do list is empty, Sir."
    n = len(items)
    listed = "; ".join(f"{i}, {t}" for i, t in enumerate(items, 1))
    return f"You have {n} item{'s' if n != 1 else ''}: {listed}."


def _intent(name):
    """Wrap a built-in Orion intent (screen / control actions) as a tool run."""
    def run(args, orion):
        orion._run_intent(name)
        return None
    return run


# --- tool registry ----------------------------------------------------------

TOOLS = [
    # Apps, browser, web
    {
        "name": "open",
        "description": "Open an app, website, folder, or media service "
                       "(Spotify, YouTube, Netflix) by name.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "What to open, e.g. 'spotify', "
                                        "'github.com', 'downloads folder'."},
            },
            "required": ["name"],
        },
        "run": _open,
    },
    {
        "name": "google_search",
        "description": "Search Google in the browser for a query.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search text."},
            },
            "required": ["query"],
        },
        "run": lambda a, o: browser_control.google_search(a.get("query", "")),
    },
    {
        "name": "go_to_site",
        "description": "Navigate the browser to a specific website.",
        "parameters": {
            "type": "object",
            "properties": {
                "site": {"type": "string",
                         "description": "Site name or URL."},
            },
            "required": ["site"],
        },
        "run": lambda a, o: browser_control.go_to_site(a.get("site", "")),
    },
    {
        "name": "open_work_tabs",
        "description": "Open the user's saved set of work browser tabs.",
        "parameters": {"type": "object", "properties": {}},
        "run": lambda a, o: browser_control.open_work_tabs(),
    },
    {
        "name": "close_all_tabs",
        "description": "Close every open browser tab.",
        "parameters": {"type": "object", "properties": {}},
        "run": lambda a, o: browser_control.close_all_tabs(),
    },
    {
        "name": "play_youtube",
        "description": (
            "ALWAYS call this for any spoken request to play, queue, put "
            "on, or start music, audio, or a video -- e.g. 'play some "
            "music', 'put on something motivational', 'queue up focus "
            "music', 'play that song by X', 'hey can you play music for "
            "me'. This tool DOES open YouTube and start playback. NEVER "
            "claim you cannot play music, NEVER fabricate that you "
            "created a playlist, NEVER describe what you would play "
            "instead of playing it. Pick a concrete YouTube search query "
            "that matches the user's mood/genre and call the tool."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "The YouTube search query. Should describe a real "
                        "video to play -- e.g. 'lofi study mix', "
                        "'motivational music workout', a song title, an "
                        "artist name. Don't just echo back the user's "
                        "request verbatim."
                    ),
                },
            },
            "required": ["query"],
        },
        "run": lambda a, o: youtube_player.play(a.get("query", "")),
    },
    {
        "name": "save_work_tab",
        "description": "Save the current browser tab to the work-tabs set.",
        "parameters": {"type": "object", "properties": {}},
        "run": lambda a, o: browser_control.save_work_tab(),
    },

    # Email (read-only, owner-only)
    {
        "name": "check_email",
        "description": (
            "ALWAYS call this for any question that mentions email, mail, "
            "inbox, or unread messages -- including conversational "
            "phrasings like 'do I have any new emails on work', 'hey is "
            "there anything in my inbox', 'check my mail for me'. This "
            "tool can read the user's accounts; you DO have email access "
            "through it. NEVER claim you cannot check email, NEVER offer "
            "to draft email when the user only asked whether they have "
            "any. Optional `account` filters to one mailbox by its "
            "friendly name (e.g. 'personal', 'work')."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "account": {
                    "type": "string",
                    "description": ("Optional account name to filter to "
                                     "(e.g. 'personal', 'work'). Leave "
                                     "empty to count across all accounts."),
                },
            },
        },
        "run": _check_email,
    },
    {
        "name": "list_unread_email",
        "description": (
            "ALWAYS call this for 'who emailed me', 'what's in my inbox', "
            "'show me my unread', 'list my new email'. Returns sender + "
            "subject only, never the body. NEVER claim you cannot see "
            "the user's inbox -- this tool reads it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "account": {
                    "type": "string",
                    "description": "Optional account-name filter.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max messages to list (default 5).",
                },
            },
        },
        "run": _who_emailed,
    },
    {
        "name": "summarize_email",
        "description": (
            "ALWAYS call this for 'summarise my email', 'summarise the "
            "latest email', 'what does that email from <person> say', "
            "'read me the email about X'. This tool actually reads the "
            "user's mail and condenses it. NEVER claim you cannot read "
            "the user's email. Optional `account` and `sender` filters."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "account": {
                    "type": "string",
                    "description": "Optional account-name filter.",
                },
                "sender": {
                    "type": "string",
                    "description": ("Optional sender filter -- full or "
                                     "partial name/address, case-"
                                     "insensitive. Searches all mail, not "
                                     "just unread."),
                },
                "limit": {
                    "type": "integer",
                    "description": "Max emails to summarise (default 5).",
                },
            },
        },
        "run": _summarize_email,
    },

    # Media playback
    {
        "name": "media_play_pause",
        "description": "Toggle play/pause for whatever media is playing.",
        "parameters": {"type": "object", "properties": {}},
        "run": lambda a, o: media_control.play_pause(),
    },
    {
        "name": "media_next",
        "description": "Skip to the next track or song.",
        "parameters": {"type": "object", "properties": {}},
        "run": lambda a, o: media_control.next_track(),
    },
    {
        "name": "media_previous",
        "description": "Go back to the previous track or song.",
        "parameters": {"type": "object", "properties": {}},
        "run": lambda a, o: media_control.previous_track(),
    },

    # PC control
    {
        "name": "lock_pc",
        "description": "Lock the computer.",
        "parameters": {"type": "object", "properties": {}},
        "run": lambda a, o: pc_control.lock_pc(),
    },
    {
        "name": "empty_trash",
        "description": "Empty the Windows recycle bin.",
        "parameters": {"type": "object", "properties": {}},
        "run": lambda a, o: pc_control.empty_trash(),
    },
    {
        "name": "shutdown_in",
        "description": "Schedule the PC to shut down after a number of minutes.",
        "parameters": {
            "type": "object",
            "properties": {
                "minutes": {"type": "number",
                            "description": "Minutes from now to shut down."},
            },
            "required": ["minutes"],
        },
        "run": lambda a, o: pc_control.shutdown_in(int(a.get("minutes", 0))),
    },
    {
        "name": "cancel_shutdown",
        "description": "Cancel a scheduled PC shutdown.",
        "parameters": {"type": "object", "properties": {}},
        "run": lambda a, o: pc_control.cancel_shutdown(),
    },
    {
        "name": "find_file",
        "description": "Search the computer for a file by name.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "File name or part of it."},
            },
            "required": ["name"],
        },
        "run": lambda a, o: pc_control.find_file(a.get("name", "")),
    },
    {
        "name": "open_folder",
        "description": "Open a folder in File Explorer.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "Folder name, e.g. 'downloads'."},
            },
            "required": ["name"],
        },
        "run": lambda a, o: pc_control.open_folder(a.get("name", "")),
    },
    {
        "name": "set_volume",
        "description": "Set the system volume to a level from 0 to 100.",
        "parameters": {
            "type": "object",
            "properties": {
                "level": {"type": "number",
                          "description": "Volume level 0-100."},
            },
            "required": ["level"],
        },
        "run": lambda a, o: pc_control.set_volume(int(a.get("level", 0))),
    },
    {
        "name": "change_volume",
        "description": "Nudge the system volume up or down a step.",
        "parameters": {
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": ["up", "down"],
                              "description": "Which way to change volume."},
            },
            "required": ["direction"],
        },
        "run": lambda a, o: pc_control.nudge_volume(
            a.get("direction") == "up"),
    },

    # Productivity
    {
        "name": "set_timer",
        "description": "Start a countdown timer.",
        "parameters": {
            "type": "object",
            "properties": {
                "minutes": {"type": "number",
                            "description": "Timer length in minutes."},
                "label": {"type": "string",
                          "description": "Optional name for the timer."},
            },
            "required": ["minutes"],
        },
        "run": _timer,
    },
    {
        "name": "set_reminder",
        "description": (
            "Set a persistent reminder. CRITICAL: `task` is WHAT to be "
            "reminded about (the action/subject -- e.g. 'watch the "
            "starship launch', 'call mom', 'take vitamins'). `when` is "
            "WHEN to fire (the time phrase -- e.g. 'today at 9pm', "
            "'tomorrow morning', 'in 20 minutes', 'every weekday at "
            "8am'). NEVER put a time word like 'today', 'tomorrow', "
            "'at 3pm' into `task`. NEVER put the action into `when`. "
            "Example: 'remind me today at 9pm to watch the starship "
            "flight' -> task='watch the starship flight', when='today "
            "at 9pm'. Example: 'remind me to call mom in 20 minutes' "
            "-> task='call mom', when='in 20 minutes'. Reminders "
            "survive Orion restarts."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {"type": "string",
                         "description": ("WHAT to be reminded about -- the "
                                          "action or subject, never a time "
                                          "word. e.g. 'watch the starship "
                                          "launch', 'call mom'.")},
                "when": {"type": "string",
                         "description": ("WHEN to fire -- a time phrase "
                                          "only, never the action. e.g. "
                                          "'today at 9pm', 'tomorrow "
                                          "morning', 'in 20 minutes', "
                                          "'every weekday at 8am'.")},
                "minutes": {"type": "number",
                            "description": ("Legacy fallback: minutes from "
                                             "now. Prefer `when`.")},
            },
            "required": ["task"],
        },
        "run": _reminder,
    },
    {
        "name": "list_reminders",
        "description": ("List the user's pending reminders. Use when they "
                         "ask what their reminders are, what's coming up, "
                         "or 'do I have any reminders'."),
        "parameters": {"type": "object", "properties": {}},
        "run": _list_reminders,
    },
    {
        "name": "cancel_reminder",
        "description": ("Cancel ONE pending reminder. Prefer `id` -- the "
                         "pending reminders are listed with their ids in "
                         "this prompt, and an id cancels exactly that one "
                         "with no ambiguity. Fall back to `text` only when "
                         "you have no id. To cancel several reminders, make "
                         "one call per reminder in the same reply; to cancel "
                         "every one, use clear_reminders instead."),
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string",
                       "description": ("Exact reminder id, e.g. 'r_1a89'. "
                                        "Cancels only that reminder.")},
                "text": {"type": "string",
                         "description": "Words from the reminder to cancel."},
            },
        },
        "run": _cancel_reminder,
    },
    {
        "name": "clear_reminders",
        "description": ("Delete every pending reminder. Use only for "
                         "'cancel all my reminders', 'clear reminders'."),
        "parameters": {"type": "object", "properties": {}},
        "run": _clear_reminders,
    },
    {
        "name": "snooze_reminder",
        "description": ("Snooze the most-recently-fired reminder by N "
                         "minutes. Use for 'snooze that', 'snooze 10 "
                         "minutes', 'remind me again in a bit'."),
        "parameters": {
            "type": "object",
            "properties": {
                "minutes": {"type": "number",
                            "description": "Snooze length in minutes "
                                           "(default 10)."},
            },
        },
        "run": _snooze_reminder,
    },
    {
        "name": "get_time",
        "description": "Tell the user the current time (and the date, if "
                       "asked) from the PC clock. Use this whenever the user "
                       "asks what time it is, what the date or day is, or "
                       "anything about the current time.",
        "parameters": {
            "type": "object",
            "properties": {
                "include_date": {
                    "type": "boolean",
                    "description": "True if the user also wants the date or "
                                   "day of the week, not just the time.",
                },
            },
        },
        "run": _get_time,
    },
    {
        "name": "calendar_summary",
        "description": "Read out today's calendar events.",
        "parameters": {"type": "object", "properties": {}},
        "run": lambda a, o: productivity_tools.calendar_summary(),
    },
    {
        "name": "add_todo",
        "description": "Add an item to the to-do list.",
        "parameters": {
            "type": "object",
            "properties": {
                "item": {"type": "string",
                         "description": "The to-do item text."},
            },
            "required": ["item"],
        },
        "run": lambda a, o: __import__("agenda").todos_add(
            a.get("item", "")),
    },
    {
        "name": "read_todos",
        "description": "Read out the current to-do list.",
        "parameters": {"type": "object", "properties": {}},
        "run": lambda a, o: _read_todos(),
    },
    {
        "name": "clear_todos",
        "description": "Clear the entire to-do list.",
        "parameters": {"type": "object", "properties": {}},
        "run": lambda a, o: __import__("agenda").todos_clear(),
    },
    {
        "name": "draft_email",
        "description": "Write an email and open it in the mail client.",
        "parameters": {
            "type": "object",
            "properties": {
                "recipient": {"type": "string",
                              "description": "Who the email is to."},
                "topic": {"type": "string",
                          "description": "What the email is about."},
            },
        },
        "run": _draft_email,
    },

    # Smart home
    {
        "name": "lights_on",
        "description": "Turn the lights on, optionally with brightness/color.",
        "parameters": {
            "type": "object",
            "properties": {
                "brightness": {"type": "number",
                               "description": "Brightness 0-100."},
                "color": {"type": "string",
                          "description": "Optional color name."},
            },
        },
        "run": _lights_on,
    },
    {
        "name": "lights_off",
        "description": "Turn the lights off.",
        "parameters": {"type": "object", "properties": {}},
        "run": lambda a, o: smart_home.lights_off(),
    },
    {
        "name": "set_brightness",
        "description": "Set the lights to a brightness from 0 to 100.",
        "parameters": {
            "type": "object",
            "properties": {
                "level": {"type": "number",
                          "description": "Brightness 0-100."},
            },
            "required": ["level"],
        },
        "run": lambda a, o: smart_home.set_brightness(a.get("level")),
    },
    {
        "name": "set_temperature",
        "description": "Set the thermostat to a temperature in degrees.",
        "parameters": {
            "type": "object",
            "properties": {
                "degrees": {"type": "number",
                            "description": "Target temperature."},
            },
            "required": ["degrees"],
        },
        "run": lambda a, o: smart_home.set_temperature(a.get("degrees")),
    },
    {
        "name": "switch_plug",
        "description": "Turn the smart plug on or off.",
        "parameters": {
            "type": "object",
            "properties": {
                "on": {"type": "boolean",
                       "description": "True to turn on, False to turn off."},
            },
            "required": ["on"],
        },
        "run": lambda a, o: smart_home.switch(bool(a.get("on"))),
    },

    # Scenes
    {
        "name": "run_scene",
        "description": "Run a named scene or routine.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "Scene name to run."},
            },
            "required": ["name"],
        },
        "run": lambda a, o: scenes.run_scene(a.get("name", "")),
    },
    {
        "name": "panic",
        "description": "Instantly close distracting apps (the panic routine).",
        "parameters": {"type": "object", "properties": {}},
        "run": lambda a, o: scenes.panic(),
    },

    # Music
    {
        "name": "identify_song",
        "description": "Identify the song or music currently playing -- "
                       "whether it's coming from a game, a video, a call, or "
                       "playing out loud in the room -- and say what it is. "
                       "Copies the artist and title to the clipboard. Use "
                       "this whenever the user asks what song this is, what's "
                       "playing, or to name a track.",
        "parameters": {"type": "object", "properties": {}},
        "run": _identify_song,
    },

    # Screen vision
    {
        "name": "explain_screen",
        "description": "Look at the screen and describe what is on it.",
        "parameters": {"type": "object", "properties": {}},
        "run": _intent("explain"),
    },
    {
        "name": "read_screen",
        "description": "Read the text currently on the screen out loud.",
        "parameters": {"type": "object", "properties": {}},
        "run": _intent("read"),
    },
    {
        "name": "summarize_screen",
        "description": "Summarize the text currently on the screen.",
        "parameters": {"type": "object", "properties": {}},
        "run": _intent("summarize"),
    },
    {
        "name": "translate_screen",
        "description": "Translate the text on the screen.",
        "parameters": {"type": "object", "properties": {}},
        "run": _intent("translate"),
    },
    {
        "name": "take_screenshot",
        "description": "Take a screenshot and copy it to the clipboard.",
        "parameters": {"type": "object", "properties": {}},
        "run": _intent("screenshot"),
    },

    # Agenda (unified calendar + reminders + todos)
    {
        "name": "list_agenda",
        "description": (
            "Read out the user's agenda for a given time range: calendar "
            "events + reminders + todos, merged chronologically. Use for "
            "'what's on for today', 'what's my schedule tomorrow', "
            "'what do I have this week', 'anything coming up'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "when": {"type": "string",
                          "description": ("'today', 'tomorrow', "
                                           "'this week', 'next week', "
                                           "'tonight'. Defaults to today.")},
            },
        },
        "run": lambda a, o: (
            __import__("agenda").spoken_summary(a.get("when", "today"))
            if _is_owner(o)
            else "Only the owner can see the agenda, Sir."),
    },
    {
        "name": "add_calendar_event",
        "description": (
            "Add an event to the user's Google Calendar (doctor "
            "appointment, meeting, flight, etc). Use when the user says "
            "'add a dentist appointment at 3pm Friday', 'put a meeting "
            "with X on my calendar for tomorrow at 10', etc. Do NOT use "
            "for reminders -- use `set_reminder` for those. CRITICAL: "
            "preserve am/pm in `when` exactly as the user said it. If "
            "they said '3pm Friday', `when` MUST contain 'pm'. NEVER "
            "drop the am/pm marker."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string",
                           "description": "Event title (e.g. 'dentist')."},
                "when": {"type": "string",
                          "description": "Natural time phrase."},
            },
            "required": ["title", "when"],
        },
        "run": lambda a, o: (
            __import__("agenda")._add_event(a.get("title", ""),
                                             a.get("when", ""))[0]
            if _is_owner(o)
            else "Only the owner can edit the calendar, Sir."),
    },
    {
        "name": "reschedule_event_relative",
        "description": (
            "Shift an existing calendar event by a relative amount. Use "
            "for 'push the dentist an hour later', 'move the meeting 30 "
            "minutes earlier', 'bump it two hours back'. The `delta` "
            "should be the natural phrase the user said. NEVER use this "
            "when the user gave a specific new time -- use "
            "`reschedule_event` for that."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "event": {"type": "string",
                           "description": "Words from the event title."},
                "delta": {"type": "string",
                           "description": ("Relative shift phrase, e.g. "
                                            "'an hour later', '30 minutes "
                                            "earlier', 'two days later'.")},
            },
            "required": ["event", "delta"],
        },
        "run": lambda a, o: (
            __import__("agenda").reschedule_relative(a.get("event", ""),
                                                      a.get("delta", ""))
            if _is_owner(o)
            else "Only the owner can edit the calendar, Sir."),
    },
    {
        "name": "reschedule_event",
        "description": ("Move an existing calendar event to a new ABSOLUTE "
                         "time. Use for 'reschedule the dentist to 4pm', "
                         "'move the meeting to tomorrow at 10'. For "
                         "relative shifts ('an hour later') use "
                         "`reschedule_event_relative` instead."),
        "parameters": {
            "type": "object",
            "properties": {
                "event": {"type": "string",
                           "description": "Words from the event title."},
                "new_when": {"type": "string",
                              "description": "New time phrase."},
            },
            "required": ["event", "new_when"],
        },
        "run": lambda a, o: (
            __import__("agenda").reschedule_event(a.get("event", ""),
                                                    a.get("new_when", ""))
            if _is_owner(o)
            else "Only the owner can edit the calendar, Sir."),
    },
    {
        "name": "cancel_event",
        "description": ("Remove a calendar event. Use for 'cancel the "
                         "dentist appointment', 'delete the 3pm meeting'."),
        "parameters": {
            "type": "object",
            "properties": {
                "event": {"type": "string",
                           "description": "Words from the event title."},
            },
            "required": ["event"],
        },
        "run": lambda a, o: (
            __import__("agenda").cancel_event(a.get("event", ""))
            if _is_owner(o)
            else "Only the owner can edit the calendar, Sir."),
    },
    {
        "name": "complete_todo",
        "description": ("Mark a todo as done (removes it from the list). "
                         "Use for 'I finished the laundry', 'done with "
                         "the milk one', 'cross off X'."),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string",
                          "description": "Words from the todo to remove."},
            },
            "required": ["text"],
        },
        "run": lambda a, o: (
            __import__("agenda").todos_complete(a.get("text", ""))
            if _is_owner(o)
            else "Only the owner can edit todos, Sir."),
    },

    # Diagnostics
    {
        "name": "system_check",
        "description": (
            "Run Orion's self-diagnostic: microphone, speakers, wake model, "
            "voice profile, Gemini reachability, email config, plus PC "
            "disk/RAM/CPU/battery. Use whenever the user asks for a system "
            "check, status report, diagnostics, or 'how are you doing'."
        ),
        "parameters": {"type": "object", "properties": {}},
        "run": lambda a, o: health_check.run_health_check(),
    },

    # Orion self-control
    {
        "name": "stop_speaking",
        "description": "Stop speaking immediately.",
        "parameters": {"type": "object", "properties": {}},
        "run": _intent("stop"),
    },
    {
        "name": "mute",
        "description": "Mute Orion's voice output.",
        "parameters": {"type": "object", "properties": {}},
        "run": _intent("mute"),
    },
    {
        "name": "unmute",
        "description": "Turn Orion's voice output back on.",
        "parameters": {"type": "object", "properties": {}},
        "run": _intent("unmute"),
    },
    {
        "name": "new_conversation",
        "description": "Clear the chat and start a fresh conversation.",
        "parameters": {"type": "object", "properties": {}},
        "run": _intent("clear"),
    },
    {
        "name": "describe_self",
        "description": (
            "ALWAYS call this when the user asks about Orion itself -- what "
            "model/LLM/AI powers it, what speech recognition or voice it "
            "uses, what its wake word is, what it can do, what libraries "
            "or stack it's built on, or any 'tell me about yourself' / "
            "'who are you' / 'what are you' style question. This tool "
            "returns the AUTHORITATIVE description of Orion's stack. "
            "NEVER guess Orion's model, STT engine, TTS voice, or "
            "capabilities from training data -- call this tool instead, "
            "then read its answer back. Pick the narrowest `topic` that "
            "fits the question; use 'general' only when the user asks "
            "broadly about Orion as a whole."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "enum": ["brain", "stt", "tts", "wake", "capabilities",
                             "stack", "privacy", "general"],
                    "description": (
                        "Which facet to describe. 'brain' = the LLM/model; "
                        "'stt' = speech recognition; 'tts' = voice output; "
                        "'wake' = wake word; 'capabilities' = what Orion "
                        "can do; 'stack' = libraries/tech; 'privacy' = "
                        "what runs locally vs cloud; 'general' = a short "
                        "overview covering brain+STT+TTS."
                    ),
                },
            },
            "required": ["topic"],
        },
        "run": lambda a, o: (
            __import__("about").full_report()
            if (a.get("topic") or "general") == "general"
            else __import__("about").describe(a.get("topic"))
        ),
    },
    {
        "name": "shut_down_orion",
        "description": "Shut Orion down and exit.",
        "parameters": {"type": "object", "properties": {}},
        "run": _intent("exit"),
    },
]


_RUN = {t["name"]: t["run"] for t in TOOLS}


def declarations():
    """Return the tool list in the schema shape Gemini expects.

    Honors `disabled_tools` from config_overrides.json so the Hub can
    switch built-in tools off without code edits.
    """
    try:
        import config_overrides as _ov
        disabled = _ov.disabled_tools()
    except Exception:
        disabled = set()
    built_in = [
        {
            "name": t["name"],
            "description": t["description"],
            "parameters": t["parameters"],
        }
        for t in TOOLS
        if t["name"] not in disabled
    ]
    # Tools discovered over MCP join the list so the LLM sees them alongside
    # Orion's own. Safe no-op if MCP is off or unavailable.
    try:
        import mcp_client
        built_in.extend(mcp_client.declarations())
    except Exception:
        pass
    return built_in


# Tools that delete data or close the user's apps/windows. Orion asks out
# loud and waits for an explicit "yes" before running any of these, so a
# misheard command can't wipe things or close apps without permission. The
# value is the plain-language action spoken back ("Did you want me to ...?").
_CONFIRM_TOOLS = {
    "close_all_tabs": "close all your browser tabs",
    "panic": "close those apps",
    "empty_trash": "empty the Recycle Bin",
    "shutdown_in": "schedule a shutdown of your PC",
    "clear_reminders": "clear all your reminders",
    "cancel_reminder": "cancel that reminder",
    "clear_todos": "clear your whole to-do list",
    "cancel_event": "cancel that calendar event",
}

# How to say the same thing about several items at once. The model answers
# one spoken request with several calls now ("cancel both reminders" is two
# cancel_reminder calls), and asking about each one separately is noise
# rather than safety -- so Orion asks once and says how many.
_CONFIRM_PLURAL = {
    "cancel_reminder": "cancel those {n} reminders",
    "cancel_event": "cancel those {n} calendar events",
}


def confirm_batch(calls, orion):
    """Get spoken approval for the destructive tools in one round of calls.

    Asks once per tool rather than once per call, with the count when it's
    more than one. Returns (approved, refused) sets of tool names; the
    caller skips anything in `refused` and hands `approved` to run_call so
    it doesn't ask a second time.
    """
    approved, refused = set(), set()
    if orion is None or not hasattr(orion, "confirm_action"):
        return approved, refused

    counts = {}
    for call in calls:
        name = call["name"]
        if name in _CONFIRM_TOOLS:
            counts[name] = counts.get(name, 0) + 1

    for name, n in counts.items():
        description = _CONFIRM_TOOLS[name]
        if n > 1:
            description = _CONFIRM_PLURAL.get(
                name, description + f", {n} of them").format(n=n)
        if orion.confirm_action(description):
            approved.add(name)
        else:
            refused.add(name)
    return approved, refused


# Tools that READ state rather than change it. Their result is information
# the model may need before it can act -- so when one of these runs, the
# agentic loop in orion.py goes back to the model with the answer instead of
# ending the turn. Everything else is a completed action whose own spoken
# reply ("Cancelled the reminder to X, Sir.") is already the final word, and
# ending there is what keeps an ordinary "lights on" at one round trip.
QUERY_TOOLS = {
    "list_reminders",
    "list_agenda",
    "calendar_summary",
    "read_todos",
    "check_email",
    "list_unread_email",
    "summarize_email",
    "find_file",
    "get_time",
    "system_check",
}


def is_query_tool(name):
    """True when `name` returns information the model should read.

    MCP tools count: a remote server hands back data, not a spoken
    confirmation, so the model needs to see it to do anything useful.
    """
    return name.startswith("mcp__") or name in QUERY_TOOLS


def run_call(name, args, orion, approved=None):
    """Execute the tool `name` with `args`. Returns spoken text or None.

    None means the handler already spoke (or there is nothing to say).

    `approved` is the set of tool names the user has already said yes to
    this turn (see confirm_batch). Tools not in it are still confirmed here,
    so a caller that skips confirm_batch keeps the gate.
    """
    # Tools whose name starts with the MCP prefix are served by a remote MCP
    # server, not a local handler -- route those through the MCP client.
    if name.startswith("mcp__"):
        try:
            import mcp_client
            return mcp_client.call(name, args or {})
        except Exception:
            return "Something went wrong running that MCP tool."

    handler = _RUN.get(name)
    if handler is None:
        return f"I don't have a tool called {name}."

    # Destructive actions need an explicit spoken yes first.
    description = _CONFIRM_TOOLS.get(name)
    if (description and name not in (approved or ())
            and orion is not None and hasattr(orion, "confirm_action")):
        if not orion.confirm_action(description):
            return "Okay, I'll leave it."

    try:
        return handler(args or {}, orion)
    except Exception:
        return "Something went wrong running that."
