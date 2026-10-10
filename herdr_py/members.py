"""Team members behind one contract, whatever runs them.

    run_turn(name, prompt, files=(), timeout=600, workdir=None, access=None) -> (reply text, state)
        state is "idle" when the turn ended normally; "error", "aborted" or "timeout" otherwise (then the reply is not
        an answer, whatever it says); workdir: the folder this turn works in (a DAG step's workspace), with access
        "write" (default), "read" or "research" (read, plus the web; codex, claude and opencode limit their tools to
        match; a command member is a program you trust, it just runs there). A turn without a workdir only answers:
        claude and opencode members get no tools (codex runs read-only);
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
import json
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
# A turn that ended in one of these did not happen because the setup broke (a backend error, an abort, a model provider
# that stalled for as long as the turn's whole time limit): a caller stops instead of counting it against the member.
BROKEN = ("error", "aborted", "provider_stall")


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

    def can_continue(self, name, workdir=None, access=None):
        return True  # a program has no conversation: a repair is a new call, told so (HERDR_REPAIR) and given what to fix

    def run_turn(self, name, prompt, model=None, files=(), timeout=600, workdir=None, access=None, cont=False):
        env = dict(os.environ, HERDR_MEMBER=name)
        if cont:
            env["HERDR_REPAIR"] = "1"
        if model:
            env["HERDR_MODEL"] = model
        if files:
            env["HERDR_FILES"] = os.pathsep.join(os.path.abspath(path) for path in files)
        if workdir:
            env["HERDR_ACCESS"] = access or "write"
        with open(os.path.join(self.root, name + ".stderr.log"), "a", encoding="utf-8") as err:
            proc = subprocess.Popen(self.commands[name], cwd=workdir or self.cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
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


# A turn in a given folder (a DAG step's clone): the session works there (OpenCode's ?directory=) and herdr-py refuses
# its calls outside the folder and, for a step that only reads, every edit and command. herdr-py hears of a call only
# when OpenCode asks about it, so OpenCode must ask about these kinds (README: Permission policy).
REFUSED = {"write": ["external_directory"], "read": ["external_directory", "edit", "bash"],
           "research": ["external_directory", "edit", "bash"]}  # web fetches are left to the daemon's policy
NO_TOOLS = {"*": False}  # a turn outside a folder (a planner's) only answers, as a claude member's (--tools "")


class DaemonMembers:
    """OpenCode agents through a running herdr-py daemon: start the agent (a new session after forget(name)) or prompt
    its session, wait until it stops, and read what it said after the last prompt. With a workdir, every turn is a new
    session working in that folder (REFUSED says what it may not do); before it starts, the daemon must say OpenCode
    asks about those kinds and OpenCode must list the folder as this machine does, or MemberError says why."""

    def __init__(self, socket=None, client=None, poll_s=1.0, clock=time.time, sleep=time.sleep, log_dir=None):
        self.client = client or Client(socket, timeout=None)
        self.poll_s, self.clock, self.sleep = poll_s, clock, sleep
        self.fresh = set()
        self.unasked = None  # the kinds OpenCode runs without asking, from the daemon's ping (read once)
        self.log = os.path.join(log_dir, "events.jsonl") if log_dir else None

    def note_tools(self, name, messages):
        """The turn's tool calls, one line each in log_dir/events.jsonl ({"agent", "t", "event"}, as the codex and
        claude backends keep theirs), so a DAG run can count calls that name another step's clone (dag.out_of_bounds)."""
        if not self.log:
            return
        os.makedirs(os.path.dirname(self.log), exist_ok=True)
        with open(self.log, "a", encoding="utf-8") as handle:
            for message in messages[turn_start(messages) + 1:]:
                if message.get("kind") == "tool":
                    handle.write(json.dumps({"agent": name, "t": time.time(), "event": message}, ensure_ascii=False) + "\n")

    def forget(self, name):
        self.fresh.add(name)

    def close(self):
        pass

    def check_folder(self, workdir, access):
        if self.unasked is None:
            unasked = self.client.call("ping").get("opencode_does_not_ask")
            if unasked is None:
                raise MemberError("the daemon could not read OpenCode's permission config, so nothing shows that OpenCode "
                                  "asks before the calls an opencode member may not make outside its folder")
            self.unasked = {item.split("=")[0] for item in unasked}
        loose = [kind for kind in REFUSED[access] if kind in self.unasked]  # research: as read
        if loose:
            raise MemberError(f"OpenCode does not ask before {', '.join(loose)}, so herdr-py could not refuse those calls "
                              f"to an opencode member working in {workdir}: start OpenCode with OPENCODE_CONFIG_CONTENT "
                              "(README: Permission policy)")
        try:
            seen = set(self.client.call("folder.list", directory=workdir)["names"])
        except ClientError as exc:
            raise MemberError(f"OpenCode cannot see {workdir} ({exc}); an OpenCode in a container needs the folder "
                              "mounted at the same path") from None
        if not seen <= set(os.listdir(workdir)):
            raise MemberError(f"OpenCode lists other files in {workdir} than this machine does ({sorted(seen)[:5]}): "
                              "an OpenCode in a container needs the folder mounted at the same path")

    def run_turn(self, name, prompt, model=None, files=(), timeout=600, workdir=None, access=None):
        place = {"tools": NO_TOOLS}
        if workdir:
            workdir, access = os.path.abspath(workdir), access or "write"
            if access not in REFUSED:
                raise MemberError(f"access is {', '.join(REFUSED)}, not {access!r}")
            self.check_folder(workdir, access)
            place = {"directory": workdir, "deny": REFUSED[access]}
        agents = {a["name"]: a for a in self.client.call("agent.list")["agents"]}
        fresh = name in self.fresh or bool(workdir)  # a turn in a folder never continues a session from another one
        self.fresh.discard(name)
        if name in agents and not fresh:
            base = agents[name].get("provider_wait_s", 0)
            self.client.call("agent.prompt", name=name, text=prompt, files=list(files))
        else:
            base = 0  # a new session counts from 0
            self.client.call("agent.start", name=name, prompt=prompt, model=model, files=list(files), fresh=name in agents,
                             **place)
        # The time limit is the member's own time: what the model provider lost (the daemon's provider_wait_s, from
        # OpenCode's retry reports) is added to it, up to one more limit; a provider that takes longer ends the turn as
        # provider_stall, a broken setup and not the member's failure.
        start, lost = self.clock(), 0.0
        while True:
            view = self.client.call("agent.get", name=name)
            if view["state"] in FINAL and not view["followups_left"]:
                state = view["state"]
                break
            lost = max(0.0, view.get("provider_wait_s", 0) - base)
            spent = self.clock() - start
            if spent - lost > timeout or spent > 2 * timeout:
                state = "timeout" if spent - lost > timeout else "provider_stall"
                self.client.call("agent.abort", name=name, reason="turn time limit" if state == "timeout" else
                                 f"the model provider stalled {lost:.0f} s")
                break
            self.sleep(self.poll_s)
        messages = self.client.call("agent.read", name=name, limit=2000)["messages"]  # a long turn has many messages
        self.note_tools(name, messages)
        reply = this_turn_reply(messages)
        if state == "provider_stall":
            reply = f"(the model provider stalled: {lost:.0f} s of {self.clock() - start:.0f} s) {reply}".strip()
        return reply, state

    def tokens(self, name):
        """The daemon's token count for this member: 0 before its first turn (as the codex and claude backends count,
        so the first turn's tokens can be worked out), None when the daemon cannot be asked."""
        try:
            agents = {a["name"]: a for a in self.client.call("agent.list")["agents"]}
        except (ClientError, OSError, KeyError):
            return None
        return agents[name].get("tokens", 0) if name in agents else 0


