"""Bridge between Orion and Google's Gemini API (free tier).

Exposes the same send_query / send_image_query / clear_chat / read_response
interface as the other bridges and calls Gemini directly over HTTP.

A query is answered in a single API call: send_query() does the request and
stashes the reply, read_response() just hands that stashed reply back.

Get a free API key at https://aistudio.google.com/apikey and put it in
config.GEMINI_API_KEY (or set the GEMINI_API_KEY environment variable).
"""

import os
import time

import config
try:
    import telemetry
except Exception:
    telemetry = None


def _record_usage(resp, llm_ms: int) -> None:
    """Push timing + token usage from a Gemini response into the active turn.
    No-op if telemetry isn't available or no turn is active."""
    if telemetry is None:
        return
    try:
        telemetry.add("llm_ms", llm_ms)
        usage = getattr(resp, "usage_metadata", None)
        t_in = getattr(usage, "prompt_token_count", None) if usage else None
        t_out = getattr(usage, "candidates_token_count", None) if usage else None
        telemetry.set_field(model=config.GEMINI_MODEL)
        if t_in is not None or t_out is not None:
            telemetry.set_field(tokens_in=t_in, tokens_out=t_out)
    except Exception:
        pass

try:
    from google import genai
    from google.genai import types
except Exception:  # google-genai not installed yet
    genai = None
    types = None

_client = None
# The most recent reply text. read_response() returns this.
_last_response = ""


def _get_client():
    """Return a cached Gemini client, or None if it can't be created."""
    global _client
    if _client is not None:
        return _client
    if genai is None:
        return None
    key = (config.GEMINI_API_KEY or os.environ.get("GEMINI_API_KEY", "")).strip()
    if not key:
        return None
    try:
        _client = genai.Client(api_key=key)
    except Exception:
        _client = None
    return _client


def _generate(contents, query_text):
    """Send `contents` to Gemini, store the reply, return True on success."""
    global _last_response

    client = _get_client()
    if client is None:
        _last_response = ""
        return False

    # Turn on Google Search grounding only for real-time questions. memory.py
    # tags those prompts with a note mentioning the "web search tool".
    gen_config = None
    if config.WEB_SEARCH and isinstance(query_text, str) \
            and "web search tool" in query_text.lower():
        try:
            gen_config = types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())]
            )
        except Exception:
            gen_config = None

    t0 = time.monotonic()
    try:
        resp = client.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=contents,
            config=gen_config,
        )
        _last_response = (resp.text or "").strip()
        _record_usage(resp, int((time.monotonic() - t0) * 1000))
        return bool(_last_response)
    except Exception:
        _last_response = ""
        return False


def send_query(query):
    """Send a text query to Gemini. Returns True if a reply was received."""
    return _generate(query, query)


def send_image_query(instruction):
    """Send the clipboard image plus an instruction to Gemini.

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
    return _generate([instruction, image], instruction)


# JSON Schema keywords google-genai's Schema model spells differently.
_SCHEMA_RENAME = {"$defs": "defs", "$ref": "ref"}


def _schema_keys():
    """Every key google-genai's Schema model will accept (names + aliases)."""
    fields = types.Schema.model_fields
    keys = set(fields)
    keys.update(f.alias for f in fields.values() if f.alias)
    return keys


def _clean_schema(node):
    """Drop JSON Schema keywords google-genai refuses to parse.

    MCP servers hand back full JSON Schema -- `$schema`, `exclusiveMinimum`,
    `oneOf` and friends. The SDK's Schema model rejects unknown keys outright
    and takes the WHOLE tool list down with it, so every tool call fails once
    a single MCP server is connected. Gemini only needs the subset below;
    everything else goes.
    """
    if isinstance(node, list):
        return [_clean_schema(item) for item in node]
    if not isinstance(node, dict):
        return node

    allowed = _schema_keys()
    out = {}
    for key, value in node.items():
        key = _SCHEMA_RENAME.get(key, key)
        if key not in allowed:
            continue
        if key in ("properties", "defs") and isinstance(value, dict):
            # Keys here are property/definition NAMES, not schema keywords.
            out[key] = {k: _clean_schema(v) for k, v in value.items()}
        elif key in ("items", "anyOf", "any_of"):
            out[key] = _clean_schema(value)
        else:
            out[key] = value
    return out


def _function_response_part(name, content):
    """Build a function-response Part, however this SDK version spells it."""
    payload = {"result": str(content)}
    try:
        return types.Part.from_function_response(name=name, response=payload)
    except Exception:
        return types.Part(function_response=types.FunctionResponse(
            name=name, response=payload))


