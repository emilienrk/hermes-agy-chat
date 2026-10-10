"""Talk to Antigravity (agy) from Telegram: an alternative to Hermes' paid LLM.

What reaches agy (the configured chat only):
  a reply            to one of agy's messages
  everything         while the agy mode is on (/agy_on … /agy_off, shown by a pinned banner);
                     slash commands and messages starting with "Hermes" still go to Hermes,
                     /stop and /new act on agy
  /agy <message>     the explicit form
Photos, files and voice notes ride along; voice notes are transcribed locally first.

Routed messages are taken in the pre_gateway_dispatch hook, before Hermes spends a token.
engine.py runs the turns (typing indicator, live status in verbose mode, queue for messages
sent meanwhile), workspace.py gives agy what Hermes knows (context, skills, MCP servers) and
hermes_mcp.py serves the Hermes tools agy lacks.

agy runs as the gateway's user, with --dangerously-skip-permissions: whoever can write in the
configured chat can run anything that user can. Without a chat_id setting, nothing is routed.
"""

import asyncio
import logging
import re
import subprocess
import time
from itertools import zip_longest
from pathlib import Path

from . import engine, workspace
from . import state as store

logger = logging.getLogger(__name__)

MODELS_TTL_S = 3600
INLINE_DOC_MAX_BYTES = 100 * 1024
_SLASH = re.compile(r"^/([A-Za-z0-9_-]+)(?:@\w+)?(?=\s|$)")
_HERMES_WORD = re.compile(r"^\s*herm[eè]s(?=$|[\s,:;.!?])", re.I)

_models_cache = {"at": 0.0, "models": []}


def register(ctx):
    configure(ctx.get_config)
    for name, handler, description, hint in (
        ("agy", _cmd_agy, "agy: status, or a message for it", "[message]"),
        ("agy-on", _cmd_on, "agy mode: everything goes to agy", ""),
        ("agy-off", _cmd_off, "End agy mode, back to Hermes", ""),
        ("agy-new", _cmd_new, "New agy conversation", "[message]"),
        ("agy-stop", _cmd_stop, "Stop agy and drop its queue", ""),
        ("agy-model", _cmd_model, "Choose agy's model", "[number or name]"),
        ("agy-verbose", _cmd_verbose, "Show/hide agy's actions", "[on|off]"),
        ("agy-voice", _cmd_voice, "Voice notes: straight to agy or local transcription", "[direct|local]"),
        ("agy-conv", _cmd_conv, "Resume an agy conversation", "[number]"),
        ("agy-retry", _cmd_retry, "Send my last message to agy again", ""),
        ("agy-doctor", _cmd_doctor, "Check the agy integration", ""),
    ):
        ctx.register_command(name, handler=handler, description=description, args_hint=hint)
    ctx.register_hook("pre_gateway_dispatch", _on_dispatch)
    ctx.register_telegram_handler(_on_telegram_connect)


def configure(get):
    """Apply the plugin settings (plugins.entries.agy-chat.settings in config.yaml)."""
    chat_id = str(get("chat_id", "") or "").strip()
    if not chat_id:
        logger.warning("agy-chat: no chat_id setting, nothing will be routed to agy "
                       "(hermes config set plugins.entries.agy-chat.settings.chat_id <telegram chat id>)")
    engine.configure(chat_id, get("agy_path", ""))
    workspace.configure(chat_id=chat_id, user=get("user_name", ""), workspace=get("workspace", ""),
                        machine_notes=get("machine_notes", ""), skip_skills=get("skip_skills", []) or [])
    store.DEFAULT_MODEL = get("default_model", "") or store.DEFAULT_MODEL


def _on_telegram_connect(_application, adapter):
    # Called by the Telegram adapter at connect(): this is the gateway, and the live bot.
    engine.bind_adapter(adapter)
    engine.schedule_startup()


# ── routing ──

async def _on_dispatch(event=None, gateway=None, **_kwargs):
    """Take agy's messages before auth and dispatch; the configured chat only, since agy runs unsandboxed."""
    try:
        source = getattr(event, "source", None)
        if source is None or getattr(event, "internal", False) or not getattr(event, "allow_gateway_control", True):
            return None
        if (not engine.TARGET_CHAT or getattr(source.platform, "value", None) != "telegram"
                or str(source.chat_id) != engine.TARGET_CHAT):
            return None
        if gateway is not None:
            engine.bind_gateway(gateway)
        decision = route(event, store.load())
        if decision is None:
            return None
        await _act(decision, event)
        return {"action": "skip", "reason": "agy-chat"}
    except Exception:
        logger.exception("agy-chat: dispatch failed")
        return None


