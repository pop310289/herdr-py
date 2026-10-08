"""Team members behind one contract, whatever runs them.

    run_turn(name, prompt, files=(), timeout=600) -> (reply text, state)
        state is "idle" when the turn ended normally; "error", "aborted" or "timeout" otherwise (then the reply is not
        an answer, whatever it says);
    tokens(name) -> tokens used so far, or None when the backend does not count them.

Backends (one member can use any of them; one team can mix them):
  opencode  OpenCode agents through a running herdr-py daemon (its socket)
  codex     Codex CLI (codex_agents.CodexAgents): codex exec --json, read-only, each member in its own empty folder
  claude    Claude Code CLI (claude_agents.ClaudeAgents): claude -p, no tools, each member in its own empty folder
  command   any program: the prompt goes to its stdin, its stdout is the reply (a rule-guided loop you already have,
            another agent CLI, a script). A new process every turn, run in the folder the team was started from, and
            stopped with its whole process group at the time limit. HERDR_MEMBER (and HERDR_MODEL, HERDR_FILES when
            given) tell it who it is.
On the command line a member is NAME=BACKEND[:MODEL] or NAME=command:COMMAND (parse_member). By default every turn
starts a new session (the prompt must carry everything); sessions="keep" lets codex, claude and opencode members keep
their conversation.
"""
import os
import re
import shlex
import signal
import subprocess
import threading
import time

from .claude_agents import ClaudeAgents
from .client import Client, ClientError
from .codex_agents import CodexAgents

BACKENDS = ("opencode", "codex", "claude", "command")
NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")  # also a folder name
FINAL = ("idle", "aborted", "error")


class MemberError(ValueError):
    pass


def parse_member(text):
    """NAME=BACKEND[:MODEL] or NAME=command:COMMAND -> {"name", "backend", "model", "command"}."""
    name, sep, rest = text.partition("=")
    if not sep or not NAME.match(name):
        raise MemberError(f"{text!r}: give NAME=BACKEND[:MODEL] or NAME=command:COMMAND (a name of letters, digits, - and _)")
    backend, _, extra = rest.partition(":")
    if backend not in BACKENDS:
        raise MemberError(f"{text!r}: the backend is one of {', '.join(BACKENDS)}")
    if backend == "command":
        if not extra.strip():
            raise MemberError(f"{text!r}: give the command after command:")
        return {"name": name, "backend": "command", "model": None, "command": extra.strip()}
    return {"name": name, "backend": backend, "model": extra or None, "command": None}


def stop_group(proc, grace=5):
    """Kill a member's whole process group; stop waiting for its pipes after `grace` seconds (a grandchild that left
    the group may still hold them)."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.communicate(timeout=grace)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass
        proc.wait()


class CommandMembers:
    """Members that are programs: prompt on stdin, reply on stdout, a new process every turn."""

    def __init__(self, root, cwd=None):
        self.root, self.cwd = root, cwd or os.getcwd()
        os.makedirs(root, exist_ok=True)
        self.commands, self.agents = {}, {}

    def add(self, name, command):
        self.commands[name] = shlex.split(command) if isinstance(command, str) else list(command)

    def forget(self, name):
        pass  # every turn is a new process already

    def close(self):
        pass

    def run_turn(self, name, prompt, model=None, files=(), timeout=600):
        env = dict(os.environ, HERDR_MEMBER=name)
        if model:
            env["HERDR_MODEL"] = model
        if files:
            env["HERDR_FILES"] = os.pathsep.join(os.path.abspath(path) for path in files)
        with open(os.path.join(self.root, name + ".stderr.log"), "a", encoding="utf-8") as err:
            proc = subprocess.Popen(self.commands[name], cwd=self.cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=err, universal_newlines=True, encoding="utf-8", errors="replace",
                                    start_new_session=True)
            try:
                out, _ = proc.communicate(prompt, timeout=timeout)
                state = "idle" if proc.returncode == 0 else ("aborted" if proc.returncode < 0 else "error")
            except subprocess.TimeoutExpired:
                stop_group(proc)
                out, state = "", "timeout"
        agent = self.agents.setdefault(name, {"name": name, "turns": 0})
        agent.update(turns=agent["turns"] + 1, state=state)
        return out.strip(), state

    def tokens(self, name):
        return None  # a program does not report what it used


class DaemonMembers:
    """OpenCode agents through a running herdr-py daemon: start the agent (a new session after forget(name)) or prompt
    its session, wait until it stops, and read what it said after the last prompt."""

    def __init__(self, socket=None, client=None, poll_s=1.0, clock=time.time, sleep=time.sleep):
        self.client = client or Client(socket, timeout=None)
        self.poll_s, self.clock, self.sleep = poll_s, clock, sleep
        self.fresh = set()

    def forget(self, name):
        self.fresh.add(name)

    def close(self):
        pass

    def run_turn(self, name, prompt, model=None, files=(), timeout=600):
        names = {a["name"] for a in self.client.call("agent.list")["agents"]}
        fresh = name in self.fresh
        self.fresh.discard(name)
        if name in names and not fresh:
            self.client.call("agent.prompt", name=name, text=prompt, files=list(files))
        else:
            self.client.call("agent.start", name=name, prompt=prompt, model=model, files=list(files), fresh=name in names)
        start = self.clock()
        while True:
            view = self.client.call("agent.get", name=name)
            if view["state"] in FINAL and not view["followups_left"]:
                state = view["state"]
                break
            if self.clock() - start > timeout:
                self.client.call("agent.abort", name=name, reason="turn time limit")
                state = "timeout"
                break
            self.sleep(self.poll_s)
        return this_turn_reply(self.client.call("agent.read", name=name, limit=60)["messages"]), state

    def tokens(self, name):
        try:
            return self.client.call("agent.get", name=name)["tokens"]
        except (ClientError, OSError, KeyError):
            return None


def this_turn_reply(messages):
    """The last thing the assistant said after the last user message: never an earlier turn's answer."""
    last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user" and m.get("kind") == "text"), default=-1)
    texts = [m["text"] for m in messages[last_user + 1:] if m.get("role") == "assistant" and m.get("kind") == "text"]
    return texts[-1].strip() if texts else ""


