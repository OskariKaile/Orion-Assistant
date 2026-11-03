"""Orion's MCP (Model Context Protocol) client.

This is what lets Orion truthfully say it "uses MCP". Orion is an MCP *client*:
on startup it launches the MCP servers listed in `config.MCP_SERVERS`, performs
the standard MCP handshake over stdio, asks each server for its tool list
(`tools/list`), and later invokes those tools (`tools/call`) -- all over the
real Model Context Protocol via the official `mcp` SDK. The discovered tools are
merged into Orion's own function-calling layer (see tools.py), so the LLM can
pick an MCP tool exactly like a built-in one.

Design
------
The `mcp` SDK is asyncio-based, but Orion's tool layer is synchronous. So this
module runs a single dedicated background thread with its own event loop. That
loop opens one long-lived `ClientSession` per server and keeps them alive for
the life of the process. Synchronous callers (`declarations()`, `call()`) hand
coroutines to that loop with `run_coroutine_threadsafe` and block for the
result.

Everything degrades gracefully: if MCP is disabled, the SDK isn't installed, or
a server fails to start, the public functions just behave as if there are no
MCP tools -- Orion runs normally on its built-in tools alone.

Public API (mirrors tools.py so wiring is trivial):
    declarations() -> list[dict]   # {name, description, parameters} per tool
    call(name, args) -> str        # run an mcp__server__tool, return its text
    tool_names()   -> set[str]     # the full mcp__... names we own
    shutdown()                     # close sessions / stop the loop
"""

import asyncio
import os
import threading

import _paths
import config

# Prefix every MCP tool name with this so they never collide with Orion's
# built-in tools and are instantly recognizable in logs/telemetry.
PREFIX = "mcp__"

# Built lazily on first use, then cached.
_started = False
_start_lock = threading.Lock()
_loop = None                 # the background asyncio loop
_thread = None               # the thread running that loop
_ready = threading.Event()   # set once startup (connect + discover) finishes

_sessions = {}               # server name -> mcp.ClientSession
_declarations = []           # cached tool schemas for the LLM
_routes = {}                 # full "mcp__server__tool" name -> (server, tool)
_shutdown_evt = None         # asyncio.Event living on the background loop


def _enabled():
    """True only if MCP is switched on AND the SDK is importable."""
    if not getattr(config, "MCP_ENABLED", False):
        return False
    try:
        import mcp  # noqa: F401
        return True
    except Exception:
        return False


def _server_params(entry):
    """Turn a config.MCP_SERVERS entry into StdioServerParameters, or None."""
    import sys
    from mcp import StdioServerParameters

    if entry.get("command"):
        command = entry["command"]
        args = list(entry.get("args", []))
    elif entry.get("module"):
        # A script inside this project, run with Orion's own interpreter so it
        # shares the venv (and therefore the installed `mcp` package).
        rel = entry["module"].replace("/", os.sep)
        command = sys.executable
        args = [os.path.join(_paths.ROOT, rel)]
    else:
        return None

    env = dict(os.environ)
    env.update(entry.get("env", {}) or {})
    return StdioServerParameters(command=command, args=args, env=env)