def _to_gemini_contents(history):
    """Convert Orion's neutral message list into Gemini `Content` objects.

    Gemini has no tool-call ids: it pairs a function_response with its
    function_call by POSITION within the turn. So every call from one model
    turn goes into a single Content, and the results that answer them go
    into the single Content that follows, in the same order -- otherwise two
    calls to the same tool in one round (cancel this reminder, and that one)
    would be impossible to tell apart.
    """
    contents = []
    pending = []  # consecutive tool results, flushed as one Content

    def flush():
        if pending:
            contents.append(types.Content(role="user", parts=list(pending)))
            pending.clear()

    for msg in history:
        role = msg.get("role")
        if role == "tool":
            pending.append(_function_response_part(msg.get("name", ""),
                                                   msg.get("content", "")))
            continue
        flush()
        if role == "assistant":
            parts = []
            if msg.get("content"):
                parts.append(types.Part(text=msg["content"]))
            for call in (msg.get("calls") or []):
                part = types.Part(function_call=types.FunctionCall(
                    name=call["name"], args=call.get("args") or {}))
                # Gemini 3 rejects a function call replayed without the
                # thought_signature it originally came with (400
                # INVALID_ARGUMENT). It's an opaque blob we carried along
                # on the neutral call dict purely to hand back here.
                if call.get("signature"):
                    part.thought_signature = call["signature"]
                parts.append(part)
            if parts:
                contents.append(types.Content(role="model", parts=parts))
        else:
            contents.append(types.Content(
                role="user",
                parts=[types.Part(text=msg.get("content", ""))]))
    flush()
    return contents


def send_with_tools(prompt, function_declarations, history=None):
    """Send a tool-enabled turn to Gemini.

    `history` is Orion's neutral message list (see llm_router) and carries
    the whole exchange so far, tool results included. When it's None this
    falls back to the bare `prompt`.

    Returns one of:
      ("calls", [{"id":.., "name":.., "args": {..}}, ...]) -- run these
      ("text",  reply_text)                                -- answered
      ("error", "")                                        -- request failed
    """
    client = _get_client()
    if client is None or types is None:
        return ("error", "")

    decls = [
        dict(d, parameters=_clean_schema(
            d.get("parameters") or {"type": "object", "properties": {}}))
        for d in function_declarations
    ]

    t0 = time.monotonic()
    try:
        contents = _to_gemini_contents(history) if history else prompt
        tool = types.Tool(function_declarations=decls)
        gen_config = types.GenerateContentConfig(tools=[tool])
        resp = client.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=contents,
            config=gen_config,
        )
    except Exception:
        return ("error", "")
    _record_usage(resp, int((time.monotonic() - t0) * 1000))

    calls, text_parts = [], []
    candidate = resp.candidates[0] if resp.candidates else None
    if candidate and candidate.content and candidate.content.parts:
        for part in candidate.content.parts:
            fn = getattr(part, "function_call", None)
            if fn and fn.name:
                # Gemini sends no id; synthesize a stable one so the loop
                # can pair each result with its call like the other bridges.
                # thought_signature rides along so the next turn can replay
                # this call -- Gemini 3 refuses it otherwise.
                calls.append({"id": f"call_{len(calls)}",
                              "name": fn.name,
                              "args": dict(fn.args or {}),
                              "signature": getattr(part, "thought_signature",
                                                    None)})
            elif getattr(part, "text", None):
                text_parts.append(part.text)

    if calls:
        return ("calls", calls)
    text = ((getattr(resp, "text", "") or "") or "".join(text_parts)).strip()
    return ("text", text)


def ask_once(prompt):
    """Send a one-off query to Gemini and return the reply text (or "").

    Unlike send_query this does not touch the cached _last_response, so it is
    safe for background work -- fact extraction, the startup greeting -- that
    must not disturb the main conversation flow.
    """
    client = _get_client()
    if client is None:
        return ""
    try:
        resp = client.models.generate_content(
            model=config.GEMINI_MODEL,
            contents=prompt,
        )
        return (resp.text or "").strip()
    except Exception:
        return ""


def clear_chat():
    """Start a fresh conversation. Session history lives in memory.py, so
    there is nothing server-side to clear -- just drop the cached reply."""
    global _last_response
    _last_response = ""
    return True


def read_response(query=""):
    """Return Gemini's most recent reply (already fetched by send_query)."""
    return _last_response