class Members:
    """Sends each member's turn to its backend (members of one backend share an instance), times every turn and
    counts how turns ended. specs: dicts from parse_member, or with the same keys."""

    def __init__(self, specs, work, socket=None, sessions="fresh", cwd=None):
        if sessions not in ("fresh", "keep"):
            raise MemberError("sessions: fresh or keep")
        self.spec, self.sessions = {}, sessions
        for spec in specs:
            if spec["name"] in self.spec:
                raise MemberError(f"{spec['name']}: two members with one name")
            self.spec[spec["name"]] = spec
        kinds = {s["backend"] for s in self.spec.values()}
        fresh = sessions == "fresh"
        self.backends = {}
        if "codex" in kinds:
            self.backends["codex"] = CodexAgents(os.path.join(work, "codex"), fresh=fresh)
        if "claude" in kinds:
            self.backends["claude"] = ClaudeAgents(os.path.join(work, "claude"), fresh=fresh)
        if "opencode" in kinds:
            if not socket:
                raise MemberError("opencode members need the herdr-py daemon's socket")
            self.backends["opencode"] = DaemonMembers(socket)
        if "command" in kinds:
            self.backends["command"] = CommandMembers(os.path.join(work, "command"), cwd=cwd)
            for spec in self.spec.values():
                if spec["backend"] == "command":
                    self.backends["command"].add(spec["name"], spec["command"])
        self.stats = {name: {"turns": 0, "seconds": 0.0, "states": {}} for name in self.spec}
        self.lock = threading.Lock()

    def names(self):
        return list(self.spec)

    def run_turn(self, name, prompt, files=(), timeout=600):
        member = self.spec[name]
        backend = self.backends[member["backend"]]
        if self.sessions == "fresh":
            backend.forget(name)
        start, state, text = time.time(), "error", ""
        try:
            text, state = backend.run_turn(name, prompt, member["model"], files=files, timeout=timeout)
        except (OSError, ClientError) as exc:  # the program is missing, the daemon is gone: this turn failed, say why
            text, state = f"(the turn could not run: {type(exc).__name__}: {exc})", "error"
        finally:
            with self.lock:
                stats = self.stats[name]
                stats["turns"] += 1
                stats["seconds"] += time.time() - start
                stats["states"][state] = stats["states"].get(state, 0) + 1
        return text, state

    def tokens(self, name):
        backend = self.backends[self.spec[name]["backend"]]
        if hasattr(backend, "tokens"):
            return backend.tokens(name)
        return (backend.agents.get(name) or {}).get("tokens")

    def summary(self):
        return [dict(self.spec[name], turns=s["turns"], seconds=round(s["seconds"], 2), states=s["states"],
                     tokens=self.tokens(name)) for name, s in self.stats.items()]

    def close(self):
        for backend in self.backends.values():
            backend.close()
