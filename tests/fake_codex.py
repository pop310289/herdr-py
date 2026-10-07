#!/usr/bin/env python3
"""A stand-in for `codex exec --json` (shape checked against codex-cli 0.160.0) for tests and smoke runs.

Like the real one it reads stdin to the end when stdin is not a terminal, so a caller that leaves stdin open hangs.
FAKE_CODEX_LOG: append each call's arguments (JSON lines).  FAKE_CODEX_MODE: echo (default), sleep, error, failed-exit-0 (turn.failed but exit 0), layout.
layout mode answers the layout team like the OpenCode smoke run: a draft lacking a lane's "y", a fix 60 px off,
a first revision with half the row, a second revision equal to the reference row, and DIFF lines from the art director.
"""
import json
import os
import re
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))


def emit(event):
    print(json.dumps(event), flush=True)


def layout_reply(prompt, state):
    ref = json.load(open(os.path.join(os.path.dirname(HERE), "examples", "slide_team", "layout", "reference_portrait.json")))
    found = re.search(r"row (\d)", prompt)
    n = int(found.group(1)) if found else 0
    row = [c for c in ref if c["id"].startswith(f"r{n}.")]
    block = lambda comps: "```json\n" + json.dumps(comps) + "\n```"
    if prompt.startswith("You are the art director"):
        return "DIFF: Long prompt tokens - move them 60 px left\nDIFF: lanes - fine"
    if prompt.startswith("Supervisor: the program cannot draw"):
        off = json.loads(json.dumps(row))
        next(c for c in off if c["id"] == f"r{n}.prompt")["x"] += 60
        return block(off)
    if "Turn the checklist for row" in prompt:
        broken = json.loads(json.dumps(row))
        next(c for c in broken if c["type"] == "lane").pop("y")
        return block(broken)
    if "Improve row" in prompt:
        key = f"revisions-row{n}"
        state[key] = state.get(key, 0) + 1
        return block(row[: len(row) // 2] if state[key] == 1 else row)
    return "?"


def main(argv):
    if not sys.stdin.isatty():
        sys.stderr.write("Reading additional input from stdin...\n")
        sys.stdin.read()
    args = argv[argv.index("exec") + 1:]
    thread = args[1] if args[:1] == ["resume"] else str(uuid.uuid4())
    images = [args[i + 1] for i, x in enumerate(args) if x == "-i"]
    prompt = args[-1]
    if os.environ.get("FAKE_CODEX_LOG"):
        with open(os.environ["FAKE_CODEX_LOG"], "a") as handle:
            handle.write(json.dumps({"args": args[:-1], "thread": thread, "images": images, "prompt": prompt}) + "\n")
    mode = os.environ.get("FAKE_CODEX_MODE", "echo")
    emit({"type": "thread.started", "thread_id": thread})
    emit({"type": "turn.started"})
    if mode in ("error", "failed-exit-0"):
        emit({"type": "error", "message": "boom"} if mode == "error" else {"type": "turn.failed", "error": {"message": "boom"}})
        return 1 if mode == "error" else 0
    if mode == "sleep":
        time.sleep(30)
    if mode == "layout":
        path = os.environ["FAKE_CODEX_STATE"]
        state = json.load(open(path)) if os.path.exists(path) else {}
        text = layout_reply(prompt, state)
        json.dump(state, open(path, "w"))
    else:
        text = f"echo: {prompt[:20]} | images {len(images)} | thread {thread}"
    emit({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": text}})
    emit({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 50, "output_tokens": 10}})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
