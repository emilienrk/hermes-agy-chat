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
import json
import logging
import re
import subprocess
import time
from datetime import datetime, timedelta
from itertools import zip_longest
from pathlib import Path

from . import config, engine, workspace
from . import state as store

logger = logging.getLogger(__name__)

MODELS_TTL_S = 3600
INLINE_DOC_MAX_BYTES = 100 * 1024
_SLASH = re.compile(r"^/([A-Za-z0-9_-]+)(?:@\w+)?(?=\s|$)")
# agy encodes the reasoning effort in the model id (gemini-3.8-flash-high); its --effort flag
# conflicts with those ids, so /agy_effort switches to a sibling variant instead.
_EFFORT_SUFFIX = re.compile(r"-(low|medium|high|xhigh|max)$")
_HERMES_WORD = re.compile(r"^\s*herm[eè]s(?=$|[\s,:;.!?])", re.I)

HANDOFF_THREAD_MESSAGES = 30
HANDBACK_TURNS = 8

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
        ("agy-effort", _cmd_effort, "agy's reasoning effort (model variant)", "[low|medium|high]"),
        ("agy-verbose", _cmd_verbose, "Show/hide agy's actions", "[on|off]"),
        ("agy-voice", _cmd_voice, "Voice notes in, and voice replies", "[direct|local|reply off|mirror|always]"),
        ("agy-conv", _cmd_conv, "Resume an agy conversation", "[number]"),
        ("agy-retry", _cmd_retry, "Send my last message to agy again", ""),
        ("agy-btw", _cmd_btw, "Side question, answered now, next to the main conversation", "<question>"),
        ("agy-handoff", _cmd_handoff, "Hand the Hermes conversation over to agy", "[message]"),
        ("agy-back", _cmd_back, "Hand the agy conversation back to Hermes", "[message]"),
        ("agy-task", _cmd_task, "Tasks agy runs on a schedule", "[add <when> | <prompt> · rm n · run n]"),
        ("agy-usage", _cmd_usage, "Antigravity quota left, and agy turns and tokens", ""),
        ("agy-doctor", _cmd_doctor, "Check the agy integration", ""),
    ):
        ctx.register_command(name, handler=handler, description=description, args_hint=hint)
    ctx.register_hook("pre_gateway_dispatch", _on_dispatch)
    ctx.register_telegram_handler(_on_telegram_connect)


def configure(get):
    """Apply the plugin settings (plugins.entries.agy-chat.settings in config.yaml)."""
    cfg = config.load(get)
    if not cfg["chat_id"]:
        logger.warning("agy-chat: no chat_id setting, nothing will be routed to agy "
                       "(hermes config set plugins.entries.agy-chat.settings.chat_id <telegram chat id>)")
    engine.configure()
    workspace.configure()
    store.DEFAULT_MODEL = cfg["default_model"] or store.DEFAULT_MODEL
    store.DEFAULT_VOICE, store.DEFAULT_VOICE_REPLY = cfg["voice_input"], cfg["voice_reply"]
    store.DEFAULT_VERBOSE = bool(cfg["verbose"])


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
        return await _act(decision, event) or {"action": "skip", "reason": "agy-chat"}
    except Exception:
        logger.exception("agy-chat: dispatch failed")
        return None


def route(event, state):
    """("job", Job) | ("btw", Job) | ("stop",) | ("new",) | ("handoff", text) | ("back", text) | None
    (the message stays Hermes')."""
    media = [str(m) for m in (getattr(event, "media_urls", None) or [])]
    caption = caption_of(event.text or "", media)
    sticky = bool(state.get("sticky"))
    to_agy = store.is_agy_message(state, getattr(event, "reply_to_message_id", None))
    to_btw = store.is_agy_message(state, getattr(event, "reply_to_message_id", None), "btw_msgs")

    command = _SLASH.match(caption)
    if command:
        name = command.group(1).lower().replace("_", "-")
        rest = caption[command.end():]
        if name in ("agy", "agy-new") and (rest.strip() or media):
            return "job", _job(event, rest, media, to_agy, new=name == "agy-new")
        if name == "agy-btw" and (rest.strip() or media):
            return "btw", _job(event, rest, media, True, new=True, side=True)
        if name in ("agy-handoff", "agy-back"):
            return name[4:], rest.strip()
        if sticky and name == "stop" and engine.busy():
            return ("stop",)
        if sticky and name == "new":
            return ("new",)
        return None
    if sticky and _HERMES_WORD.match(caption):
        return None
    if to_btw:
        return "btw", _job(event, caption, media, True, side=True)
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


