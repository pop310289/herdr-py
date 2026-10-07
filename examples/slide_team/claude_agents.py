"""Claude Code CLI sessions as team members, with the same contract as CodexAgents.run_turn: run_turn(name, prompt,
model, files, timeout) -> (reply text, state).

One `claude -p --output-format stream-json --verbose` per turn; a member's later turns add `--resume <session id>`
(unless fresh=True or forget(name) was called), so every member keeps its own conversation. Checked with Claude Code
2.1.284 on 2026-10-08: `claude --help`, code.claude.com/docs, and the real CLI against a local stand-in for the
Messages API (no model was called):
- events: system/init {session_id, tools}; assistant {message: {id, content, usage}}, one event per content block, so
  a message can come twice; result {subtype, is_error, result, usage, session_id, num_turns} last;
- the tokens come from result.usage (this run's total, final output included): an assistant event repeats the usage of
  its message's start (output_tokens 1);
- an API error ends with is_error true (subtype still "success"), exit code 1 and the error text as an assistant
  message, so the reply is "" unless the turn ended without an error: an error text is never read as an answer;
- stream-json needs --verbose; --tools and --add-dir take several values, so the prompt goes after "--";
- -p waits 3 s for piped stdin before it starts: stdin is closed, so no turn waits;
- images: -p has no attachment flag. The prompt lists the files and the member opens them with the Read tool
  (--tools Read); --add-dir makes their folders readable (without it Read is denied);
- --safe-mode keeps CLAUDE.md, hooks, skills, plugins and MCP servers out (a CLAUDE.md in the member's folder no
  longer reached the model) and --permission-mode dontAsk denies whatever would need a person, so a run depends on the
  model and the prompt, not on how this machine's Claude Code is set up. Login and models work as usual;
- a session that cannot be resumed ends with "No conversation found" and num_turns 0: it is forgotten, so the member
  starts a new conversation next time instead of failing every turn.
Every member runs in its own empty folder with no tools (Read only on a turn with images). agents.json (for view.py)
holds each member's state, tokens and last words; events.jsonl keeps every event.
"""
import json
import os
import signal
import subprocess
import threading
import time

ISOLATION = ["--safe-mode", "--permission-mode", "dontAsk"]
# set by a Claude Code session for the commands it runs (seen in 2.1.283): a member started from inside such a session
# (an agent running the experiment) must not look like a part of that session
PARENT_SESSION = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
                  "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
                  "CLAUDE_CODE_BRIDGE_SESSION_ID", "CLAUDE_CODE_EXECPATH", "CLAUDE_PID", "CLAUDE_EFFORT")


def default_claude():
    return os.environ.get("CLAUDE_BIN") or "claude"


def tokens_of(usage):
    """Input (cache reads and writes included) plus output: what the model processed, as the Codex members count it."""
    usage = usage if isinstance(usage, dict) else {}
    return sum(int(usage.get(key) or 0) for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
                                                     "output_tokens"))


