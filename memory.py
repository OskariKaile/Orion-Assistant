"""Orion's memory.

Two layers:

* Session memory -- the full conversation (user + the AI turns) is kept in a
  Python list for the lifetime of the process and prepended to every query, so
  Orion remembers what was said earlier in the session.
* Persistent memory -- facts about the user are stored in a local memory.json
  file that survives restarts. The facts are injected into every query so
  the AI always has that context.
"""

import datetime
import json
import os
import threading

import config

# In-memory transcript of the current session: list of (role, text, speaker)
# where role is "User" or "Orion" and speaker is "owner", "other", or None
# (for Orion's own lines, which carry no speaker tag).
_session = []

# Index into _session at which a non-owner first spoke in this session. When a
# non-owner asks something later, the live transcript shown to the AI is
# sliced to [_shared_from:] -- so private back-and-forth before the other
# person joined stays private, but everything from when they joined is shared
# context they can refer back to. None means no non-owner has spoken yet.
_shared_from = None

# Cached list of persistent facts loaded from memory.json.
_facts = []

# Hot recap: a single ~150-word rolling paragraph summarising every past
# session. Sent to the AI on every turn so Orion has gist-level continuity.
_recap = ""

# Cold archive: list of dicts {"date": str, "summary": str, "embedding": list}.
# NEVER sent verbatim; searched locally on each turn for an on-demand match.
_archive = []
_archive_loaded = False

_compress_lock = threading.Lock()
_compress_in_progress = False

_HERE = os.path.dirname(os.path.abspath(__file__))
_MEMORY_PATH = os.path.join(_HERE, config.MEMORY_FILE)
_RECAP_PATH = os.path.join(_HERE, getattr(config, "RECAP_FILE",
                                          "conversation_recap.json"))
_ARCHIVE_PATH = os.path.join(_HERE, getattr(config, "ARCHIVE_FILE",
                                            "conversation_archive.jsonl"))