def _job(event, text, media, to_agy, new=False, side=False):
    kind = getattr(getattr(event, "message_type", None), "value", "")
    voice = media[:1] if kind == "voice" else []
    types = list(getattr(event, "media_types", None) or [])
    files = [(m, t or "") for m, t in zip_longest(media, types) if m and m not in voice]
    quoted = "" if to_agy else (getattr(event, "reply_to_text", None) or "")
    return engine.Job(text=text.strip(), files=files, voice=voice, message_id=getattr(event, "message_id", None),
                      quoted=quoted, new_conversation=new, side=side)


async def _act(decision, event):
    """Run a routing decision; a dict is the hook's answer instead of the default skip."""
    if decision[0] == "handoff":
        await _handoff(decision[1], event.message_id)
    elif decision[0] == "back":
        return {"action": "rewrite", "text": await _hand_back(decision[1])}
    elif decision[0] == "btw":
        decision[1].model = await _btw_model()
        engine.side(decision[1])
    elif decision[0] == "job":
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
    lines.append(f"Display: {'detailed' if store.verbose(state) else 'discreet'} (/agy_verbose)")
    vmode = store.voice_mode(state)
    lines.append(f"Voice notes: {'straight to agy (Google)' if vmode == 'direct' else 'local transcription (Whisper)'}"
                 f" · voice replies: {store.voice_reply(state)} (/agy_voice)")
    if snap["busy"]:
        busy = f"Running for {snap['elapsed']} · {snap['actions']} actions"
        if snap["current"]:
            busy += f" · ▶ {snap['current']}"
        if snap["queued"]:
            busy += f" · {snap['queued']} queued"
        lines.append(busy)
    lines.append("\n/agy_on · /agy_off · /agy_new · /agy_stop · /agy_model · /agy_effort · /agy_verbose · /agy_voice · /agy_conv · "
                 "/agy_retry · /agy_btw · /agy_handoff · /agy_back · /agy_task · /agy_usage · /agy_doctor")
    return "\n".join(lines)


async def _cmd_on(_raw_args: str):
    already = store.load().get("sticky")
    store.update(sticky=True)
    if await engine.set_mode_banner(True):
        return f"{engine.MARK} agy mode already on.".strip() if already else None
    return engine.mode_banner()


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


async def _cmd_effort(raw_args: str):
    choice = raw_args.strip().lower()
    current = store.model(store.load())
    variants = _effort_variants(current, [name for name, _ in await asyncio.to_thread(_list_models)])
    if not variants:
        return f"{current} has no effort variants (/agy_model to pick another model)."
    level = _EFFORT_SUFFIX.search(current).group(1)
    if not choice:
        if await _picker("agy's reasoning effort:", [
                {"value": model, "label": effort, "is_current": model == current}
                for effort, model in variants.items()], _pick_model):
            return None
        return f"Effort: {level} ({current})\n/agy_effort {' | '.join(variants)}"
    if choice not in variants:
        return f"No {choice} effort for this model: {', '.join(variants)}."
    return await _pick_model(None, variants[choice])


def _effort_variants(model, models):
    """{effort: model id} for the variants of ``model``'s family, in `agy models` order."""
    if not _EFFORT_SUFFIX.search(model):
        return {}
    family = _EFFORT_SUFFIX.sub("", model)
    return {m[len(family) + 1:]: m for m in models if _EFFORT_SUFFIX.sub("", m) == family and m != family}


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
        verbose = not store.verbose(store.load())
    store.update(verbose=verbose)
    if verbose:
        return "Detailed display: live status (actions, what agy writes) then a summary of the actions."
    return "Discreet display: \"typing…\" while agy works, then its answer only."


async def _cmd_voice(raw_args: str):
    arg = raw_args.strip().lower()
    if arg.split()[:1] == ["reply"]:
        return _voice_reply(arg.split()[1:])
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


