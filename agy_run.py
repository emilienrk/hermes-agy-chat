"""Run one agy task unattended and print agy's answer (Hermes' cron delivers stdout as is).

Usage: python agy_run.py '<task json>'  (written by tasks.py into the task's cron script)
"""

import importlib.util
import json
import re
import subprocess
import sys
import time
from pathlib import Path

# The plugin's state module, by path: this runs outside the package.
_spec = importlib.util.spec_from_file_location("agy_chat_state", Path(__file__).resolve().with_name("state.py"))
store = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(store)

_FILE_LINK = re.compile(r"\[([^\]]+)\]\(file://[^)]*\)")


def main():
    task = json.loads(sys.argv[1])
    workspace = Path(task["workspace"]).expanduser()
    rules = workspace / ".agents" / "plugins" / "hermes" / "rules" / "AGENTS.md"
    prompt = (f"(Scheduled task \"{task['name']}\": you run unattended, nobody can answer a question. "
              f"Your final answer is sent to {task['user']} on Telegram as is: make it the deliverable, "
              f"not an account of your work. Context: {rules}.)\n\n{task['prompt']}")
    model = task.get("model") or store.model(store.load())
    cmd = [task["agy_bin"], "--dangerously-skip-permissions", "--model", model,
           "--print-timeout", f"{task['timeout_minutes']}m", f"-p={prompt}", "--output-format", "stream-json"]
    started, error = time.time(), ""
    try:
        proc = subprocess.run(cmd, cwd=str(workspace), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              errors="replace", timeout=(task["timeout_minutes"] + 5) * 60)
        lines = proc.stdout.splitlines()
    except (OSError, subprocess.TimeoutExpired) as e:
        lines, proc = [], None
        error = str(e)
    result, conv = None, None
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("event") == "init":
            conv = event.get("conversation_id")
        elif event.get("event") == "result":
            result = event.get("result") or {}
    ok = bool(result) and result.get("status") == "SUCCESS"
    if not result:
        error = error or "\n".join(lines[-5:]) or (proc.stderr[-500:] if proc else "") or "no output"
    response = (result or {}).get("response") or ""
    store.log_turn({"task": task["name"], "model": model, "conversation_id": conv, "message": task["prompt"][:500],
                    "response": response[:2000], "status": (result or {}).get("status", "NO_RESULT"),
                    "seconds": int(time.time() - started),
                    "tokens": int(((result or {}).get("usage") or {}).get("total_tokens") or 0)})
    head = f"{task['mark']} agy · {task['name']}".strip()
    if not ok:
        print(f"{head}\n\n❌ {(result or {}).get('error') or error}"[:3500])
        return 1
    lines = [line if line.lstrip().startswith("MEDIA:") else _FILE_LINK.sub(r"\1", line)
             for line in response.strip().splitlines()]
    print(f"{head}\n\n" + "\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