def stop(proc):
    """End the member and anything it started (it runs in its own process group)."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:  # already gone
        pass


class ClaudeAgents:
    def __init__(self, root, claude=None, model=None, fresh=False, isolation=None):
        """fresh=True: every turn is a new conversation (no resume); the caller's prompt carries everything.
        isolation: the flags that keep this machine's Claude Code setup out of the run (default ISOLATION)."""
        self.root, self.claude, self.model, self.fresh = root, claude or default_claude(), model, fresh
        self.isolation = list(ISOLATION if isolation is None else isolation)
        os.makedirs(root, exist_ok=True)
        self.sessions, self.agents = {}, {}
        self.lock = threading.Lock()
        self.events = open(os.path.join(root, "events.jsonl"), "a", encoding="utf-8", buffering=1)

    def close(self):
        self.events.close()

    def forget(self, name):
        """This member's next turn starts a new conversation."""
        self.sessions.pop(name, None)

    def _publish(self):
        tmp = os.path.join(self.root, "agents.json.tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self.agents, handle)
        os.replace(tmp, os.path.join(self.root, "agents.json"))  # the viewer never reads half a file

    def _set(self, name, **fields):
        with self.lock:
            agent = self.agents.setdefault(name, {"name": name, "state": "starting", "tokens": 0, "turns": 0,
                                                  "stream": {"kind": "text", "text": ""}})
            agent.update(fields)
            self._publish()

    def args(self, name, prompt, model=None, files=()):
        session = None if self.fresh else self.sessions.get(name)
        out = [self.claude, "-p", "--output-format", "stream-json", "--verbose"] + self.isolation
        if session:
            out += ["--resume", session]
        if model or self.model:
            out += ["--model", model or self.model]
        paths = [os.path.abspath(path) for path in files]
        if paths:
            out += ["--tools", "Read", "--add-dir"] + sorted({os.path.dirname(path) for path in paths})
            prompt += "\n\nOpen each image with the Read tool before you answer:\n" + "\n".join(
                f"Image {i}: {path}" for i, path in enumerate(paths, 1))
        else:
            out += ["--tools", ""]  # no tools at all: the member only writes its answer
        return out + ["--", prompt]  # --tools and --add-dir take several values: without "--" they eat the prompt

    def run_turn(self, name, prompt, model=None, files=(), timeout=600):
        argv = self.args(name, prompt, model, files)
        folder = os.path.join(self.root, name)
        os.makedirs(folder, exist_ok=True)
        self._set(name, state="working")
        self.events.write(json.dumps({"t": round(time.time(), 2), "agent": name, "argv": argv[:-1] + ["<prompt>"]}) + "\n")
        err_path = os.path.join(self.root, name + ".stderr.log")  # a file, not a pipe: a full stderr pipe would stall it
        err = open(err_path, "a", encoding="utf-8")
        env = {key: value for key, value in os.environ.items() if key not in PARENT_SESSION}
        try:
            proc = subprocess.Popen(argv, cwd=folder, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=err,
                                    universal_newlines=True, encoding="utf-8", start_new_session=True)
        except OSError:  # no such program: the member shows the error, the caller gets the exception
            err.close()
            self._set(name, state="error")
            raise
        timed_out = []
        timer = threading.Timer(timeout, lambda: (timed_out.append(True), stop(proc)))
        timer.start()
        texts, result, session, seen, used = [], None, None, set(), 0
        try:
            for line in proc.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                self.events.write(json.dumps({"t": round(time.time(), 2), "agent": name, "event": event}) + "\n")
                session = event.get("session_id") or session
                kind = event.get("type")
                if kind == "assistant":
                    message = event.get("message") or {}
                    for block in message.get("content") or []:
                        if isinstance(block, dict) and block.get("type") == "text":
                            texts.append(block.get("text") or "")
                            self._set(name, stream={"kind": "text", "text": texts[-1][-300:]})
                    if message.get("id") not in seen:  # the same message comes once per content block
                        seen.add(message.get("id"))
                        used += tokens_of(message.get("usage"))
                elif kind == "result":
                    result = event
            proc.wait()
        finally:
            timer.cancel()
            if proc.poll() is None:  # interrupted (Ctrl-C in the runner): do not leave the member running
                stop(proc)
                proc.wait()
            proc.stdout.close()
            err.close()
        if result is not None:
            used = tokens_of(result.get("usage")) or used
        if timed_out or proc.returncode < 0:
            state = "aborted"
        elif result is None or result.get("is_error") or proc.returncode:
            state = "error"
        else:
            state = "idle"
        if session and not (state == "error" and result is not None and result.get("num_turns") == 0):
            self.sessions[name] = session
        else:
            self.forget(name)  # the conversation never started (e.g. "No conversation found"): do not resume it again
        if state != "idle":
            with open(err_path, encoding="utf-8") as handle:
                tail = handle.read()[-500:]
            self.events.write(json.dumps({"t": round(time.time(), 2), "agent": name, "exit": proc.returncode, "stderr": tail,
                                          "result": (result or {}).get("result")}) + "\n")
        with self.lock:
            tokens = self.agents.get(name, {}).get("tokens", 0) + used
            turns = self.agents.get(name, {}).get("turns", 0) + 1
        self._set(name, state=state, tokens=tokens, turns=turns)
        reply = ""
        if state == "idle":
            reply = result["result"] if isinstance(result.get("result"), str) else (texts[-1] if texts else "")
        return reply.strip(), state