async def _serve():
    """Connect to every configured server and keep the sessions open.

    Runs as a single task on the background loop. Holds all sessions inside one
    AsyncExitStack so a single `shutdown()` tears everything down cleanly.
    """
    global _shutdown_evt
    from contextlib import AsyncExitStack
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    _shutdown_evt = asyncio.Event()

    async with AsyncExitStack() as stack:
        for entry in getattr(config, "MCP_SERVERS", []) or []:
            name = entry.get("name") or "server"
            params = _server_params(entry)
            if params is None:
                continue
            try:
                # Open the stdio transport + session, do the MCP handshake,
                # then ask the server what tools it has. 20s guards a hang.
                read, write = await stack.enter_async_context(
                    stdio_client(params))
                session = await stack.enter_async_context(
                    ClientSession(read, write))
                await asyncio.wait_for(session.initialize(), timeout=20)
                listed = await asyncio.wait_for(session.list_tools(), timeout=20)
            except Exception as exc:
                print(f"[mcp] server '{name}' failed to start: {exc}")
                continue

            _sessions[name] = session
            # Optional per-server safety filter. "allow" = whitelist of bare
            # tool names; "deny" = blacklist. Denied/unlisted tools are never
            # shown to the LLM and never routed -- e.g. a read-only filesystem.
            allow = set(entry.get("allow") or [])
            deny = set(entry.get("deny") or [])
            exposed = 0
            for tool in listed.tools:
                if allow and tool.name not in allow:
                    continue
                if tool.name in deny:
                    continue
                exposed += 1
                full = f"{PREFIX}{name}__{tool.name}"
                _routes[full] = (name, tool.name)
                _declarations.append({
                    "name": full,
                    "description": (tool.description or "").strip(),
                    # MCP calls the JSON schema `inputSchema`; Orion/the LLM
                    # expect it under `parameters`.
                    "parameters": tool.inputSchema
                    or {"type": "object", "properties": {}},
                })
            hidden = len(listed.tools) - exposed
            note = f" ({hidden} hidden)" if hidden else ""
            print(f"[mcp] connected '{name}': {exposed} tool(s){note}")

        # Signal the synchronous side that discovery is done, then idle here
        # keeping the sessions alive until shutdown() fires.
        _ready.set()
        await _shutdown_evt.wait()


def _run_loop():
    """Thread entry point: own event loop that hosts the MCP sessions."""
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    try:
        _loop.run_until_complete(_serve())
    except Exception as exc:
        print(f"[mcp] client loop stopped: {exc}")
        _ready.set()  # unblock waiters even on failure
    finally:
        _loop.close()


def _ensure_started():
    """Start the background client once and wait (briefly) for discovery."""
    global _started, _thread
    if not _enabled():
        return False
    with _start_lock:
        if not _started:
            _started = True
            _thread = threading.Thread(
                target=_run_loop, name="mcp-client", daemon=True)
            _thread.start()
    # Wait for the first connect+discover pass so declarations() is populated.
    # If servers are slow/broken we give up and Orion runs without them.
    _ready.wait(timeout=25)
    return bool(_sessions)


def declarations():
    """Return MCP tool schemas for the LLM, in tools.py's shape. [] if none."""
    if not _enabled():
        return []
    try:
        _ensure_started()
    except Exception as exc:
        print(f"[mcp] startup error: {exc}")
        return []
    return list(_declarations)


def tool_names():
    """The set of full mcp__... names this client owns (for routing)."""
    return set(_routes.keys())


def _extract_text(result):
    """Flatten an MCP call result's content blocks into one spoken string."""
    parts = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    out = " ".join(p.strip() for p in parts if p).strip()
    if getattr(result, "isError", False):
        return out or "That MCP tool reported an error."
    return out


def call(name, args):
    """Run an MCP tool by its full mcp__server__tool name. Returns spoken text."""
    if name not in _routes or _loop is None:
        return f"I don't have an MCP tool called {name}."
    server, tool = _routes[name]
    session = _sessions.get(server)
    if session is None:
        return "That MCP server isn't connected right now."

    async def _do():
        result = await asyncio.wait_for(
            session.call_tool(tool, arguments=args or {}), timeout=30)
        return _extract_text(result)

    try:
        fut = asyncio.run_coroutine_threadsafe(_do(), _loop)
        text = fut.result(timeout=35)
    except Exception as exc:
        print(f"[mcp] call to '{name}' failed: {exc}")
        return "Something went wrong running that MCP tool."
    return text or None


def shutdown():
    """Close all MCP sessions and stop the background loop."""
    if _loop is None or _shutdown_evt is None:
        return
    try:
        _loop.call_soon_threadsafe(_shutdown_evt.set)
    except Exception:
        pass
