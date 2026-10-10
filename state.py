"""Persistent plugin state (state.json) and the turn log (turns.log, one JSON object per line)."""

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def _data_dir():
    # Outside the install tree, which `hermes plugins update` replaces.
    try:
        from plugins.plugin_storage import plugin_data_dir
        return plugin_data_dir("agy-chat")
    except Exception:
        path = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes") / "plugin-data" / "agy-chat"
        path.mkdir(parents=True, exist_ok=True)
        return path


DATA_DIR = _data_dir()
STATE_FILE = DATA_DIR / "state.json"
TURNS_LOG = DATA_DIR / "turns.log"
DEFAULT_MODEL = "gemini-3.8-flash-high"
DEFAULT_TRIGGERS = ("jet", "agy")
DEFAULT_VOICE = "direct"
AGY_MSGS_KEEP = 400
LOG_TAIL_BYTES = 512 * 1024

_lock = threading.Lock()


def load():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def update(**changes):
    """Merge ``changes`` into the state (None deletes a key); returns the new state."""
    with _lock:
        state = load()
        for key, value in changes.items():
            if value is None:
                state.pop(key, None)
            else:
                state[key] = value
        _save(state)
        return state


def _save(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    tmp.replace(STATE_FILE)


def model(state):
    return state.get("model") or DEFAULT_MODEL


def triggers(state):
    return list(state.get("triggers") or DEFAULT_TRIGGERS)


def voice_mode(state):
    return state.get("voice") or DEFAULT_VOICE


def remember_agy_messages(ids):
    """Record Telegram message ids sent as agy, so a reply to one of them reaches agy."""
    ids = [str(i) for i in ids if i]
    if not ids:
        return
    with _lock:
        state = load()
        state["agy_msgs"] = (state.get("agy_msgs", []) + ids)[-AGY_MSGS_KEEP:]
        _save(state)


def is_agy_message(state, message_id):
    return message_id is not None and str(message_id) in state.get("agy_msgs", ())


def log_turn(entry):
    entry = {"at": datetime.now().isoformat(timespec="seconds"), **entry}
    try:
        with TURNS_LOG.open("a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        logger.warning("agy-chat: cannot write %s", TURNS_LOG)


def recent_turns():
    """Logged turns, oldest first (only the log's tail is read)."""
    try:
        with TURNS_LOG.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - LOG_TAIL_BYTES))
            raw = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = raw.splitlines()
    if size > LOG_TAIL_BYTES:
        lines = lines[1:]  # first line is cut
    turns = []
    for line in lines:
        try:
            turns.append(json.loads(line))
        except ValueError:
            continue
    return turns


def conversations(limit=8):
    """Recent agy conversations, newest first: id, first message, last activity, turn count."""
    convs = {}
    for order, turn in enumerate(recent_turns()):
        cid = turn.get("conversation_id")
        if not cid:
            continue
        conv = convs.setdefault(cid, {"id": cid, "first": turn.get("message") or "", "turns": 0})
        conv.update(last=turn.get("at") or "", order=order)
        conv["turns"] += 1
    return sorted(convs.values(), key=lambda c: c["order"], reverse=True)[:limit]


def recap(conversation_id, max_turns, user="User", max_chars=600):
    """The last exchanges of a conversation, to seed a fresh one."""
    turns = [t for t in recent_turns() if t.get("conversation_id") == conversation_id and t.get("response")]
    lines = []
    for turn in turns[-max_turns:]:
        lines.append(f"- {user}: {_cut(turn.get('message') or '', max_chars)}")
        lines.append(f"  agy: {_cut(turn.get('response') or '', max_chars)}")
    return "\n".join(lines)


def _cut(text, limit):
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"