def turn_start(messages):
    """Where this turn starts: the last user text OpenCode did not write itself (when a session fills up in the middle
    of a turn, OpenCode compacts it and adds a synthetic "Continue if you have next steps ..."; that is not a prompt)."""
    return max((i for i, m in enumerate(messages) if m.get("role") == "user" and m.get("kind") == "text"
                and not m.get("synthetic")), default=-1)


def this_turn_reply(messages):
    """The last thing the assistant said after the last prompt: never an earlier turn's answer."""
    texts = [m["text"] for m in messages[turn_start(messages) + 1:] if m.get("role") == "assistant" and m.get("kind") == "text"]
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
            self.backends["opencode"] = DaemonMembers(socket, log_dir=os.path.join(work, "opencode"))
        if "command" in kinds:
            self.backends["command"] = CommandMembers(os.path.join(work, "command"), cwd=cwd)
            for spec in self.spec.values():
                if spec["backend"] == "command":
                    self.backends["command"].add(spec["name"], spec["command"])
        self.stats = {name: {"turns": 0, "seconds": 0.0, "states": {}} for name in self.spec}
        self.lock = threading.Lock()

    def names(self):
        return list(self.spec)

    def can_continue(self, name, workdir=None, access=None):
        """True when this member's backend can continue its last turn (a repair): Claude and command members can."""
        backend = self.backends[self.spec[name]["backend"]]
        check = getattr(backend, "can_continue", None)
        return bool(check and check(name, workdir, access if workdir else None))

    def run_turn(self, name, prompt, files=(), timeout=600, workdir=None, access=None, cont=False):
        """cont: continue the member's last turn (a repair), which can_continue must allow."""
        member = self.spec[name]
        backend = self.backends[member["backend"]]
        if self.sessions == "fresh" and not cont:
            backend.forget(name)
        start, state, text = time.time(), "error", ""
        place = {"workdir": workdir, "access": access} if workdir else {}
        if cont:
            place["cont"] = True
        try:
            text, state = backend.run_turn(name, prompt, member["model"], files=files, timeout=timeout, **place)
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
        # Codex and Claude count tokens per member from the member's first turn on: none yet is 0, not unknown
        # (otherwise the tokens of every member's first turn could not be worked out from before and after)
        return (backend.agents.get(name) or {}).get("tokens", 0)

    def summary(self):
        return [dict(self.spec[name], turns=s["turns"], seconds=round(s["seconds"], 2), states=s["states"],
                     tokens=self.tokens(name)) for name, s in self.stats.items()]

    def close(self):
        for backend in self.backends.values():
            backend.close()
