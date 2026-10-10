"""MCP server giving agy the Hermes tools it lacks (stdio, launched by agy itself).

agy already has a terminal, files, a browser, web search and image generation. What it
lacks is what makes Hermes Hermes: the shared memory, past conversations, skills it can
write, the scheduler, voice notes and a way to message the user mid-task. The registry
tools keep Hermes' own schema and handler, so both agents read and write the same state.

Run with Hermes' venv python: agy-chat's workspace.py writes the launch line into agy's
mcp_config.json.
"""

import asyncio
import importlib
import io
import json
import os
import sys
from pathlib import Path

# MCP owns stdout; Hermes modules may print while importing or running, so every print
# goes to stderr and the protocol gets the original stream.
_PROTOCOL_OUT = sys.stdout
sys.stdout = sys.stderr

HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
HERMES_SRC = HERMES_HOME / "hermes-agent"
if str(HERMES_SRC) not in sys.path:
    sys.path.insert(0, str(HERMES_SRC))

# Written into agy's mcp_config.json by workspace.py, from the plugin settings.
TARGET_CHAT = os.environ.get("AGY_CHAT_CHAT_ID", "")
USER = os.environ.get("AGY_CHAT_USER") or "the user"
TARGET = f"telegram:{TARGET_CHAT}"
SESSION_KEY = f"agent:main:telegram:dm:{TARGET_CHAT}"

# tool -> (module registering it, note appended to Hermes' description for agy)
REGISTRY_TOOLS = {
    "memory": ("tools.memory_tool", (
        "Memory shared with Hermes: what you write here, Hermes reads on every turn, and vice versa. "
        "Only put durable things in it (preference, stable fact, decision), not a task report.")),
    "session_search": ("tools.session_search_tool", (
        f"Searches {USER}'s past conversations with Hermes (Telegram, CLI, email…).")),
    "skill_manage": ("tools.skill_manager_tool", (
        "Creates or fixes a Hermes skill: both Hermes and you will see it afterwards. "
        "Do it when you found a non-obvious procedure worth reusing.")),
    "cronjob_manage": ("tools.cronjob_tools", (
        f"For {USER}: deliver='{TARGET}'. Reminder or fixed task: no_agent=true + script (free, the script's "
        "output is sent as is). A job with a prompt runs Hermes' LLM, which is paid: suggest it to "
        f"{USER} before creating it.")),
    "text_to_speech": ("tools.tts_tool", (
        "Here the default provider is 'edge' (free). The result holds a path: put MEDIA:<path> on a "
        "line of its own in your answer to send it as a voice note.")),
}

CUSTOM_TOOLS = {
    "telegram_send": {
        "description": (
            f"Sends a Telegram message to {USER} right away: progress update on long work, or a file to "
            "deliver before the end (a MEDIA:/absolute/path line in the message). Your final answer is "
            "sent anyway: don't use this to duplicate it."),
        "inputSchema": {
            "type": "object",
            "properties": {"message": {"type": "string", "description": "Text (simple Markdown)."}},
            "required": ["message"],
        },
    },
    "hermes_thread": {
        "description": (
            f"Latest messages between {USER} and Hermes on Telegram (the thread you don't see). Useful "
            f"when {USER} refers to something said to or asked of Hermes."),
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "Number of messages (default 20, max 100)."}},
        },
    },
}

_loaded = {}


def _boot():
    from hermes_cli.env_loader import load_hermes_dotenv
    load_hermes_dotenv()  # the Telegram token and tool settings live in Hermes' .env


def _registry_entry(name):
    from tools.registry import registry
    if name not in _loaded:
        importlib.import_module(REGISTRY_TOOLS[name][0])
        _loaded[name] = True
    return registry.get_entry(name)


def list_tools():
    tools = []
    for name, (_module, note) in REGISTRY_TOOLS.items():
        try:
            entry = _registry_entry(name)
        except Exception as e:  # one broken Hermes module must not hide the other tools
            print(f"hermes-mcp: {name} unavailable: {e}", file=sys.stderr)
            continue
        if entry is None:
            continue
        schema = entry.schema
        tools.append({"name": name, "description": f"{schema.get('description', '')}\n\n{note}".strip(),
                      "inputSchema": schema.get("parameters") or {"type": "object", "properties": {}}})
    tools += [{"name": name, **spec} for name, spec in CUSTOM_TOOLS.items()]
    return tools


def call_tool(name, args):
    args = dict(args or {})
    if name == "telegram_send":
        from tools.send_message_tool import send_message_tool
        return send_message_tool({"action": "send", "target": TARGET, "message": str(args.get("message") or "")})
    if name == "hermes_thread":
        return json.dumps(hermes_thread(args.get("limit") or 20), ensure_ascii=False)
    if name not in REGISTRY_TOOLS:
        return json.dumps({"error": f"Unknown tool: {name}"})
    if name == "text_to_speech":
        args.setdefault("provider", "edge")
    from tools.registry import registry
    _registry_entry(name)
    result = registry.dispatch(name, args)
    return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)


def hermes_thread(limit, max_chars=1500):
    """Recent text messages of the user's Telegram DM session with Hermes, oldest first."""
    from hermes_state_registry import acquire, release_or_close
    limit = max(1, min(int(limit), 100))
    db = acquire()
    try:
        row = next((r for r in db.list_gateway_sessions(active_only=True)
                    if r.get("session_key") == SESSION_KEY), None)
        if row is None:
            return {"messages": [], "note": "no active Telegram session"}
        messages = []
        for msg in db.get_messages(row["id"]):
            content = msg.get("content")
            if msg.get("role") not in ("user", "assistant") or not isinstance(content, str) or not content.strip():
                continue
            messages.append({"role": USER if msg["role"] == "user" else "Hermes",
                             "at": _iso(msg.get("timestamp")), "text": content.strip()[:max_chars]})
        return {"session_title": row.get("title") or "", "messages": messages[-limit:]}
    finally:
        release_or_close(db)


def _iso(ts):
    from datetime import datetime
    try:
        return datetime.fromtimestamp(float(ts)).isoformat(timespec="minutes")
    except (TypeError, ValueError, OSError):
        return ""


async def _serve():
    import anyio
    from mcp import types
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server

    async def on_list_tools(_ctx, _params):
        tools = await asyncio.to_thread(list_tools)
        return types.ListToolsResult(tools=[
            types.Tool(name=t["name"], description=t["description"], input_schema=t["inputSchema"]) for t in tools])

    async def on_call_tool(_ctx, params):
        try:
            text = await asyncio.to_thread(call_tool, params.name, params.arguments)
            failed = _is_error(text)
        except Exception as e:
            text, failed = json.dumps({"error": f"{type(e).__name__}: {e}"}), True
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=failed)

    server = Server("hermes", instructions="Hermes' tools (memory, history, skills, scheduling, Telegram).",
                    on_list_tools=on_list_tools, on_call_tool=on_call_tool)
    stdout = anyio.wrap_file(io.TextIOWrapper(_PROTOCOL_OUT.buffer, encoding="utf-8"))
    async with stdio_server(stdout=stdout) as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def _is_error(text):
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return False
    return isinstance(data, dict) and (data.get("success") is False or
                                       (bool(data.get("error")) and not data.get("success")))


if __name__ == "__main__":
    _boot()
    asyncio.run(_serve())
