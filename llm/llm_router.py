"""Picks which model answers each request.

Orion's brain is normally DeepSeek: cheapest of the options and good at
tool routing. DeepSeek is text-only and has no server-side web search, so
two kinds of request are handed to Gemini instead:

  * anything with an image attached -- "describe my screen", "look at this"
  * real-time questions that need Google Search grounding

Everything else -- ordinary chat, tool calls, background one-shots like
fact extraction and the startup greeting -- goes to DeepSeek.

GOING BACK TO GEMINI-ONLY is one line in config.py:

    LLM_BACKEND = "gemini"

or flip it in the Hub under Settings -> Model, which applies without a
restart. Gemini handles images and search itself, so the hand-offs below
turn into no-ops automatically -- nothing else needs changing.

This module is a drop-in for the individual bridges: same send_query /
send_image_query / send_with_tools / ask_once / clear_chat / read_response
interface, so orion.py imports it as `ai`.
"""

import importlib

import config

# Short config name -> module name.
_MODULES = {
    "deepseek": "deepseek_bridge",
    "gemini": "gemini_bridge",
    "grok": "grok_bridge",
}

_DEFAULT = "deepseek"

# Imported bridges, keyed by module name.
_cache = {}

# The bridge that produced the reply read_response() should hand back. Set
# by every call that stashes a response, so a Gemini image answer isn't
# read off DeepSeek's cache (or vice versa).
_last = None


def _module_name(name):
    """Resolve a short backend name to an importable module name, or None."""
    return _MODULES.get(name)


def _load(name):
    """Import a bridge by short name. Returns the module, or None if the
    name is unknown or the import fails (e.g. its SDK isn't installed)."""
    mod_name = _module_name((name or "").strip().lower())
    if mod_name is None:
        return None
    if mod_name not in _cache:
        try:
            _cache[mod_name] = importlib.import_module(mod_name)
        except Exception:
            _cache[mod_name] = None
    return _cache[mod_name]


def backend_name():
    """Short name of the configured text backend.

    Read live on every call so a change from the Hub takes effect without
    restarting Orion.
    """
    name = (getattr(config, "LLM_BACKEND", _DEFAULT) or "").strip().lower()
    return name if _module_name(name) else _DEFAULT


def _main():
    """The bridge handling ordinary text. Falls back to Gemini if the
    configured one won't import at all."""
    return _load(backend_name()) or _load("gemini")


def _wants_search(text):
    """True when this prompt asked for web grounding.

    memory.build_prompt() tags real-time questions with a note mentioning
    the "web search tool"; the Gemini and Grok bridges look for the same
    marker to decide whether to switch grounding on.
    """
    return isinstance(text, str) and "web search tool" in text.lower()


def _route(capability, text=""):
    """Return the bridge that should handle this request.

    `capability` is "image", "search" or "text". A request only leaves the
    main backend when that backend can't do the job itself.
    """
    main = _main()
    if main is None:
        return None
    if capability == "image" and not getattr(main, "SUPPORTS_VISION", True):
        return _load(getattr(config, "LLM_IMAGE_BACKEND", "gemini")) or main
    if capability == "search" and not getattr(main, "SUPPORTS_WEB_SEARCH", True):
        return _load(getattr(config, "LLM_SEARCH_BACKEND", "gemini")) or main
    return main


def _fallback(used):
    """Second-chance bridge after `used` failed outright (missing key, API
    down, rate limit). config.LLM_FALLBACK names it; "" turns it off."""
    name = (getattr(config, "LLM_FALLBACK", "") or "").strip().lower()
    if not name:
        return None
    alt = _load(name)
    return alt if alt is not None and alt is not used else None


def supports_tools():
    """True when the active backend can do function calling.

    orion.py checks this before taking the tool-calling path.
    """
    return hasattr(_main(), "send_with_tools")


def send_query(query):
    """Send a text query. Returns True if a reply was received."""
    global _last
    bridge = _route("search" if _wants_search(query) else "text", query)
    if bridge is None:
        return False
    _last = bridge
    if bridge.send_query(query):
        return True

    alt = _fallback(bridge)
    if alt is None:
        return False
    _last = alt
    return alt.send_query(query)


def send_image_query(instruction):
    """Send the clipboard image plus an instruction. Returns True on success.

    No fallback here: if the vision backend can't answer, the text-only one
    behind it can't either.
    """
    global _last
    bridge = _route("image", instruction)
    if bridge is None or not hasattr(bridge, "send_image_query"):
        return False
    _last = bridge
    return bridge.send_image_query(instruction)


def send_with_tools(prompt, function_declarations, history=None):
    """Send a tool-enabled turn with Orion's tools attached.

    `history` is the neutral message list described in NEUTRAL FORMAT below
    and carries the exchange so far, including what earlier tools returned,
    so the model can take another step. Pass None for a one-shot turn built
    from `prompt` alone.

    NEUTRAL FORMAT -- provider-independent, each bridge converts it:

        {"role": "user",      "content": "..."}
        {"role": "assistant", "content": "...",
                              "calls": [{"id":.., "name":.., "args": {..}}]}
        {"role": "tool",      "id": .., "name": .., "content": "..."}

    A call may also carry "signature": an opaque blob the bridge that
    produced it needs handed straight back on the next turn (Gemini 3's
    thought_signature). Bridges that don't use it ignore it.

    Returns one of:
      ("calls", [{"id":.., "name":.., "args": {..}}, ...]) -- run these
      ("text",  reply_text)                                -- answered
      ("error", "")                                        -- request failed
    """
    global _last
    bridge = _route("text", prompt)
    if bridge is None or not hasattr(bridge, "send_with_tools"):
        return ("error", "")
    _last = bridge
    kind, payload = bridge.send_with_tools(prompt, function_declarations,
                                           history=history)
    if kind != "error":
        return (kind, payload)

    alt = _fallback(bridge)
    if alt is None or not hasattr(alt, "send_with_tools"):
        return ("error", "")
    _last = alt
    return alt.send_with_tools(prompt, function_declarations, history=history)


def ask_once(prompt):
    """Send a one-off query and return the reply text (or "").

    Does not touch the cached response, so background work -- fact
    extraction, the startup greeting, reminder parsing -- can't disturb the
    main conversation flow.
    """
    bridge = _route("search" if _wants_search(prompt) else "text", prompt)
    if bridge is None:
        return ""
    reply = bridge.ask_once(prompt) if hasattr(bridge, "ask_once") else ""
    if reply:
        return reply
    alt = _fallback(bridge)
    if alt is None or not hasattr(alt, "ask_once"):
        return ""
    return alt.ask_once(prompt)


def clear_chat():
    """Start a fresh conversation on every bridge that's been used."""
    global _last
    _last = None
    ok = True
    for bridge in _cache.values():
        if bridge is None:
            continue
        try:
            ok = bool(bridge.clear_chat()) and ok
        except Exception:
            ok = False
    return ok


def read_response(query=""):
    """Return the most recent reply, from whichever bridge produced it."""
    bridge = _last or _main()
    if bridge is None:
        return ""
    return bridge.read_response(query)