def route(event, state):
    """("job", Job) | ("stop",) | ("new",) | None (the message stays Hermes')."""
    media = [str(m) for m in (getattr(event, "media_urls", None) or [])]
    caption = caption_of(event.text or "", media)
    sticky = bool(state.get("sticky"))
    to_agy = store.is_agy_message(state, getattr(event, "reply_to_message_id", None))

    command = _SLASH.match(caption)
    if command:
        name = command.group(1).lower().replace("_", "-")
        rest = caption[command.end():]
        if name in ("agy", "agy-new") and (rest.strip() or media):
            return "job", _job(event, rest, media, to_agy, new=name == "agy-new")
        if sticky and name == "stop" and engine.busy():
            return ("stop",)
        if sticky and name == "new":
            return ("new",)
        return None
    if sticky and _HERMES_WORD.match(caption):
        return None
    if to_agy or sticky:
        return "job", _job(event, caption, media, to_agy)
    return None


def caption_of(text, media):
    """What the user typed: inlined text documents and the adapter's file notes removed."""
    for path in media:
        body = _small_text(path)
        if not body or body not in text:
            continue
        end = text.index(body) + len(body)
        start = text.rfind("[Content of ", 0, end - len(body))
        text = text[:start if start != -1 else end - len(body)] + text[end:]
    kept = [line for line in text.splitlines()
            if not (line.strip().startswith("[") and any(m in line for m in media))]
    return "\n".join(kept).strip()


def _small_text(path):
    try:
        p = Path(path)
        if p.stat().st_size > INLINE_DOC_MAX_BYTES:
            return ""
        return p.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _job(event, text, media, to_agy, new=False):
    kind = getattr(getattr(event, "message_type", None), "value", "")
    voice = media[:1] if kind == "voice" else []
    types = list(getattr(event, "media_types", None) or [])
    files = [(m, t or "") for m, t in zip_longest(media, types) if m and m not in voice]
    quoted = "" if to_agy else (getattr(event, "reply_to_text", None) or "")
    return engine.Job(text=text.strip(), files=files, voice=voice, message_id=getattr(event, "message_id", None),
                      quoted=quoted, new_conversation=new)


async def _act(decision, event):
    if decision[0] == "job":
        await engine.submit(decision[1])
    elif decision[0] == "stop":
        await engine.send(_stop_text(), reply_to=event.message_id)
    elif decision[0] == "new":
        store.update(conversation_id=None)
        await engine.send("🆕 New agy conversation from the next message.", reply_to=event.message_id)


# ── commands ──

async def _cmd_agy(raw_args: str):
    if raw_args.strip():  # normally taken by the dispatch hook; this is the fallback
        await engine.submit(engine.Job(text=raw_args.strip()))
        return None
    return _status_card()


def _status_card():
    state, snap = store.load(), engine.snapshot()
    lines = [f"{engine.MARK} agy (Antigravity)"]
    if state.get("sticky"):
        lines.append("agy mode: your messages go to agy (\"Hermes …\" and / for Hermes) · /agy_off")
    else:
        lines.append("Hermes mode: agy when you reply to agy, with /agy <message>, or /agy_on")
    if not engine.TARGET_CHAT:
        lines.append("⚠️ No chat_id setting: nothing is routed to agy (see /agy_doctor)")
    lines.append(f"Model: {store.model(state)}")
    conv = state.get("conversation_id")
    lines.append(f"Conversation: {conv[:8] + '…' if conv else 'new from the next message'}")
    lines.append(f"Display: {'detailed' if state.get('verbose') else 'discreet'} (/agy_verbose)")
    vmode = store.voice_mode(state)
    lines.append(f"Voice notes: {'straight to agy (Google)' if vmode == 'direct' else 'local transcription (Whisper)'} (/agy_voice)")
    if snap["busy"]:
        busy = f"Running for {snap['elapsed']} · {snap['actions']} actions"
        if snap["current"]:
            busy += f" · ▶ {snap['current']}"
        if snap["queued"]:
            busy += f" · {snap['queued']} queued"
        lines.append(busy)
    lines.append("\n/agy_on · /agy_off · /agy_new · /agy_stop · /agy_model · /agy_verbose · /agy_voice · /agy_conv · "
                 "/agy_retry · /agy_doctor")
    return "\n".join(lines)


async def _cmd_on(_raw_args: str):
    already = store.load().get("sticky")
    store.update(sticky=True)
    if await engine.set_mode_banner(True):
        return "🛸 agy mode already on." if already else None
    return engine.MODE_BANNER


async def _cmd_off(_raw_args: str):
    store.update(sticky=None)
    await engine.set_mode_banner(False)
    return "↩️ Back to Hermes. agy stays reachable with /agy <message> or by replying to its messages."


async def _cmd_new(raw_args: str):
    store.update(conversation_id=None)
    if raw_args.strip():
        await engine.submit(engine.Job(text=raw_args.strip(), new_conversation=True))
        return None
    return "🆕 Next message to agy = new conversation."


async def _cmd_stop(_raw_args: str):
    return _stop_text()


def _stop_text():
    running, dropped = engine.stop()
    if not running and not dropped:
        return "Nothing running."
    text = "🛑 Stop requested." if running else "🛑"
    if dropped:
        text += f" {dropped} queued message(s) dropped."
    return text