# --- persistent memory ---
def load():
    """Load persistent facts from memory.json into the in-memory cache.

    Also loads the cross-session conversation recap and the cold archive (if
    enabled) so the first query of the session can already use them.
    """
    global _facts
    try:
        with open(_MEMORY_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        _facts = [str(f).strip() for f in data.get("facts", []) if str(f).strip()]
    except (OSError, ValueError):
        _facts = []
    if getattr(config, "CONVERSATION_RECAP", False):
        _load_recap()
        _load_archive()
    return _facts


def _save():
    """Write the persistent facts back to memory.json."""
    try:
        with open(_MEMORY_PATH, "w", encoding="utf-8") as fh:
            json.dump({"facts": _facts}, fh, indent=2)
    except OSError:
        pass


def remember(fact):
    """Store a new fact about the user. Returns False if it's a duplicate."""
    fact = fact.strip()
    if not fact or fact.lower() in (f.lower() for f in _facts):
        return False
    _facts.append(fact)
    _save()
    return True


def forget_all():
    """Drop every persistent fact."""
    global _facts
    _facts = []
    _save()


def update_fact(index, new_text):
    """Replace the fact at `index` with `new_text`. Returns False on a bad
    index or empty/duplicate text."""
    new_text = (new_text or "").strip()
    if not new_text or not (0 <= index < len(_facts)):
        return False
    if new_text.lower() in (f.lower() for i, f in enumerate(_facts) if i != index):
        return False
    _facts[index] = new_text
    _save()
    return True


def delete_fact(index):
    """Drop the fact at `index`. Returns False on a bad index."""
    if not (0 <= index < len(_facts)):
        return False
    _facts.pop(index)
    _save()
    return True


def set_recap(text):
    """Overwrite the rolling cross-session recap with `text`."""
    global _recap
    _recap = (text or "").strip()
    _save_recap()
    return True


def facts():
    return list(_facts)


# --- auto-memory: learning facts and greeting the user ---
def _part_of_day(hour):
    if hour < 12:
        return "morning"
    if hour < 18:
        return "afternoon"
    return "evening"


def time_of_day_greeting():
    """A plain spoken greeting based on the current time.

    The offline fallback used whenever no AI-generated greeting is available.
    """
    return f"Good {_part_of_day(datetime.datetime.now().hour)}, Sir."


def build_extraction_prompt(user_text):
    """Prompt asking the AI to pull durable, long-term facts about the user
    out of a single thing they said. Used by Orion's auto-memory."""
    parts = [
        "You are the long-term memory of Orion, a personal voice assistant. "
        "From the user's message below, extract any DURABLE facts worth "
        "remembering across future sessions -- their name, where they live, "
        "their job, preferences, habits, relationships, important dates, or "
        "ongoing goals. Ignore one-off questions, requests, commands and "
        "small talk.",
        "",
        f'The user just said: "{user_text.strip()}"',
        "",
    ]
    if _facts:
        parts.append("You already remember these, so do NOT repeat them:")
        parts.extend(f"- {f}" for f in _facts)
        parts.append("")
    parts.append(
        "Reply with only the NEW durable facts, one per line, each a short "
        'third-person statement (for example: "The user\'s name is Alex" or '
        '"The user works night shifts"). If there is nothing new and durable '
        "to remember, reply with exactly: NONE"
    )
    return "\n".join(parts)


def build_greeting_prompt():
    """Prompt asking the AI for a short, personalised spoken startup greeting."""
    now = datetime.datetime.now()
    parts = [
        "You are Orion, a JARVIS-style personal voice assistant, greeting "
        "your user as you come online.",
        f"It is currently {now:%A} {_part_of_day(now.hour)}.",
        "",
    ]
    if _facts:
        parts.append("Things you remember about your user:")
        parts.extend(f"- {f}" for f in _facts)
        parts.append("")
    parts.append(
        "Give a brief, warm spoken greeting -- one or two sentences. Greet "
        "them by the time of day. If something you remember is relevant right "
        "now (the day of the week, a habit, an upcoming date or goal), you "
        "may weave it in naturally. Plain spoken text only: no markdown or "
        "symbols. Keep it short."
    )
    return "\n".join(parts)


# --- cross-session conversation memory (hot recap + cold archive) ---
def _load_recap():
    global _recap
    try:
        with open(_RECAP_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        _recap = str(data.get("recap", "")).strip()
    except (OSError, ValueError):
        _recap = ""


def _save_recap():
    try:
        with open(_RECAP_PATH, "w", encoding="utf-8") as fh:
            json.dump({"recap": _recap}, fh, indent=2)
    except OSError:
        pass


def _load_archive():
    global _archive, _archive_loaded
    _archive = []
    try:
        with open(_ARCHIVE_PATH, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    _archive.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    _archive_loaded = True


def _append_archive(entry):
    try:
        with open(_ARCHIVE_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
        _archive.append(entry)
    except OSError:
        pass


def recap():
    """Return the current rolling recap string (may be empty)."""
    return _recap


def _build_session_summary_prompt(turns, word_budget):
    """Prompt for distilling a list of (role, text, speaker) turns into one summary."""
    transcript = "\n".join(f"{role}: {text}" for role, text, *_ in turns)
    return (
        "You are summarising a finished voice-assistant conversation for the "
        "assistant's long-term memory. Read the transcript below and write a "
        f"single paragraph of at most {word_budget} words capturing what "
        "actually mattered: topics discussed, decisions reached, things the "
        "assistant was asked to do, ongoing projects or threads the user is "
        "working on, and anything the user might reasonably refer back to "
        "later. Skip trivial one-off lookups (clock, weather, single web "
        "searches), greetings, and small talk. Write in plain prose, third "
        "person (\"The user...\"), no bullet points, no preamble.\n\n"
        f"Transcript:\n{transcript}"
    )


def _build_recap_update_prompt(old_recap, new_summary, word_budget):
    """Prompt for folding a new session summary into the rolling recap."""
    return (
        "You maintain a tiny rolling memory note for a voice assistant. "
        "Below is the current note, then a fresh summary of the session that "
        "just ended. Rewrite the note so it absorbs whatever in the new "
        "summary is still worth remembering, while dropping anything from "
        f"the old note that is now stale or no longer relevant. Hard limit: "
        f"{word_budget} words. Plain prose, third person (\"The user...\"), "
        "no bullet points, no preamble, no meta-commentary. If both inputs "
        "are empty or trivial, reply with exactly: NONE\n\n"
        f"Current note:\n{old_recap or '(empty)'}\n\n"
        f"New session summary:\n{new_summary}"
    )


def _llm_ask(prompt):
    """Best-effort one-shot call to the active model. Returns "" on failure."""
    try:
        import llm_router
        return llm_router.ask_once(prompt)
    except Exception:
        return ""


def _embed_one(text):
    """Embed one string locally. Returns a list of floats or None."""
    try:
        import semantic
        vecs = semantic.embed_texts([text])
        if not vecs:
            return None
        return [float(x) for x in vecs[0]]
    except Exception:
        return None


def _cosine(a, b):
    import math
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    denom = (math.sqrt(na) * math.sqrt(nb)) or 1.0
    return dot / denom


def recall(query):
    """On-demand archive lookup. Returns (entry, score) for the best matching
    past session if its similarity beats the threshold, otherwise (None, 0.0).

    Cheap: one local embedding + N cosine dot products. Never calls the API.
    """
    if not query or not _archive:
        return None, 0.0
    qv = _embed_one(query)
    if qv is None:
        return None, 0.0
    threshold = getattr(config, "ARCHIVE_RECALL_THRESHOLD", 0.55)
    best, best_score = None, 0.0
    for entry in _archive:
        ev = entry.get("embedding")
        if not ev:
            continue
        score = _cosine(qv, ev)
        if score > best_score:
            best, best_score = entry, score
    if best is not None and best_score >= threshold:
        return best, best_score
    return None, best_score


def _compress(turns, label):
    """Summarise a stretch of turns, append it to the archive (with an
    embedding) and fold the result into the rolling recap. Synchronous; meant
    to be called from a background thread or at shutdown."""
    if not turns:
        return
    word_budget = getattr(config, "ARCHIVE_SUMMARY_WORDS", 200)
    summary_prompt = _build_session_summary_prompt(turns, word_budget)
    summary = _llm_ask(summary_prompt).strip()
    if not summary or summary.upper().startswith("NONE"):
        return

    today = datetime.date.today().isoformat()
    embedding = _embed_one(summary) or []
    _append_archive({
        "date": today,
        "label": label,
        "summary": summary,
        "embedding": embedding,
    })

    global _recap
    recap_budget = getattr(config, "RECAP_WORD_BUDGET", 150)
    recap_prompt = _build_recap_update_prompt(_recap, summary, recap_budget)
    new_recap = _llm_ask(recap_prompt).strip()
    if new_recap and not new_recap.upper().startswith("NONE"):
        _recap = new_recap
        _save_recap()


def compress_if_needed():
    """Mid-session compression: if the live session has grown past
    RECAP_MAX_TURNS, summarise the oldest portion in a background thread and
    drop those turns from the live transcript -- so the prompt stays small
    even during marathon sessions. Safe to call after every turn."""
    if not getattr(config, "CONVERSATION_RECAP", False):
        return
    max_turns = getattr(config, "RECAP_MAX_TURNS", 30)
    keep = getattr(config, "RECAP_KEEP_RECENT_TURNS", 6)
    user_turns = sum(1 for row in _session if row[0] == "User")
    if user_turns < max_turns:
        return

    global _compress_in_progress, _shared_from
    with _compress_lock:
        if _compress_in_progress:
            return
        # Snapshot the older portion and drop it from the live transcript.
        older = _session[:-keep * 2] if len(_session) > keep * 2 else list(_session)
        if not older:
            return
        dropped = len(older)
        del _session[:dropped]
        if _shared_from is not None:
            _shared_from = max(0, _shared_from - dropped)
        _compress_in_progress = True

    def _worker():
        try:
            _compress(older, label="mid-session")
        finally:
            global _compress_in_progress
            _compress_in_progress = False

    threading.Thread(target=_worker, daemon=True).start()


def end_session():
    """Called on shutdown. Summarises whatever is left of the live session,
    archives it, and updates the rolling recap. Synchronous so the work
    actually finishes before the process exits."""
    if not getattr(config, "CONVERSATION_RECAP", False):
        return
    min_turns = getattr(config, "RECAP_MIN_TURNS_TO_SAVE", 2)
    user_turns = sum(1 for row in _session if row[0] == "User")
    if user_turns < min_turns:
        return
    try:
        _compress(list(_session), label="session-end")
    except Exception:
        pass


# --- live context ---
def active_window():
    """Return the title of the window the user currently has focused, or "".

    A single cheap local Win32 call -- no API request, no background polling,
    no measurable CPU cost. Lets Orion know what the user is doing ("summarize
    this", "what's wrong here") without the user having to spell it out.
    """
    try:
        import pygetwindow as gw
        win = gw.getActiveWindow()
        return (win.title or "").strip() if win else ""
    except Exception:
        return ""


# --- session memory ---
_write_gate = None  # optional callable; see set_write_gate


def set_write_gate(gate):
    """Install a predicate that decides whether transcript writes are allowed.

    Orion uses this for the open mic. When you interrupt or add to a command,
    the answer already in flight is abandoned, but the thread producing it
    can't be killed and will still try to record its question and its reply.
    That exchange never reached you, so letting it into the transcript would
    give the model a memory of a conversation that never happened. The gate
    returns False for such an abandoned turn and its writes are dropped.
    """
    global _write_gate
    _write_gate = gate


def _writes_allowed():
    if _write_gate is None:
        return True
    try:
        return bool(_write_gate())
    except Exception:
        return True


def add_user(text, speaker="owner"):
    """Append a user turn tagged with who spoke ("owner" or "other").

    The first time a non-owner is seen in this session, the shared-from
    marker is set to this turn's index so older private back-and-forth never
    leaks to the new speaker.
    """
    global _shared_from
    if not _writes_allowed():
        return
    if speaker == "other" and _shared_from is None:
        _shared_from = len(_session)
    _session.append(("User", text.strip(), speaker))
    compress_if_needed()


def add_orion(text):
    if not _writes_allowed():
        return
    _session.append(("Orion", text.strip(), None))
    compress_if_needed()


def clear_session():
    """Forget the current conversation (persistent facts are kept)."""
    global _shared_from
    _session.clear()
    _shared_from = None


# --- prompt assembly ---
def _live_state_block():
    """Return the owner's current reminders and to-dos as prompt text.

    Both come straight from local JSON, so this costs no API call and no
    network round trip -- calendar events are deliberately left out for
    exactly that reason (agenda.snapshot would hit Google every turn).

    The point is the ids. A model that can see

        r_1a89 | 2026-09-09 22:00 | daily | study for German
        r_a286 | 2026-09-09 22:00 | daily | study German

    can cancel both of those in one reply, with two ids and no guessing.
    Given only the user's words it has to invent a search string and hope,
    which is how "cancel both" ends up cancelling one.
    """
    limit = getattr(config, "LIVE_STATE_MAX_ITEMS", 10)
    now = datetime.datetime.now()
    parts = ["[Right now] It is "
             + now.strftime("%A %Y-%m-%d %H:%M") + " local time."]

    try:
        import reminders
        pending = reminders.list_pending()
    except Exception:
        pending = []
    if pending:
        parts.append("")
        parts.append("[My pending reminders -- id | when | repeat | what]")
        for r in pending[:limit]:
            when = str(r.get("fires_at", "")).replace("T", " ")
            recur = (r.get("recur") or {}).get("kind") or "once"
            parts.append(f"- {r.get('id', '?')} | {when} | {recur} | "
                         f"{r.get('text', '')}")
        if len(pending) > limit:
            parts.append(f"- (+{len(pending) - limit} more)")
        parts.append("To cancel, call cancel_reminder with the id. If the "
                     "user means several, make one call per id in the same "
                     "reply.")

    try:
        import agenda
        todos = agenda.todos_list()
    except Exception:
        todos = []
    if todos:
        parts.append("")
        parts.append("[My to-do list]")
        for t in todos[:limit]:
            text = t.get("text", "") if isinstance(t, dict) else str(t)
            parts.append(f"- {text}")
        if len(todos) > limit:
            parts.append(f"- (+{len(todos) - limit} more)")

    return "\n".join(parts)


def build_prompt(query, web_search=False, speaker="owner", with_tools=False):
    """Combine persistent facts + session transcript + the new query.

    This is the exact text typed into the AI, so the AI sees the running
    context even though each query is dispatched separately. When `web_search`
    is set, a note asks the AI to use its built-in web search tool so the
    answer reflects current information.

    When `with_tools` is True, a hard-rule instruction is prepended that
    forces the model to actually CALL the function-calling tools it's
    handed instead of refusing or hallucinating results -- counters
    Gemini Flash's safety-refusal default on conversationally-phrased
    requests.

    `speaker` ("owner" or "other") is the verified identity of whoever just
    spoke. When it isn't the owner, all personal context is withheld from the
    prompt: persistent facts, the cross-session recap, archive recall, and
    the older portion of the live session that the other person never heard.
    Nothing personal is sent, so prompt-injection cannot leak what isn't in
    the context to begin with.
    """
    is_owner = (speaker != "other")
    parts = []

    if config.SPOKEN_STYLE:
        parts.append(config.SPOKEN_STYLE_INSTRUCTION)
        parts.append("")

    if with_tools and getattr(config, "TOOL_USE_INSTRUCTION", ""):
        parts.append(config.TOOL_USE_INSTRUCTION)
        if (getattr(config, "AGENTIC_TOOLS", False)
                and getattr(config, "MULTI_STEP_INSTRUCTION", "")):
            parts.append(config.MULTI_STEP_INSTRUCTION)
        parts.append("")

    if getattr(config, "STT_TOLERANCE", False):
        parts.append(config.STT_TOLERANCE_INSTRUCTION)
        parts.append("")

    if getattr(config, "MOOD_AWARENESS", False):
        try:
            import mood
            if mood.detect_mood(query) == "stressed":
                parts.append(mood.STRESSED_INSTRUCTION)
                parts.append("")
        except Exception:
            pass

    if web_search:
        parts.append("[Note] This question may need current information -- "
                      "use your web search tool to look it up before "
                      "answering.")
        parts.append("")

    if not is_owner:
        parts.append(
            "[Note] The person now speaking is NOT your user. Treat them as a "
            "guest: be polite and helpful with general questions, but never "
            "reveal or speculate about anything personal regarding your user "
            "-- their name, habits, schedule, past conversations, or any "
            "memories. If asked about your user or what you remember about "
            "them, simply say you can't share that. Stay in character."
        )
        parts.append("")

    if is_owner and _facts:
        parts.append("[Things to remember about me]")
        parts.extend(f"- {f}" for f in _facts)
        parts.append("")

    if is_owner and getattr(config, "CONVERSATION_RECAP", False) and _recap:
        parts.append("[Gist of past conversations]")
        parts.append(_recap)
        parts.append("")

    if is_owner and getattr(config, "CONVERSATION_RECAP", False):
        try:
            hit, _score = recall(query)
            if hit:
                parts.append(
                    f"[Possibly relevant past conversation from {hit.get('date', 'earlier')}]"
                )
                parts.append(hit.get("summary", "").strip())
                parts.append("")
        except Exception:
            pass

    if is_owner and getattr(config, "CONTEXT_AWARENESS", False):
        title = active_window()
        if title:
            parts.append(
                "[What I'm doing right now] The application window I "
                f"currently have focused is: {title}. Use this for context "
                "if the question refers to what I'm looking at; otherwise "
                "ignore it.")
            parts.append("")

    if (with_tools and is_owner
            and getattr(config, "LIVE_STATE_IN_PROMPT", False)):
        state = _live_state_block()
        if state:
            parts.append(state)
            parts.append("")

    # When the guest isn't the owner, the live transcript is sliced to the
    # point they first joined this session -- so anything the owner said
    # before that point stays private even though they share the same mic.
    if config.SEND_SESSION_HISTORY and _session:
        if is_owner:
            visible = _session
        elif _shared_from is not None:
            visible = _session[_shared_from:]
        else:
            visible = []
        if visible:
            parts.append("[Conversation so far]")
            for role, text, *_ in visible:
                parts.append(f"{role}: {text}")
            parts.append("")
        parts.append(f"User: {query}")
    else:
        parts.append(query)

    return "\n".join(parts)
