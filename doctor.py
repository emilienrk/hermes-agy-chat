"""/agy_doctor: check every link of the chain without running an agy turn."""

import asyncio
import json
import subprocess
import time
from pathlib import Path

from . import engine, workspace

TOKEN_FILE = Path.home() / ".gemini/antigravity-cli/antigravity-oauth-token"
AGY_SETTINGS = Path.home() / ".gemini/antigravity-cli/settings.json"
COMMANDS = ("agy", "agy_on", "agy_off", "agy_new", "agy_stop", "agy_model", "agy_verbose", "agy_voice",
            "agy_conv", "agy_retry", "agy_doctor")


async def report():
    checks = await asyncio.gather(
        asyncio.to_thread(_chat), asyncio.to_thread(_binary), asyncio.to_thread(_token), asyncio.to_thread(_workspace),
        _bridge(), asyncio.to_thread(_menu), asyncio.to_thread(_trusted), return_exceptions=True)
    lines = []
    for check in checks:
        if isinstance(check, Exception):
            lines.append(f"✗ {type(check).__name__}: {check}")
        else:
            lines.extend(check)
    lines.append("✓ Telegram adapter bound" if engine.adapter() is not None
                 else "✗ Telegram adapter not bound (answers go through Hermes' standalone sender)")
    ok = all(line.startswith("✓") for line in lines)
    return ("🩺 agy: all good" if ok else "🩺 agy: needs a look") + "\n" + "\n".join(lines)


def _chat():
    if not engine.TARGET_CHAT:
        return ["✗ no chat_id setting: hermes config set plugins.entries.agy-chat.settings.chat_id <id>, "
                "then restart the gateway"]
    return [f"✓ routed chat: {engine.TARGET_CHAT}"]


def _binary():
    try:
        out = subprocess.run([engine.AGY_BIN, "--version"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        return [f"✗ agy missing or silent ({engine.AGY_BIN}): {e}"]
    return [f"✓ agy {out.stdout.strip() or '?'}"]


def _token():
    try:
        age_h = (time.time() - TOKEN_FILE.stat().st_mtime) / 3600
    except OSError:
        return ["✗ no Antigravity token: run `agy` once on the machine to log in"]
    return [f"✓ Antigravity token refreshed {age_h:.0f} h ago"]


def _workspace():
    summary = workspace.prepare()
    lines = [f"✓ Hermes context: {summary['rules_bytes'] // 1024} KB of rules, {summary['skills']} skills"]
    lines.append(f"✓ MCP servers for agy: {', '.join(summary['mcp_servers'])}")
    lines += [f"✗ {error}" for error in summary["errors"]]
    return lines


async def _bridge():
    """Start hermes_mcp.py like agy would and list its tools."""
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as e:
        return [f"✗ cannot test the MCP bridge: {e}"]
    config = json.loads(workspace.MCP_CONFIG.read_text())["mcpServers"]["hermes"]
    params = StdioServerParameters(command=config["command"], args=config["args"],
                                   env={"HOME": str(Path.home()), "PATH": "/usr/bin:/bin", **config.get("env", {})})

    async def probe():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return [tool.name for tool in (await session.list_tools()).tools]
    try:
        names = await asyncio.wait_for(probe(), 30)
    except Exception as e:
        return [f"✗ hermes MCP bridge: {type(e).__name__}: {e}"]
    return [f"✓ hermes MCP bridge: {', '.join(names)}"]


def _menu():
    from hermes_cli.commands_platforms import telegram_menu_commands, telegram_menu_max_commands
    menu, _hidden = telegram_menu_commands(max_commands=telegram_menu_max_commands())
    missing = [c for c in COMMANDS if c not in {name for name, _ in menu}]
    if missing:
        return [f"✗ missing from Telegram's / menu: {', '.join('/' + c for c in missing)} "
                "(platforms.telegram.extra.command_menu.priority in config.yaml)"]
    return ["✓ agy commands in Telegram's / menu"]


def _trusted():
    try:
        trusted = json.loads(AGY_SETTINGS.read_text()).get("trustedWorkspaces", [])
    except (OSError, ValueError):
        return [f"✗ cannot read agy's {AGY_SETTINGS}"]
    if str(workspace.WORKSPACE) in trusted:
        return [f"✓ {workspace.WORKSPACE} is a trusted workspace for agy"]
    return [f"✗ {workspace.WORKSPACE} missing from agy's trustedWorkspaces ({AGY_SETTINGS})"]

