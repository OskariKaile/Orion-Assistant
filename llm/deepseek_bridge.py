"""Bridge between Orion and the DeepSeek API.

Drop-in replacement for gemini_bridge: it exposes the same send_query /
send_image_query / send_with_tools / ask_once / clear_chat / read_response
interface, so llm_router can swap it in without any other changes.

DeepSeek's API is OpenAI-compatible, so this talks to it through the
`openai` SDK pointed at https://api.deepseek.com.

Two things DeepSeek cannot do, both flagged below so llm_router.py knows to
hand those requests to Gemini instead:
  * see images -- the chat models are text-only
  * search the web -- there is no server-side grounding tool

Get an API key at https://platform.deepseek.com and put it in
config.DEEPSEEK_API_KEY (or secrets_local.py, or the DEEPSEEK_API_KEY
environment variable).
"""

import os
import time

import config
import openai_shapes

try:
    import telemetry
except Exception:
    telemetry = None

try:
    from openai import OpenAI
except Exception:  # openai package not installed yet
    OpenAI = None

# Capability flags read by llm_router.py to decide what to route elsewhere.
SUPPORTS_VISION = False
SUPPORTS_WEB_SEARCH = False

_client = None
# The most recent reply text. read_response() returns this.
_last_response = ""


def _get_client():
    """Return a cached DeepSeek client, or None if it can't be created."""
    global _client
    if _client is not None:
        return _client
    if OpenAI is None:
        return None
    key = (config.DEEPSEEK_API_KEY
           or os.environ.get("DEEPSEEK_API_KEY", "")).strip()
    if not key:
        return None
    try:
        _client = OpenAI(api_key=key, base_url=config.DEEPSEEK_BASE_URL)
    except Exception:
        _client = None
    return _client


def _record_usage(resp, llm_ms):
    """Push timing + token usage into the active telemetry turn.
    No-op if telemetry isn't available or no turn is active."""
    if telemetry is None:
        return
    try:
        telemetry.add("llm_ms", llm_ms)
        telemetry.set_field(model=config.DEEPSEEK_MODEL)
        usage = getattr(resp, "usage", None)
        if usage is not None:
            telemetry.set_field(
                tokens_in=getattr(usage, "prompt_tokens", None),
                tokens_out=getattr(usage, "completion_tokens", None),
            )
    except Exception:
        pass


def _chat(messages, tools=None):
    """Call DeepSeek once. Returns the OpenAI response object, or None."""
    client = _get_client()
    if client is None:
        return None
    kwargs = {"model": config.DEEPSEEK_MODEL, "messages": messages}
    if tools:
        kwargs["tools"] = tools
    t0 = time.monotonic()
    try:
        resp = client.chat.completions.create(**kwargs)
    except Exception:
        return None
    _record_usage(resp, int((time.monotonic() - t0) * 1000))
    return resp


def _generate(messages):
    """Send `messages` to DeepSeek, store the reply, return True on success."""
    global _last_response
    resp = _chat(messages)
    if resp is None:
        _last_response = ""
        return False
    try:
        _last_response = (resp.choices[0].message.content or "").strip()
    except Exception:
        _last_response = ""
    return bool(_last_response)


def send_query(query):
    """Send a text query to DeepSeek. Returns True if a reply came back."""
    return _generate([{"role": "user", "content": query}])


def send_image_query(instruction):
    """Always False -- DeepSeek's chat models are text-only.

    Kept so this module stays a drop-in for the other bridges. llm_router
    reads SUPPORTS_VISION and sends image questions to Gemini instead, so
    this only fires if something calls the bridge directly.
    """
    return False


def send_with_tools(prompt, function_declarations, history=None):
    """Send a tool-enabled turn to DeepSeek.

    `history` is Orion's neutral message list (see llm_router) and carries
    the whole exchange so far, tool results included. When it's None this
    falls back to a single user turn built from `prompt`, which is what the
    one-shot callers (the startup warm-up ping) still pass.

    Returns one of:
      ("calls", [{"id":.., "name":.., "args": {..}}, ...]) -- run these
      ("text",  reply_text)                                -- answered
      ("error", "")                                        -- request failed
    """
    messages = (openai_shapes.to_messages(history) if history
                else [{"role": "user", "content": prompt}])
    resp = _chat(messages, tools=openai_shapes.to_tools(function_declarations))
    if resp is None:
        return ("error", "")

    try:
        message = resp.choices[0].message
    except Exception:
        return ("error", "")

    calls = openai_shapes.parse_calls(message)
    if calls:
        return ("calls", calls)
    return ("text", (message.content or "").strip())


def ask_once(prompt):
    """Send a one-off query to DeepSeek and return the reply text (or "").

    Unlike send_query this does not touch the cached _last_response, so it is
    safe for background work -- fact extraction, the startup greeting -- that
    must not disturb the main conversation flow.
    """
    resp = _chat([{"role": "user", "content": prompt}])
    if resp is None:
        return ""
    try:
        return (resp.choices[0].message.content or "").strip()
    except Exception:
        return ""


def clear_chat():
    """Start a fresh conversation. Session history lives in memory.py, so
    there is nothing server-side to clear -- just drop the cached reply."""
    global _last_response
    _last_response = ""
    return True


def read_response(query=""):
    """Return DeepSeek's most recent reply (already fetched by send_query)."""
    return _last_response
