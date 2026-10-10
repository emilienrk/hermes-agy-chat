#!/usr/bin/env python3
"""Stand-in for the agy CLI: emits agy's stream-json events, steered by mode.txt next to it."""

import json
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).parent


def emit(event):
    print(json.dumps(event), flush=True)


def main():
    args = sys.argv[1:]
    if args == ["models"]:
        print("Fetching available models...")
        print("gemini-3.8-flash-high\tGemini 3.8 Flash (High)\nclaude-opus-4-6-thinking\tClaude Opus 4.6 (Thinking)")
        return
    if args == ["--version"]:
        print("9.9.9")
        return
    mode = (HERE / "mode.txt").read_text().strip() if (HERE / "mode.txt").exists() else "ok"
    prompt = next(a[3:] for a in args if a.startswith("-p="))
    conv = args[args.index("--conversation") + 1] if "--conversation" in args else None
    with (HERE / "calls.jsonl").open("a") as f:
        f.write(json.dumps({"args": args, "prompt": prompt, "conversation": conv}) + "\n")
    cid = conv or str(uuid.uuid4())
    emit({"event": "init", "conversation_id": cid, "init": {"model": "x", "cwd": str(Path.cwd())}})
    emit({"event": "step_update", "step_update": {"conversation_id": cid, "step_index": 0, "state": "DONE",
                                                  "step_type": "user_input"}})
    if mode == "saturated" and conv:
        emit({"event": "result", "result": {"conversation_id": cid, "status": "ERROR",
                                            "error": "API error (attempt 1): UNAVAILABLE (code 503)"}})
        return
    if mode == "crash":
        print("panic: something broke", flush=True)
        sys.exit(2)
    emit({"event": "step_update", "step_update": {"conversation_id": cid, "step_index": 1, "state": "DONE",
                                                  "step_type": "agent_response", "text_delta": "Je regarde.\n"}})
    emit({"event": "step_update", "step_update": {
        "conversation_id": cid, "step_index": 2, "state": "ACTIVE", "step_type": "tool", "tool_name": "run_command",
        "tool_info": {"name": "run_command", "parameters": {"CommandLine": f"ls {Path.home()}/projects"}}}})
    time.sleep({"slow": 60, "verbose": 9}.get(mode, 0.2))
    emit({"event": "step_update", "step_update": {
        "conversation_id": cid, "step_index": 2, "state": "DONE", "step_type": "tool", "tool_name": "run_command",
        "tool_info": {"name": "run_command", "parameters": {"CommandLine": f"ls {Path.home()}/projects"}}}})
    response = f"ECHO:{prompt[-200:]}"
    if mode == "media":
        response += "\n" + (HERE / "media.txt").read_text()
    if mode == "long":
        response = "x" * 15000
    emit({"event": "result", "result": {"conversation_id": cid, "status": "SUCCESS",
                                        "response": f"Voir [main.py](file://{Path.home()}/main.py).\n{response}",
                                        "usage": {"total_tokens": 54321}}})


if __name__ == "__main__":
    main()
