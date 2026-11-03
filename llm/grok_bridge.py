"""Bridge between Orion and xAI's Grok API.

Drop-in replacement for gemini_bridge: it exposes the same send_query /
send_image_query / send_with_tools / ask_once / clear_chat / read_response
interface, so orion.py can use it without any other changes.

xAI's API is OpenAI-compatible, so this talks to it through the `openai`
SDK pointed at https://api.x.ai/v1.

Get an API key at https://console.x.ai and put it in config.GROK_API_KEY
(or secrets_local.py, or the GROK_API_KEY environment variable).
"""

import base64
import io
import os

import config
import openai_shapes

try:
    from openai import OpenAI
except Exception:  # openai package not installed yet
    OpenAI = None

_client = None
# The most recent reply text. read_response() returns this.
_last_response = ""


def _get_client():
    """Return a cached Grok client, or None if it can't be created."""
    global _client
    if _client is not None:
        return _client
    if OpenAI is None:
        return None
    key = (config.GROK_API_KEY or os.environ.get("GROK_API_KEY", "")).strip()
    if not key:
        return None
    try:
        _client = OpenAI(api_key=key, base_url=config.GROK_BASE_URL)
    except Exception:
        _client = None
    return _client


def _live_search(query_text):
    """Return xAI Live Search params for real-time questions, else None.

    memory.py tags real-time prompts with a note mentioning the "web search
    tool"; only those get Live Search turned on (it bills per source used).
    """
    if not config.WEB_SEARCH:
        return None
    if not (isinstance(query_text, str)
            and "web search tool" in query_text.lower()):
        return None
    return {"search_parameters": {"mode": "auto"}}


def _chat(messages, query_text, tools=None):
    """Call Grok once. Returns the OpenAI response object, or None on failure."""
    client = _get_client()
    if client is None:
        return None
    kwargs = {"model": config.GROK_MODEL, "messages": messages}
    if tools:
        kwargs["tools"] = tools
    extra = _live_search(query_text)
    if extra:
        kwargs["extra_body"] = extra
    try:
        return client.chat.completions.create(**kwargs)
    except Exception:
        return None


def _generate(messages, query_text):
    """Send `messages` to Grok, store the reply, return True on success."""
    global _last_response
    resp = _chat(messages, query_text)
    if resp is None:
        _last_response = ""
        return False
    try:
        _last_response = (resp.choices[0].message.content or "").strip()
    except Exception:
        _last_response = ""
    return bool(_last_response)


def send_query(query):
    """Send a text query to Grok. Returns True if a reply was received."""
    return _generate([{"role": "user", "content": query}], query)


def send_image_query(instruction):
    """Send the clipboard image plus an instruction to Grok.

    The image must already be on the clipboard (vision.copy_image_to_clipboard).
    Returns True if a reply was received.
    """
    try:
        from PIL import ImageGrab
        image = ImageGrab.grabclipboard()
    except Exception:
        image = None
    if image is None:
        return False
    try:
        buf = io.BytesIO()
        image.convert("RGB").save(buf, format="JPEG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return False
    content = [
        {"type": "text", "text": instruction},
        {"type": "image_url",
         "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
    ]
    return _generate([{"role": "user", "content": content}], instruction)


def send_with_tools(prompt, function_declarations, history=None):
    """Send a tool-enabled turn to Grok.

    `history` is Orion's neutral message list (see llm_router) and carries
    the whole exchange so far, tool results included. When it's None this
    falls back to a single user turn built from `prompt`.

    Returns one of:
      ("calls", [{"id":.., "name":.., "args": {..}}, ...]) -- run these
      ("text",  reply_text)                                -- answered
      ("error", "")                                        -- request failed
    """
    messages = (openai_shapes.to_messages(history) if history
                else [{"role": "user", "content": prompt}])
    resp = _chat(messages, prompt,
                 tools=openai_shapes.to_tools(function_declarations))
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
    """Send a one-off query to Grok and return the reply text (or "").

    Unlike send_query this does not touch the cached _last_response, so it is
    safe for background work -- fact extraction, the startup greeting -- that
    must not disturb the main conversation flow.
    """
    resp = _chat([{"role": "user", "content": prompt}], prompt)
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
    """Return Grok's most recent reply (already fetched by send_query)."""
    return _last_response