def _voice_reply(args):
    if not args:
        return (f"Voice replies: {store.voice_reply(store.load())}.\n"
                "/agy_voice reply off · mirror (when you sent a voice note) · always")
    if args[0] not in config.VOICE_REPLY_MODES:
        return f"Invalid mode: `{args[0]}`. Use off, mirror or always."
    store.update(voice_reply=args[0])
    return {"off": "🔇 Voice replies off.",
            "mirror": "🔊 agy answers your voice notes with a voice note too.",
            "always": "🔊 agy also answers every message with a voice note."}[args[0]]


# ── handoff ──

async def _cmd_handoff(raw_args: str):
    await _handoff(raw_args.strip())  # normally taken by the dispatch hook; this is the fallback
    return None


async def _handoff(message, reply_to=None):
    """New agy conversation seeded with the Hermes thread, and agy mode on."""
    thread = await asyncio.to_thread(workspace.hermes_thread, HANDOFF_THREAD_MESSAGES)
    user = workspace.USER
    text = (f"Hermes hands this conversation with {user} over to you. Recent thread between {user} and "
            f"Hermes:\n{thread or '(empty)'}\n\n"
            + (message or f"Pick up where Hermes left off: in a few lines, say where things stand and what "
                          f"you will do next, then wait for {user}."))
    store.update(sticky=True)
    await engine.set_mode_banner(True)
    await engine.submit(engine.Job(text=text, message_id=reply_to, new_conversation=True))


async def _cmd_back(raw_args: str):
    # Normally taken by the dispatch hook, which hands the recap to Hermes as the message itself.
    await _hand_back("")
    return "↩️ Back to Hermes."


async def _hand_back(message):
    """agy mode off; returns the message Hermes gets: the agy conversation's recap, then ``message``."""
    state = store.load()
    store.update(sticky=None)
    await engine.set_mode_banner(False)
    user = workspace.USER
    recap = await asyncio.to_thread(store.recap, state.get("conversation_id"), HANDBACK_TURNS, user, 1200)
    head = (f"[Handover from agy (Antigravity): {user} worked with agy and comes back to you. Their last "
            f"exchanges:\n{recap or '(none)'}]\n\n")
    return head + (message or "Take over from here: in a few lines, say where things stand and what's next.")


# ── tasks ──

async def _cmd_task(raw_args: str):
    from . import tasks
    verb, _, rest = raw_args.strip().partition(" ")
    verb = verb.lower()
    try:
        if verb == "add":
            when, sep, prompt = rest.partition("|")
            if not sep or not when.strip() or not prompt.strip():
                return ("Usage: /agy_task add <when> | <prompt>\n"
                        "when: every day at 8am · every monday 9am · 0 7 * * 1-5 · in 2h · every 6h")
            task = await asyncio.to_thread(tasks.create, when, prompt, task_settings())
            return (f"⏰ agy task \"{task['name']}\" scheduled ({task['schedule']}"
                    f"{', next ' + _when(task['next_run_at']) if task.get('next_run_at') else ''}).")
        listed = await asyncio.to_thread(tasks.list_tasks)
        if verb in ("rm", "run"):
            if not rest.strip().isdigit() or not 1 <= int(rest) <= len(listed):
                return f"Invalid number: 1 to {len(listed)}." if listed else "No agy task."
            task = listed[int(rest) - 1]
            if verb == "rm":
                await asyncio.to_thread(tasks.remove, task["job_id"])
                return f"🗑 agy task \"{task['name']}\" removed."
            await asyncio.to_thread(tasks.run_now, task["job_id"])
            return f"▶ agy task \"{task['name']}\" started: its answer will arrive here."
    except Exception as e:
        logger.exception("agy-chat: /agy_task failed")
        return f"❌ agy task: {e}"
    if not listed:
        return ("No agy task. A task runs a prompt on agy on a schedule, free of Hermes' LLM:\n"
                "/agy_task add every day at 8am | summarize my unread mails")
    rows = [f"{i}. {t['name']} · {t['schedule']}"
            + (f" · next {_when(t['next_run_at'])}" if t.get("next_run_at") else "")
            + (f" · {t['state']}" if t.get("state") not in (None, "scheduled", "active") else "")
            for i, t in enumerate(listed, 1)]
    return ("agy tasks:\n" + "\n".join(rows)
            + "\n\n/agy_task rm <n> · /agy_task run <n> · /agy_task add <when> | <prompt>")


