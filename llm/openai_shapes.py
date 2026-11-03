"""Message/tool shape conversion for the OpenAI-compatible bridges.

DeepSeek and Grok both speak OpenAI's chat-completions dialect, so they
share every conversion between Orion's neutral shapes and the wire format.
This module holds that conversion once instead of once per bridge.

Orion's neutral conversation format (see llm_router) is a list of dicts:

    {"role": "user",      "content": "..."}
    {"role": "assistant", "content": "...",
                          "calls": [{"id":.., "name":.., "args": {..}}]}
    {"role": "tool",      "id": .., "name": .., "content": "..."}

Gemini does not use any of this -- its bridge builds native `types.Content`
objects from the same neutral list.
"""

import json


def to_tools(function_declarations):
    """Wrap Gemini-shaped tool declarations in OpenAI's function-tool schema."""
    return [
        {
            "type": "function",
            "function": {
                "name": d["name"],
                "description": d.get("description", ""),
                "parameters": d.get("parameters",
                                    {"type": "object", "properties": {}}),
            },
        }
        for d in function_declarations
    ]


def to_messages(history):
    """Convert a neutral message list into OpenAI chat messages.

    Assistant turns carry their tool calls back verbatim and every tool
    result is tied to the call it answers by id -- both APIs reject a
    `tool` message whose tool_call_id doesn't match a call they just made.
    """
    out = []
    for msg in history:
        role = msg.get("role")
        if role == "assistant":
            calls = msg.get("calls") or []
            entry = {"role": "assistant",
                     "content": msg.get("content") or None}
            if calls:
                entry["tool_calls"] = [
                    {
                        "id": c["id"],
                        "type": "function",
                        "function": {
                            "name": c["name"],
                            "arguments": json.dumps(c.get("args") or {}),
                        },
                    }
                    for c in calls
                ]
            out.append(entry)
        elif role == "tool":
            out.append({
                "role": "tool",
                "tool_call_id": msg.get("id", ""),
                "content": str(msg.get("content", "")),
            })
        else:
            out.append({"role": role or "user",
                        "content": msg.get("content", "")})
    return out


def parse_calls(message):
    """Pull the tool calls off an OpenAI response message, neutral-shaped.

    A model that emits malformed JSON arguments still gets its call run with
    empty args -- better than dropping the call and going silent.
    """
    calls = []
    for i, tc in enumerate(getattr(message, "tool_calls", None) or []):
        try:
            raw = tc.function.arguments or "{}"
            args = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except Exception:
            args = {}
        if not isinstance(args, dict):
            args = {}
        calls.append({
            "id": getattr(tc, "id", None) or f"call_{i}",
            "name": tc.function.name,
            "args": args,
        })
    return calls
