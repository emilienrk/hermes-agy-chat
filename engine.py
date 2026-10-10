"""agy turns: the queue, the agy process, its stream-json events and the Telegram output.

Everything runs on the gateway's event loop; blocking work (transcription, file copies,
workspace refresh) goes to threads, since a blocked loop trips the gateway's liveness watchdog.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import state as store
from . import workspace
from .config import cfg

logger = logging.getLogger(__name__)

# Set by configure() from the plugin settings.
AGY_BIN = "agy"
TARGET_CHAT = ""
TARGET = ""
PRINT_TIMEOUT = "30m"  # PRINT_TIMEOUT, HARD_TIMEOUT_S, MARK, REACT_*: set by configure()
HARD_TIMEOUT_S = 35 * 60
TYPING_EVERY_S = 4.5
STATUS_EVERY_S = 4.0
STATUS_REFRESH_S = 30.0
STREAM_LIMIT = 64 * 1024 * 1024  # stream-json lines carry whole responses
MAX_ARG_PROMPT = 100_000  # Linux caps one argv string at 128 KiB
MAX_REPLY_CHARS = 12000
SUMMARY_MAX_LINES = 12
STATUS_ACTIONS = 5
INBOX_KEEP_DAYS = 30
RECAP_TURNS = 6
REACT_WORKING = "👀"
REACT_QUEUED = "✍"
MARK = "🛸"
MAX_SPOKEN_CHARS = 4000


def mode_banner():
    return (f"{MARK} agy mode on · /agy_off to go back to Hermes\n"
            "Your messages, photos and voice notes go to agy. For Hermes: \"Hermes …\" or a / command. "
            "Here /stop and /new act on agy.")

_PARAM_KEYS = ("CommandLine", "AbsolutePath", "TargetFile", "Url", "Query", "SearchPath",
               "DirectoryPath", "Pattern", "Path")
_FILE_LINK = re.compile(r"\[([^\]]+)\]\(file://[^)]*\)")
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp"}
_AUDIO_EXTS = {".ogg", ".oga", ".opus", ".mp3", ".m4a", ".wav", ".flac", ".aac"}
# A long conversation ends in a permanent 503 on every model, while a fresh one answers
# (measured 2026-10-01).
_SATURATED = re.compile(r"UNAVAILABLE|code 503")
# Two agy processes sharing the OAuth token (e.g. another account on the machine): a
# simultaneous refresh fails one of them once.
_TRANSIENT = re.compile(r"not logged into Antigravity|INTERNAL \(code 500\)", re.I)
_QUOTA = re.compile(r"RESOURCE_EXHAUSTED|quota|rate.?limit|code 429", re.I)


@dataclass
class Job:
    """One message from the user for agy."""
    text: str = ""
    files: list = field(default_factory=list)  # [(path, mime)] in the gateway cache
    voice: list = field(default_factory=list)  # voice notes to transcribe
    message_id: str | None = None
    quoted: str = ""  # the message it replies to, when that one is not agy's
    new_conversation: bool = False


class Run:
    """The live turn."""

    def __init__(self, jobs, model):
        self.jobs, self.model = jobs, model
        self.started = time.time()
        self.proc = None
        self.reset()
        self.stopped = False
        self.status_msg = None

    def reset(self):
        self.actions = []
        self.current = None
        self.text = {}  # step_index -> text streamed so far
        self.last_step = None
        self.result = None
        self.noise = []
        self.timed_out = False
        self.changed = True
        self.tokens = 0

    def error_text(self):
        if self.result is not None:
            return str(self.result.get("error") or self.result.get("response") or "")
        return "\n".join(self.noise)

    def failed(self):
        return not self.stopped and (self.result is None or self.result.get("status") != "SUCCESS")


_rt = {"gateway": None, "adapter": None, "loop": None, "notice_sent": False}
_queue = []
_worker = None
_run = None


def configure():
    """Apply config.cfg (loaded by the plugin's register())."""
    global AGY_BIN, TARGET_CHAT, TARGET, PRINT_TIMEOUT, HARD_TIMEOUT_S, MARK, REACT_WORKING, REACT_QUEUED
    TARGET_CHAT = cfg["chat_id"]
    TARGET = f"telegram:{TARGET_CHAT}"
    AGY_BIN = os.path.expanduser(cfg["agy_path"]) if cfg["agy_path"] else (
        shutil.which("agy") or str(Path.home() / ".local/bin/agy"))
    PRINT_TIMEOUT = f"{cfg['timeout_minutes']}m"
    HARD_TIMEOUT_S = (cfg["timeout_minutes"] + 5) * 60
    MARK = str(cfg["mark"] or "")
    REACT_WORKING, REACT_QUEUED = str(cfg["react_working"] or ""), str(cfg["react_queued"] or "")


# ── binding to the gateway ──

def bind_adapter(adapter):
    _rt["adapter"] = adapter
    try:
        _rt["loop"] = asyncio.get_running_loop()
    except RuntimeError:
        pass


def bind_gateway(gateway):
    _rt["gateway"] = gateway
    _rt["loop"] = asyncio.get_running_loop()


def adapter():
    if _rt["adapter"] is not None:
        return _rt["adapter"]
    gateway = _rt["gateway"]
    if gateway is None:
        return None
    try:
        from gateway.config import Platform
        return (getattr(gateway, "adapters", None) or {}).get(Platform.TELEGRAM)
    except Exception:
        return None


def schedule_startup(delay=20):
    """Once per gateway start, when the bot is up: the restart notice and the mode banner."""
    loop = _rt["loop"]
    if loop is None or _rt["notice_sent"]:
        return
    _rt["notice_sent"] = True
    loop.call_later(delay, lambda: loop.create_task(_startup()))


async def _startup():
    await _restart_notice()
    await set_mode_banner(bool(store.load().get("sticky")))


async def _restart_notice():
    inflight = store.load().get("inflight")
    if not inflight or _run is not None:
        return
    store.update(inflight=None)
    started = inflight.get("at", "")[11:16]
    await send(f"⚠️ Hermes restarted while agy was answering \"{_cut(inflight.get('message', ''), 120)}\" "
               f"(started at {started}): the answer is lost. /agy_retry to run it again.")


# ── queue ──

def busy():
    return _run is not None


def snapshot():
    run = _run
    return {"busy": run is not None, "queued": len(_queue),
            "elapsed": _elapsed(run.started) if run else "", "actions": len(run.actions) if run else 0,
            "current": run.current if run else None}


async def submit(job):
    """Queue a job and start the worker when idle; returns True when it waits behind a turn."""
    global _worker
    _queue.append(job)
    if _worker is None or _worker.done():
        _worker = asyncio.get_running_loop().create_task(_drain(), name="agy-chat")
        return False
    if job.message_id and REACT_QUEUED:
        await react(job.message_id, REACT_QUEUED)
    return True


def stop():
    """Kill the running turn and drop the queue; returns (was_running, dropped)."""
    dropped = len(_queue)
    for job in _queue:
        if job.message_id:
            asyncio.get_running_loop().create_task(react(job.message_id, None))
    _queue.clear()
    run = _run
    if run is None:
        return False, dropped
    run.stopped = True  # also covers a turn still transcribing, before agy starts
    if run.proc is not None and run.proc.returncode is None:
        _kill(run.proc)
    return True, dropped


async def _drain():
    global _run
    while _queue:
        jobs = _queue[:]
        _queue.clear()
        try:
            await _turn(jobs)
        except Exception as e:  # the worker must survive and always answer
            logger.exception("agy-chat: turn failed")
            await send(f"❌ agy-chat: {e}\n/agy_retry to run it again.")
        finally:
            _run = None
            for job in jobs:
                if job.message_id:
                    await react(job.message_id, None)


# ── turn ──

async def _turn(jobs):
    global _run
    if any(job.new_conversation for job in jobs):
        store.update(conversation_id=None)
    state = store.load()
    conv = state.get("conversation_id")
    verbose = store.verbose(state)
    anchor = next((job.message_id for job in reversed(jobs) if job.message_id), None)
    run = _run = Run(jobs, store.model(state))
    for job in jobs:
        if job.message_id and REACT_WORKING:
            await react(job.message_id, REACT_WORKING)

    message, files = await _compose(jobs, verbose)
    await asyncio.to_thread(workspace.prepare)
    store.update(inflight={"message": message[:500], "at": datetime.now().isoformat(timespec="seconds")},
                 last_turn={"text": message, "files": files})
    prompt = message if conv else f"{workspace.first_turn_preamble()}\n\n{message}"

    loop = asyncio.get_running_loop()
    side_tasks = [loop.create_task(_typing_loop())]
    if verbose:
        side_tasks.append(loop.create_task(_status_loop(run, anchor)))
    notice = ""
    try:
        await _exec(run, prompt, conv)
        if run.failed() and conv and _SATURATED.search(run.error_text()):
            notice = "♻️ Conversation saturated (503): new conversation, seeded with the recent thread."
            store.update(conversation_id=None)
            recap = store.recap(conv, RECAP_TURNS, workspace.USER)
            seeded = f"Last exchanges of the previous conversation:\n{recap}\n\n{message}" if recap else message
            run.reset()
            conv = None
            await _exec(run, f"{workspace.first_turn_preamble()}\n\n{seeded}", None)
        elif run.failed() and _TRANSIENT.search(run.error_text()):
            await asyncio.sleep(5)
            run.reset()
            await _exec(run, prompt, store.load().get("conversation_id"))
    finally:
        for task in side_tasks:
            task.cancel()
        store.update(inflight=None)

    body, attachments = _reply(run, verbose, notice)
    if verbose and run.status_msg:
        await _edit(run.status_msg, _summary(run))
    await deliver(body, attachments, anchor)
    if _speaks(state, jobs) and not run.failed() and run.result:
        await _speak(_spoken_text(run.result.get("response") or ""), anchor)
    await asyncio.to_thread(store.log_turn, {
        "model": run.model, "conversation_id": store.load().get("conversation_id"),
        "message": message[:500], "response": ((run.result or {}).get("response") or "")[:2000],
        "status": "STOPPED" if run.stopped else (run.result or {}).get("status", "NO_RESULT"),
        "seconds": int(time.time() - run.started), "files": [p for p, _ in files],
        "actions": run.actions, "tokens": run.tokens})


async def _compose(jobs, verbose):
    """The message agy receives, and the attachments copied to its inbox."""
    parts, files = [], []
    vmode = store.voice_mode(store.load())
    user = workspace.USER
    for job in jobs:
        text = job.text.strip()
        for path in job.voice:
            if vmode == "direct":
                files += await asyncio.to_thread(_stash, [(path, "audio/ogg")])
                voice_note = (f"Voice message from {user} (open the attached audio file with view_file to hear "
                              "the request and answer it).")
                text = f"{text}\n{voice_note}".strip()
                if verbose:
                    await send("🎙 Voice note passed straight to agy (Google)", reply_to=job.message_id)
            else:
                transcript = await asyncio.to_thread(_transcribe, path)
                if transcript:
                    text = f"{text}\n{transcript}".strip()
                    if verbose:
                        await send(f"🎙 \"{transcript}\"", reply_to=job.message_id)
                else:
                    files += await asyncio.to_thread(_stash, [(path, "audio/ogg")])
                    text = f"{text}\n(voice message not transcribed: listen to the attached file)".strip()
        files += await asyncio.to_thread(_stash, job.files)
        if job.quoted:
            text = f"[Replying to: \"{_cut(job.quoted, 1500)}\"]\n{text}"
        parts.append(text or "Look at the attached file(s).")
    if len(parts) == 1:
        message = parts[0]
    else:
        message = f"Several messages from {user}, in order:\n\n" + "\n\n".join(
            f"{i}. {part}" for i, part in enumerate(parts, 1))
    if files:
        listing = "\n".join(f"- {path}" + (f" ({kind})" if kind else "") for path, kind in files)
        message += f"\n\nFiles attached by {user} (open them with your tools):\n{listing}"
    return message, files


def _transcribe(path):
    try:
        from tools.transcription_tools import transcribe_audio
        result = transcribe_audio(path, None, "gateway")
    except Exception:
        logger.exception("agy-chat: transcription failed")
        return ""
    return (result.get("transcript") or "").strip() if result.get("success") else ""


def _stash(files):
    """Copy attachments out of the gateway cache (pruned on its own schedule) into agy's inbox."""
    inbox = workspace.INBOX
    inbox.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - INBOX_KEEP_DAYS * 86400
    for old in inbox.iterdir():
        if old.is_file() and old.stat().st_mtime < cutoff:
            old.unlink(missing_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = []
    for i, (src, kind) in enumerate(files):
        dest = inbox / f"{stamp}-{i}-{Path(src).name}"
        try:
            shutil.copy2(src, dest)
        except OSError:
            logger.warning("agy-chat: cannot copy attachment %s", src)
            continue
        out.append((str(dest), kind or ""))
    return out


async def _exec(run, prompt, conv):
    if run.stopped:
        return
    if len(prompt) > MAX_ARG_PROMPT:
        path = workspace.INBOX / f"{datetime.now():%Y%m%d-%H%M%S}-message.md"
        await asyncio.to_thread(path.write_text, prompt)
        prompt = f"{workspace.USER}'s message is too long for the command line: read {path} and answer it."
    cmd = [AGY_BIN, "--dangerously-skip-permissions", "--model", run.model,
           "--print-timeout", PRINT_TIMEOUT, f"-p={prompt}", "--output-format", "stream-json"]
    if conv:
        cmd[1:1] = ["--conversation", conv]
    # The gateway process holds every API key in os.environ; don't hand them to agy.
    from tools.environments.local import build_subprocess_env
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(workspace.WORKSPACE), env=build_subprocess_env(), stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, limit=STREAM_LIMIT,
        start_new_session=True)  # own process group: stop/timeout also reach agy's children
    run.proc = proc
    try:
        await asyncio.wait_for(_read_events(run, proc), max(5.0, HARD_TIMEOUT_S - (time.time() - run.started)))
    except (asyncio.TimeoutError, ValueError):
        run.timed_out = True
        _kill(proc)
    try:
        await asyncio.wait_for(proc.wait(), 10)
    except asyncio.TimeoutError:
        _kill(proc, signal.SIGKILL)
        await proc.wait()


async def _read_events(run, proc):
    async for raw in proc.stdout:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            run.noise = (run.noise + [line])[-40:]
            continue
        kind = event.get("event")
        if kind == "init" and event.get("conversation_id"):
            store.update(conversation_id=event["conversation_id"])
        elif kind == "step_update":
            track_step(run, event.get("step_update") or {})
        elif kind == "result":
            run.result = event.get("result") or {}
            run.tokens = int(((run.result.get("usage") or {}).get("total_tokens")) or 0)


def track_step(run, step):
    kind, status = step.get("step_type"), step.get("state")
    if kind == "tool":
        desc = _describe_tool(step)
        if status == "ACTIVE":
            run.current = desc
        elif status in ("DONE", "ERROR"):
            run.actions.append(f"{'✓' if status == 'DONE' else '✗'} {desc}")
            run.current = None
        run.changed = True
    elif kind == "agent_response" and step.get("text_delta"):
        index = step.get("step_index")
        run.text[index] = run.text.get(index, "") + step["text_delta"]
        run.last_step = index
        run.changed = True


def _describe_tool(step):
    info = step.get("tool_info") or {}
    params = info.get("parameters") or {}
    arg = next((params[k] for k in _PARAM_KEYS if isinstance(params.get(k), str)), None)
    if arg is None:
        arg = next((v for v in params.values() if isinstance(v, str)), "")
    arg = _cut(_home(arg), 80)
    return f"{info.get('name') or step.get('tool_name') or '?'} {arg}".rstrip()


def _kill(proc, sig=signal.SIGTERM):
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


# ── Telegram output ──

async def _typing_loop():
    while True:
        ad = adapter()
        if ad is not None:
            try:
                await ad.send_typing(TARGET_CHAT)
            except Exception:
                logger.debug("agy-chat: typing failed", exc_info=True)
        await asyncio.sleep(TYPING_EVERY_S)


async def _status_loop(run, anchor):
    shown_at = 0.0
    while True:
        await asyncio.sleep(STATUS_EVERY_S)
        if not run.changed and time.time() - shown_at < STATUS_REFRESH_S:
            continue
        run.changed, shown_at = False, time.time()
        text = _status_text(run)
        if run.status_msg is None:
            ids = await send(text, reply_to=anchor)
            run.status_msg = ids[-1] if ids else None
        else:
            await _edit(run.status_msg, text, final=False)


def _status_text(run):
    lines = [f"{MARK} agy working · {_elapsed(run.started)} · {len(run.actions)} actions".strip()]
    if run.current:
        lines.append(f"▶ {run.current}")
    lines += run.actions[-STATUS_ACTIONS:][::-1]
    said = run.text.get(run.last_step, "").strip().splitlines()
    if said:
        lines.append(f"💬 {_cut(said[-1], 160)}")
    return "\n".join(lines)


def _summary(run):
    errors = sum(a.startswith("✗") for a in run.actions)
    head = f"🔧 {len(run.actions)} actions" + (f" ({errors} failed)" if errors else "")
    head += f" · {_elapsed(run.started)}" + (f" · {run.tokens // 1000}k tokens" if run.tokens else "")
    shown = run.actions[-SUMMARY_MAX_LINES:]
    more = [f"… +{len(run.actions) - len(shown)} earlier"] if len(run.actions) > len(shown) else []
    return "\n".join([head, *more, *shown])


def _reply(run, verbose, notice=""):
    """(text, attachments) for the final answer."""
    from gateway.platforms.base import BasePlatformAdapter
    result = run.result or {}
    attachments = []
    if run.stopped:
        text = "🛑 Stopped."
    elif run.result is None:
        text = (f"⌛ No answer from agy in {HARD_TIMEOUT_S // 60} min." if run.timed_out else
                f"❌ agy exited without an answer (code {run.proc.returncode if run.proc else '?'}):\n"
                f"{_cut(run.error_text(), 800) or 'no output'}") + _hint(run)
    elif result.get("status") != "SUCCESS":
        text = f"❌ agy {result.get('status')}: {_cut(run.error_text(), 800) or '?'}" + _hint(run)
    else:
        attachments, text = BasePlatformAdapter.extract_media(clean(result.get("response") or ""))
        attachments = [path for path, _voice in attachments]
        text = text.strip() or ("" if attachments else "(empty answer)")
        if len(text) > MAX_REPLY_CHARS:
            full = workspace.OUTBOX / f"answer-{datetime.now():%Y%m%d-%H%M%S}.md"
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(text)
            text = text[:MAX_REPLY_CHARS] + "\n\n[…continued in the attached file]"
            attachments.append(str(full))
    if not verbose and run.actions and not run.stopped and (run.result or {}).get("status") != "SUCCESS":
        text += "\n\nLast actions:\n" + "\n".join(run.actions[-5:])
    footer = ""
    if cfg["footer"]:
        parts = [f"{MARK} agy".strip(), *([run.model] if verbose else []), _elapsed(run.started)]
        footer = "— " + " · ".join(parts)
    if verbose and not run.status_msg and run.actions:  # turn too short for the live status message
        text += "\n\n" + _summary(run)
    if notice:
        text = f"{notice}\n\n{text}"
    return f"{text}\n\n{footer}".strip(), attachments


def _hint(run):
    err = run.error_text()
    if _QUOTA.search(err):
        return "\n\nQuota reached on this model? /agy_model to switch."
    if _SATURATED.search(err):
        return "\n\nConversation too heavy? /agy_new then /agy_retry."
    if _TRANSIENT.search(err):
        return "\n\nAntigravity session to reconnect? Run `agy` on the machine (e.g. over SSH)."
    return "\n\n/agy_retry to run it again."


def clean(text):
    # file:// links are dead ends on a phone: keep the label. MEDIA: lines keep their absolute path.
    lines = [line if line.lstrip().startswith("MEDIA:") else _home(_FILE_LINK.sub(r"\1", line))
             for line in text.splitlines()]
    return "\n".join(lines).strip()


async def deliver(text, attachments=(), reply_to=None):
    """Send agy's answer and its files; every message sent is remembered as agy's."""
    from gateway.platforms.base import BasePlatformAdapter
    ids = await send(text, reply_to=reply_to) if text.strip() else []
    ad = adapter()
    dropped = []  # same policy as Hermes' own sends: no secrets (.env, ssh keys…), no missing files
    safe = await asyncio.to_thread(BasePlatformAdapter.filter_media_delivery_paths,
                                   [(path, False) for path in attachments], "", dropped)
    for drop in dropped:
        ids += await send(f"⚠️ File not sent ({drop['reason']}): {_home(drop['path'])}", reply_to=reply_to)
    for path, _voice in safe:
        p = Path(path)
        if ad is None:
            ids += await send(f"MEDIA:{p}")
            continue
        ext = p.suffix.lower()
        try:
            if ext in _IMAGE_EXTS:
                res = await ad.send_image_file(TARGET_CHAT, str(p), reply_to=reply_to)
            elif ext in _VIDEO_EXTS:
                res = await ad.send_video(TARGET_CHAT, str(p), reply_to=reply_to)
            elif ext in _AUDIO_EXTS:
                res = await ad.send_voice(TARGET_CHAT, str(p), reply_to=reply_to)
            else:
                res = await ad.send_document(TARGET_CHAT, str(p), reply_to=reply_to)
        except Exception as e:
            logger.exception("agy-chat: cannot send %s", p)
            res = None
            error = str(e)
        else:
            error = getattr(res, "error", "")
        if res is not None and res.success:
            ids.append(res.message_id)
        else:
            ids += await send(f"⚠️ Cannot send {p.name}: {error or '?'}", reply_to=reply_to)
    store.remember_agy_messages(ids)
    return ids


async def send(text, reply_to=None):
    """Send a text message as agy; returns the Telegram message ids."""
    ad = adapter()
    if ad is not None:
        try:
            res = await ad.send(TARGET_CHAT, text, reply_to=reply_to)
            if res.success:
                ids = [*getattr(res, "continuation_message_ids", ()), res.message_id]
                store.remember_agy_messages(ids)
                return [i for i in ids if i]
            logger.warning("agy-chat: adapter send failed: %s", res.error)
        except Exception:
            logger.exception("agy-chat: adapter send raised")
    # No live adapter (or it failed): Hermes' standalone sender. It also mirrors the text into
    # Hermes' session, which the adapter path avoids.
    out = await asyncio.to_thread(_send_standalone, text)
    return [out] if out else []


def _send_standalone(text):
    try:
        from tools.send_message_tool import send_message_tool
        out = json.loads(send_message_tool({"action": "send", "target": TARGET, "message": text}))
        if out.get("success"):
            return out.get("message_id")
        logger.warning("agy-chat: send_message failed: %s", str(out)[:500])
    except Exception:
        logger.exception("agy-chat: send_message raised")
    return None


async def set_mode_banner(sticky):
    """The agy mode's indicator: a banner pinned at the top of the chat while the mode is on.
    False when the bot can't be reached (the caller then answers in plain text)."""
    bot = getattr(adapter(), "_bot", None)  # the adapter has no pin API
    if bot is None:
        return False
    chat, pinned = int(TARGET_CHAT), store.load().get("mode_pin")
    try:
        if sticky and not pinned:
            from telegram import ReplyKeyboardRemove
            # ReplyKeyboardRemove clears the input keyboard an earlier version used as the indicator.
            msg = await bot.send_message(chat_id=chat, text=mode_banner(), reply_markup=ReplyKeyboardRemove())
            await bot.pin_chat_message(chat_id=chat, message_id=msg.message_id, disable_notification=True)
            store.update(mode_pin=msg.message_id)
        elif not sticky and pinned:
            store.update(mode_pin=None)
            for call in (bot.unpin_chat_message, bot.delete_message):
                try:  # the user may have unpinned or deleted it already
                    await call(chat_id=chat, message_id=pinned)
                except Exception:
                    logger.debug("agy-chat: %s failed", call.__name__, exc_info=True)
    except Exception:
        logger.exception("agy-chat: mode banner failed")
        return False
    return True


async def _edit(message_id, text, final=True):
    ad = adapter()
    if ad is None or message_id is None:
        return
    try:
        await ad.edit_message(TARGET_CHAT, message_id, text, finalize=final)
    except Exception:
        logger.debug("agy-chat: edit failed", exc_info=True)


async def react(message_id, emoji):
    """Set (None: clear) the bot's reaction on one of the user's messages."""
    ad = adapter()
    # The Telegram adapter has no public reaction API; _set_reaction is what its own
    # processing reactions use, and it swallows its errors.
    setter = getattr(ad, "_set_reaction", None)
    if setter is None or message_id is None:
        return
    try:
        await setter(TARGET_CHAT, message_id, emoji)
    except Exception:
        logger.debug("agy-chat: reaction failed", exc_info=True)


# ── voice replies ──

def _speaks(state, jobs):
    mode = store.voice_reply(state)
    return mode == "always" or (mode == "mirror" and any(job.voice for job in jobs))


def _spoken_text(response):
    """What is worth reading aloud: no MEDIA lines, links reduced to their label."""
    lines = [line for line in response.splitlines() if not line.lstrip().startswith("MEDIA:")]
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", "\n".join(lines)).strip()
    return text[:MAX_SPOKEN_CHARS]


async def _speak(text, reply_to=None):
    """Read agy's answer aloud and send it as a voice note."""
    if not text:
        return
    try:
        path = await (_edge_voice(text) if cfg["tts_provider"] == "edge" else asyncio.to_thread(_hermes_voice, text))
    except Exception:
        logger.exception("agy-chat: TTS failed")
        return
    ad = adapter()
    if not path or ad is None:
        return
    try:
        res = await ad.send_voice(TARGET_CHAT, path, reply_to=reply_to)
    except Exception:
        logger.exception("agy-chat: cannot send the voice reply")
        return
    if res is not None and res.success:
        store.remember_agy_messages([res.message_id])


def _hermes_voice(text):
    """Hermes' TTS, with its configured provider (it ignores per-call provider overrides)."""
    from tools.tts_tool import text_to_speech_tool
    out = json.loads(text_to_speech_tool(text=text))
    if not out.get("success"):
        logger.warning("agy-chat: Hermes TTS failed: %s", str(out.get("error") or out)[:300])
    return out.get("file_path") if out.get("success") else None


async def _edge_voice(text):
    """Edge TTS (free) straight from the edge_tts package Hermes ships, as an OGG/Opus voice note."""
    import edge_tts
    try:
        from tools.tts_text_normalize import prepare_spoken_text
        text = prepare_spoken_text(text, max_chars=None)
    except Exception:
        pass
    workspace.OUTBOX.mkdir(parents=True, exist_ok=True)
    base = workspace.OUTBOX / f"voice-{datetime.now():%Y%m%d-%H%M%S}"
    mp3, ogg = base.with_suffix(".mp3"), base.with_suffix(".ogg")
    await edge_tts.Communicate(text, _edge_voice_name()).save(str(mp3))
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(mp3), "-c:a", "libopus", "-b:a", "32k", str(ogg),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _out, err = await proc.communicate()
    if proc.returncode != 0:  # no ffmpeg/opus: Telegram still plays the MP3, as an audio file
        logger.warning("agy-chat: ffmpeg failed: %s", err.decode(errors="replace")[:300])
        return str(mp3)
    mp3.unlink(missing_ok=True)
    return str(ogg)


def _edge_voice_name():
    if cfg["tts_voice"]:
        return cfg["tts_voice"]
    try:
        import yaml
        tts = (yaml.safe_load((workspace.HERMES_HOME / "config.yaml").read_text()) or {}).get("tts") or {}
        return (tts.get("edge") or {}).get("voice") or "en-US-AriaNeural"
    except Exception:
        return "en-US-AriaNeural"


# ── helpers ──

def _home(text):
    return text.replace(str(Path.home()), "~")


def _cut(text, limit):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _elapsed(started):
    secs = int(time.time() - started)
    return f"{secs // 60} min {secs % 60:02d}" if secs >= 60 else f"{secs}s"
