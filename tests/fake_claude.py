#!/usr/bin/env python3
"""A stand-in for `claude -p --output-format stream-json --verbose` (shape checked against Claude Code 2.1.284) for tests.

Like the real one: options that take several values (--tools, --add-dir, ...) keep taking arguments until the next
option or "--", so a prompt written right after them is lost; stream-json without --verbose is refused; piped stdin is
waited for up to 3 s; --resume of an unknown session fails with num_turns 0; each content block of a message is its
own assistant event with the usage of the message's start, and the result carries the final usage; Read works only
inside the working folder and the --add-dir folders.
FAKE_CLAUDE_LOG: append each call (JSON lines).  FAKE_CLAUDE_STATE: JSON file of saved sessions (needed for --resume).
FAKE_CLAUDE_MODE: echo (default), sleep, sleep-child (a child process keeps stdout open), error (API error, exit 1),
error-exit-0, no-result.
"""
import json
import os
import re
import select
import subprocess
import sys
import time
import uuid

FLAGS = {"-p", "--print", "--verbose", "--safe-mode", "--no-session-persistence", "--bare"}
VALUE = {"--output-format", "--model", "--resume", "-r", "--permission-mode", "--session-id", "--input-format", "--effort"}
MANY = {"--tools", "--add-dir", "--allowedTools", "--allowed-tools", "--disallowedTools", "--disallowed-tools", "--mcp-config"}
MODES = ("acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan", "default")
DEFAULT_TOOLS = ["Bash", "Edit", "Read", "Write", "WebFetch"]


def emit(event):
    print(json.dumps(event), flush=True)


def fail(text):
    sys.stderr.write(text + "\n")
    return 1


def parse(args):
    """commander-style: returns (options, positionals) or raises ValueError with the CLI's message."""
    opts, positional, i = {}, [], 0
    while i < len(args):
        x = args[i]
        if x == "--":
            positional += args[i + 1:]
            break
        if x in FLAGS:
            opts[x] = True
        elif x in VALUE:
            if i + 1 >= len(args):
                raise ValueError(f"error: option '{x}' argument missing")
            opts[x] = args[i + 1]
            i += 1
        elif x in MANY:
            values = []
            while i + 1 < len(args) and not args[i + 1].startswith("-"):
                values.append(args[i + 1])
                i += 1
            opts.setdefault(x, []).extend(values)
        elif x.startswith("-"):
            raise ValueError(f"error: unknown option '{x}'")
        else:
            positional.append(x)
        i += 1
    return opts, positional


def wait_stdin():
    """Seconds spent waiting for piped stdin (the real CLI gives up after 3 s and warns)."""
    if sys.stdin.isatty():
        return 0.0
    start = time.time()
    ready, _, _ = select.select([sys.stdin], [], [], 3.0)
    if not ready:
        sys.stderr.write("Warning: no stdin data received in 3s, proceeding without it.\n")
        return round(time.time() - start, 2)
    sys.stdin.read()
    return round(time.time() - start, 2)


def readable(path, roots):
    real = os.path.realpath(path)
    return os.path.isfile(real) and any(real.startswith(os.path.realpath(root) + os.sep) for root in roots)


def main(argv):
    try:
        opts, positional = parse(argv[1:])
    except ValueError as exc:
        return fail(str(exc))
    if not (opts.get("-p") or opts.get("--print")):
        return fail("fake_claude only knows print mode (-p)")
    if opts.get("--output-format") == "stream-json" and not opts.get("--verbose"):
        return fail("Error: When using --print, --output-format=stream-json requires --verbose")
    mode_opt = opts.get("--permission-mode", "default")
    if mode_opt not in MODES:
        return fail(f"error: option '--permission-mode <mode>' argument '{mode_opt}' is invalid.")
    waited = wait_stdin()
    if len(positional) != 1 or not positional[0].strip():
        return fail("Error: Input must be provided either through stdin or as a prompt argument when using --print")
    prompt = positional[0]
    tools = [t for t in opts.get("--tools", DEFAULT_TOOLS) if t]
    state_path = os.environ.get("FAKE_CLAUDE_STATE")
    saved = {}
    if state_path and os.path.exists(state_path):
        with open(state_path) as handle:
            saved = json.load(handle)
    resume = opts.get("--resume") or opts.get("-r")
    session = resume or str(uuid.uuid4())
    listed = re.findall(r"(/[^\s]+\.png)", prompt)
    roots = [os.getcwd()] + opts.get("--add-dir", [])
    images = [p for p in listed if "Read" in tools and readable(p, roots)]
    if os.environ.get("FAKE_CLAUDE_LOG"):
        with open(os.environ["FAKE_CLAUDE_LOG"], "a") as handle:
            handle.write(json.dumps({"args": argv[1:-1], "prompt": prompt, "session": session, "resumed": bool(resume),
                                     "cwd": os.getcwd(), "listed": listed, "images": images, "stdin_wait": waited,
                                     "tools": tools, "env": sorted(k for k in ("CLAUDECODE", "CLAUDE_CODE_MESSAGING_SOCKET",
                                                                              "CLAUDE_EFFORT", "ANTHROPIC_BASE_URL")
                                                                   if k in os.environ)}) + "\n")
    emit({"type": "system", "subtype": "commands_changed", "commands": [], "session_id": session})
    if resume and resume not in saved:
        sys.stderr.write(f"No conversation found with session ID: {resume}\n")
        emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": None, "session_id": resume,
              "num_turns": 0, "usage": {"input_tokens": 0, "output_tokens": 0}})
        return 1
    emit({"type": "system", "subtype": "init", "cwd": os.getcwd(), "session_id": session, "tools": tools,
          "model": opts.get("--model", "claude-default"), "permissionMode": mode_opt})
    if not opts.get("--no-session-persistence") and state_path:
        saved[session] = {"cwd": os.getcwd(), "turns": saved.get(session, {}).get("turns", 0) + 1}
        with open(state_path, "w") as handle:
            json.dump(saved, handle)
    mode = os.environ.get("FAKE_CLAUDE_MODE", "echo")
    if mode == "sleep-child":  # like a tool's shell that keeps our stdout open
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        time.sleep(30)
    if mode == "sleep":
        time.sleep(30)
    start = {"input_tokens": 10, "cache_creation_input_tokens": 20, "cache_read_input_tokens": 30, "output_tokens": 1}
    if mode in ("error", "error-exit-0"):
        zero = {"input_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0}
        emit({"type": "assistant", "message": {"id": str(uuid.uuid4()), "role": "assistant", "usage": zero,
                                               "content": [{"type": "text", "text": "API Error: 400 boom"}]}, "session_id": session})
        emit({"type": "result", "subtype": "success", "is_error": True, "result": "API Error: 400 boom", "session_id": session,
              "num_turns": 1, "usage": zero})
        return 1 if mode == "error" else 0
    text = f"echo: {prompt[:20]} | images {len(images)} | session {session}"
    message = {"id": "msg_" + uuid.uuid4().hex[:12], "role": "assistant", "usage": start}
    emit({"type": "assistant", "message": dict(message, content=[{"type": "thinking", "thinking": "..."}]), "session_id": session})
    emit({"type": "assistant", "message": dict(message, content=[{"type": "text", "text": text}]), "session_id": session})
    if mode == "no-result":
        return 0
    emit({"type": "result", "subtype": "success", "is_error": False, "result": text, "session_id": session, "num_turns": 1,
          "total_cost_usd": 0.001, "usage": dict(start, output_tokens=7)})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