def task_settings():
    """What a task's script needs to run agy outside the gateway."""
    return {"agy_bin": engine.AGY_BIN, "workspace": str(workspace.WORKSPACE), "model": "",
            "timeout_minutes": config.cfg["timeout_minutes"], "user": workspace.USER, "mark": engine.MARK,
            "chat_id": engine.TARGET_CHAT}


def _when(iso):
    return str(iso or "")[:16].replace("T", " ")


# ── usage ──

async def _cmd_usage(_raw_args: str):
    quota, turns = await asyncio.gather(asyncio.to_thread(_quota), asyncio.to_thread(store.recent_turns))
    head = f"{engine.MARK} agy usage".strip()
    quota = "Antigravity quota left:\n" + quota if quota else "Antigravity quota: unavailable (`agy -p /usage` failed)"
    return f"{head}\n{quota}\n\n" + (_turn_stats(turns) if turns else "No agy turn recorded.")


def _quota():
    """The subscription's remaining quota, as `agy -p /usage` reports it (costs nothing)."""
    try:
        out = subprocess.run([engine.AGY_BIN, "-p=/usage", "--output-format", "json"], capture_output=True,
                             text=True, timeout=30, cwd=str(workspace.WORKSPACE), stdin=subprocess.DEVNULL)
        groups = json.loads(out.stdout)["command"]["data"]["groups"]
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        logger.warning("agy-chat: agy /usage failed", exc_info=True)
        return ""
    lines = []
    for group in groups:
        buckets = [f"{_WINDOWS.get(b.get('window'), b.get('window'))} {round(100 * float(b.get('remaining_fraction', 0)))}%"
                   f" (reset {_local(b.get('reset_time'))})" for b in group.get("buckets", [])]
        lines.append(f"{group.get('name', '?')}: " + " · ".join(buckets))
    return "\n".join(lines)


_WINDOWS = {"weekly": "week", "5h": "5 h"}


def _local(iso):
    try:
        when = datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(workspace.local_tz())
    except ValueError:
        return "?"
    return f"{when:%H:%M}" if when.date() == workspace.local_now().date() else f"{when:%a %H:%M}"


def _turn_stats(turns):
    today = datetime.now().date()
    rows = []
    for label, days in (("Today", 1), ("7 days", 7), ("30 days", 30)):
        since = (today - timedelta(days=days - 1)).isoformat()
        window = [t for t in turns if (t.get("at") or "") >= since]
        if not window:
            rows.append(f"{label}: nothing")
            continue
        tokens = sum(int(t.get("tokens") or 0) for t in window)
        minutes = sum(int(t.get("seconds") or 0) for t in window) // 60
        failed = sum(t.get("status") not in ("SUCCESS", "STOPPED") for t in window)
        tasks = sum(bool(t.get("task")) for t in window)
        rows.append(f"{label}: {len(window)} turns" + (f" ({tasks} tasks)" if tasks else "")
                    + f" · {tokens // 1000}k tokens · {minutes} min" + (f" · {failed} failed" if failed else ""))
    since = (today - timedelta(days=29)).isoformat()
    models = {}
    for t in turns:
        if (t.get("at") or "") >= since:
            model = t.get("model") or "?"
            models[model] = models.get(model, 0) + int(t.get("tokens") or 0)
    by_model = " · ".join(f"{m} {n // 1000}k" for m, n in sorted(models.items(), key=lambda kv: -kv[1]))
    return "Turns through this plugin:\n" + "\n".join(rows) + (f"\n30 days by model: {by_model}" if by_model else "")


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


async def _cmd_btw(raw_args: str):
    if not raw_args.strip():
        return ("/agy_btw <question>: a side question, answered right away even while agy works, without "
                "touching the main conversation. Reply to its answer to keep that side conversation going.")
    # Normally taken by the dispatch hook; this is the fallback.
    engine.side(engine.Job(text=raw_args.strip(), new_conversation=True, side=True, model=await _btw_model()))
    return None


async def _btw_model():
    """The current model's low-effort variant when there is one: a side question wants speed."""
    current = store.model(store.load())
    models = [name for name, _ in await asyncio.to_thread(_list_models)]
    return _effort_variants(current, models).get("low") or current


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
