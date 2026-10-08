"""Codex CLI sessions as team members (one of herdr-py's member backends, see members.py), with the same contract as
slide_team.Team.run_turn: run_turn(name, prompt, model, files, timeout) -> (last reply text, state).

One `codex exec --json` per turn; later turns use `codex exec resume <thread>`, so every member keeps its own
conversation. Checked with codex-cli 0.160.0 on 2026-10-07:
- events: thread.started {thread_id}, item.completed {item: {type: agent_message, text}}, turn.completed {usage},
  error / turn.failed on failures;
- `exec resume` keeps the first turn's sandbox and folder (it has no -s or -C) and accepts --json, -i and -m;
- stdin must be closed: otherwise exec waits with "Reading additional input from stdin...";
- -i/--image takes several values, so the prompt must come after "--" or it is read as an image path.
A turn over its time limit is stopped with everything Codex started (its own process group) and ends "timeout", not
"aborted", so a slow member is told apart from one that was killed. Every member runs read-only in its own empty folder,
unless a turn names a workdir (a DAG step's workspace, dag.py): then it runs there, in Codex's workspace-write sandbox
(access "write": writes only inside the folder; reads are not limited) or read-only. agents.json (for view.py) holds
each member's state, tokens and last words; events.jsonl keeps every event.
"""
import json
import os
import signal
import subprocess
import threading
import time


def default_codex():
    app = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
    return os.environ.get("CODEX_BIN") or (app if os.path.exists(app) else "codex")


def stop(proc):
    """End Codex and anything it started (it runs in its own process group)."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:  # already gone
        pass


class CodexAgents:
    def __init__(self, root, codex=None, model=None, sandbox="read-only", fresh=False):
        """fresh=True: every turn is a new conversation (no resume); the caller's prompt carries everything."""
        self.root, self.codex, self.model, self.sandbox, self.fresh = root, codex or default_codex(), model, sandbox, fresh
        os.makedirs(root, exist_ok=True)
        self.threads, self.agents = {}, {}
        self.lock = threading.Lock()
        self.events = open(os.path.join(root, "events.jsonl"), "a", encoding="utf-8", buffering=1)

    def close(self):
        self.events.close()

    def forget(self, name):
        """This member's next turn starts a new conversation (teamrun.py: members with fresh sessions)."""
        self.threads.pop(name, None)

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

    def args(self, name, prompt, model=None, files=(), workdir=None, access=None):
        thread = None if (self.fresh or workdir) else self.threads.get(name)  # a workspace turn is always a new thread
        out = [self.codex, "exec"] + (["resume", thread] if thread else []) + ["--json", "--skip-git-repo-check"]
        if not thread:
            folder = workdir or os.path.join(self.root, name)
            os.makedirs(folder, exist_ok=True)
            sandbox = {"write": "workspace-write", "read": "read-only"}[access or "write"] if workdir else self.sandbox
            out += ["-s", sandbox, "-C", folder]
        if model or self.model:
            out += ["-m", model or self.model]
        for path in files:
            out += ["-i", os.path.abspath(path)]
        return out + ["--", prompt]  # -i takes several values: without "--" the prompt is read as one more image

    def run_turn(self, name, prompt, model=None, files=(), timeout=600, workdir=None, access=None):
        """workdir: run this turn in that folder (access "write", the default there, or "read")."""
        if access not in (None, "write", "read"):
            raise ValueError("access: write or read")
        argv = self.args(name, prompt, model, files, workdir=workdir, access=access)
        self._set(name, state="working")
        self.events.write(json.dumps({"t": round(time.time(), 2), "agent": name, "argv": argv[:-1] + ["<prompt>"]}) + "\n")
        err_path = os.path.join(self.root, name + ".stderr.log")  # a file, not a pipe: a full stderr pipe would stall Codex
        err = open(err_path, "a", encoding="utf-8")
        proc = subprocess.Popen(argv, cwd=self.root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=err,
                                universal_newlines=True, encoding="utf-8", start_new_session=True)
        timed_out = []
        timer = threading.Timer(timeout, lambda: (timed_out.append(True), stop(proc)))
        timer.start()
        texts, state, used = [], "idle", 0
        try:
            for line in proc.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                self.events.write(json.dumps({"t": round(time.time(), 2), "agent": name, "event": event}) + "\n")
                kind = event.get("type")
                if kind == "thread.started" and event.get("thread_id") and not workdir:  # never resume into a workspace
                    self.threads[name] = event["thread_id"]
                elif kind == "item.completed" and (event.get("item") or {}).get("type") == "agent_message":
                    texts.append(event["item"].get("text", ""))
                    self._set(name, stream={"kind": "text", "text": texts[-1][-300:]})
                elif kind == "turn.completed":
                    usage = event.get("usage") or {}
                    used = int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
                elif kind in ("error", "turn.failed"):
                    state = "error"
            proc.wait()
        finally:
            timer.cancel()
            proc.stdout.close()
            err.close()
        if timed_out:
            state = "timeout"
        elif proc.returncode and state != "error":
            state = "aborted" if proc.returncode < 0 else "error"
        if state != "idle":
            with open(err_path, encoding="utf-8") as handle:
                tail = handle.read()[-500:]
            row = {"t": round(time.time(), 2), "agent": name, "exit": proc.returncode, "stderr": tail}
            if timed_out:
                row["timeout"] = timeout
            self.events.write(json.dumps(row) + "\n")
        with self.lock:
            tokens = self.agents.get(name, {}).get("tokens", 0) + used
            turns = self.agents.get(name, {}).get("turns", 0) + 1
        self._set(name, state=state, tokens=tokens, turns=turns)
        return (texts[-1].strip() if texts else ""), state