async def _cmd_model(raw_args: str):
    choice = raw_args.strip()
    current = store.model(store.load())
    models = await asyncio.to_thread(_list_models)
    if not choice:
        if not models:
            return f"Current model: {current}\n(list unavailable: `agy models` failed)"
        sent = await _picker("agy's model:", [
            {"value": name, "label": _short_model(label or name), "is_current": name == current}
            for name, label in models], _pick_model)
        if sent:
            return None
        rows = [f"{'▶' if name == current else ' '} {i}. {name} — {label}" for i, (name, label) in enumerate(models, 1)]
        return "agy models:\n" + "\n".join(rows) + "\n\n/agy_model <number> or <name> to switch."
    names = [name for name, _ in models]
    if choice.isdigit() and models:
        index = int(choice) - 1
        if not 0 <= index < len(models):
            return f"Invalid number: 1 to {len(models)}."
        choice = names[index]
    elif models and choice not in names:
        return f"Unknown model: {choice}\n/agy_model to see the list."
    store.update(model=choice)
    return f"agy model → {choice}" + ("" if models else "\n⚠️ List unavailable: name not checked.")


async def _pick_model(_chat_id, value):
    store.update(model=value)
    return f"agy model → {value}"


def _short_model(label):
    return label.replace("Gemini ", "").replace(" (Thinking)", "").replace("Claude ", "")


def _list_models():
    if time.time() - _models_cache["at"] < MODELS_TTL_S and _models_cache["models"]:
        return _models_cache["models"]
    try:
        out = subprocess.run([engine.AGY_BIN, "models"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return _models_cache["models"]
    models = [tuple(line.split("\t", 1)) for line in out.splitlines() if "\t" in line]
    if models:
        _models_cache.update(at=time.time(), models=models)
    return models


async def _cmd_verbose(raw_args: str):
    arg = raw_args.strip().lower()
    if arg in ("on", "yes", "1"):
        verbose = True
    elif arg in ("off", "no", "0"):
        verbose = False
    else:
        verbose = not store.load().get("verbose")
    store.update(verbose=verbose or None)
    if verbose:
        return "Detailed display: live status (actions, what agy writes) then a summary of the actions."
    return "Discreet display: \"typing…\" while agy works, then its answer only."


async def _cmd_voice(raw_args: str):
    arg = raw_args.strip().lower()
    current = store.voice_mode(store.load())
    if arg in ("direct", "google", "agy"):
        mode = "direct"
    elif arg in ("local", "whisper"):
        mode = "local"
    elif not arg:
        mode = "local" if current == "direct" else "direct"
    else:
        return f"Invalid voice mode: `{arg}`. Use `direct` (Google) or `local` (Whisper)."
    store.update(voice=mode)
    if mode == "direct":
        return "🎙 Voice notes → direct mode: the raw audio file goes to agy (Google)."
    return "🎙 Voice notes → local mode (Whisper transcription on this machine)."


async def _cmd_conv(raw_args: str):
    convs = await asyncio.to_thread(store.conversations)
    current = store.load().get("conversation_id")
    if not convs:
        return "No agy conversation recorded."
    arg = raw_args.strip()
    if arg:
        if not arg.isdigit() or not 1 <= int(arg) <= len(convs):
            return f"Invalid number: 1 to {len(convs)}."
        return await _pick_conv(None, convs[int(arg) - 1]["id"])
    choices = [{"value": c["id"], "label": f"{c['last'][5:10]} · {_cut(c['first'], 28)}",
                "is_current": c["id"] == current} for c in convs]
    if await _picker("Resume an agy conversation:", choices, _pick_conv):
        return None
    rows = [f"{'▶' if c['id'] == current else ' '} {i}. {c['last'][:16].replace('T', ' ')} · {c['turns']} turns · "
            f"{_cut(c['first'], 60)}" for i, c in enumerate(convs, 1)]
    return "agy conversations:\n" + "\n".join(rows) + "\n\n/agy_conv <number> to resume."


async def _pick_conv(_chat_id, value):
    conv = next((c for c in store.conversations(50) if c["id"] == value), None)
    store.update(conversation_id=value)
    first = f" (\"{_cut(conv['first'], 60)}\")" if conv else ""
    return f"agy conversation resumed{first}."


async def _picker(title, choices, on_selected):
    """Inline-keyboard picker through the adapter; False when unavailable."""
    ad = engine.adapter()
    picker = getattr(ad, "send_choice_picker", None)
    if picker is None:
        return False
    try:
        result = await picker(engine.TARGET_CHAT, title, choices, f"agy-chat:{engine.TARGET_CHAT}", on_selected)
    except Exception:
        logger.exception("agy-chat: picker failed")
        return False
    return bool(getattr(result, "success", False))


async def _cmd_retry(_raw_args: str):
    last = store.load().get("last_turn")
    if not last or not last.get("text"):
        return "Nothing to send again."
    queued = await engine.submit(engine.Job(text=last["text"]))
    return "🔁 Sent to agy again" + (" (after the current answer)." if queued else ".")


async def _cmd_doctor(_raw_args: str):
    from . import doctor
    return await doctor.report()


def _cut(text, limit):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"
