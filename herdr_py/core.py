"""The hub: every agent herdr-py drives, kept in sync with OpenCode's own events.

State of an agent (what UIs show):
  starting  created, first prompt sent, OpenCode has not reported busy yet
  working   OpenCode says the session is busy
  retry     OpenCode is retrying a provider error
  blocked   a permission request or question (the agent's own or a subagent's) is waiting for an answer
  idle      the session is idle and no follow-up is queued
  aborted   herdr-py aborted it (time budget or a user request); the next prompt clears it
  error     OpenCode reported a session error; the next prompt clears it

Only sessions created by herdr-py (and their subagent sessions) are touched: requests from other sessions on the same
OpenCode server are ignored.
"""
import base64
import collections
import json
import mimetypes
import os
import queue
import threading
import time

from .policy import REPLIES, describe

CHILD_STATE_EVENTS = {"permission.asked", "permission.replied", "question.asked", "question.replied", "question.rejected"}
FINAL = {"idle", "aborted", "error"}
STATES = ("starting", "working", "retry", "blocked", "idle", "aborted", "error")


class HubError(Exception):
    pass


MAX_FILE_BYTES = 8 * 1024 * 1024


def file_parts(paths):
    """Local files -> OpenCode file parts with data URLs (read by the daemon, so paths are the daemon's paths)."""
    parts = []
    for path in paths or ():
        size = os.path.getsize(path)
        if size > MAX_FILE_BYTES:
            raise HubError(f"{path} is {size} bytes; attachments are limited to {MAX_FILE_BYTES}")
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as handle:
            data = base64.b64encode(handle.read()).decode()
        parts.append({"mime": mime, "url": f"data:{mime};base64,{data}", "filename": os.path.basename(path)})
    return parts


class Agent:
    def __init__(self, name, session_id, model=None, budget_s=None, followups=(), created=None):
        self.name, self.session_id, self.model = name, session_id, model
        self.budget_s = budget_s
        self.followups = list(followups)
        self.created = created or time.time()
        self.base = "starting"            # from session.status / session.idle
        self.sticky = None                # "aborted" or "error" until the next prompt
        self.since = self.created
        self.history = []                 # [(t, state)]
        self.children = set()
        self.pending = collections.OrderedDict()   # request id -> {"kind": "permission"|"question", "request": ..., "child": bool}
        self.activity = collections.OrderedDict()  # key -> (t, text, tone)
        self.stream = {"part": None, "kind": "", "text": ""}
        self.tokens = 0
        self.turns = 0                    # prompts sent
        self.idles = 0                    # turns finished (busy -> idle)
        self.awaiting_busy = False        # a prompt was sent and OpenCode has not reported busy yet
        self.working_since = None         # start of the current busy period (for the time budget)
        self.decisions = []               # [(t, request id, description, action, by)]
        self.seq = 0                      # +1 on every state change (waits compare against a baseline, as in herdr)
        self.past_sessions = []           # earlier OpenCode sessions of this name (start with fresh=True)

    @property
    def state(self):
        if self.sticky:
            return self.sticky
        if self.pending:
            return "blocked"
        return self.base

    def view(self, now=None):
        now = now or time.time()
        return {"name": self.name, "session_id": self.session_id, "state": self.state, "since": round(self.since, 3),
                "seconds_in_state": round(now - self.since, 1), "tokens": self.tokens, "turns": self.turns,
                "followups_left": len(self.followups), "budget_s": self.budget_s, "children": sorted(self.children),
                "seq": self.seq, "completions": self.idles,
                "pending": [{"id": rid, "kind": p["kind"], "child": p["child"], "description": p["description"]}
                            for rid, p in self.pending.items()],
                "activity": [{"t": round(t, 3), "text": text, "tone": tone} for t, text, tone in list(self.activity.values())[-6:]],
                "stream": {"kind": self.stream["kind"], "text": self.stream["text"][-400:]},
                "model": self.model, "past_sessions": self.past_sessions[-5:]}


class Hub:
    def __init__(self, client, policy, log_path=None, state_path=None, model=None, questions="ask",
                 clock=time.time, run=None, max_queue=2000, max_agents=None, max_prompts=None):
        self.max_agents, self.max_prompts = max_agents, max_prompts  # guards against a runaway manager agent
        self.client, self.policy, self.model = client, policy, model
        self.questions = questions        # "ask" or "reject"
        self.clock = clock
        self.run = run or (lambda fn: threading.Thread(target=fn, daemon=True).start())
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.agents = collections.OrderedDict()   # name -> Agent
        self.by_session = {}                      # root session id -> name
        self.child_root = {}                      # subagent session id -> root session id
        self.part_kind = {}                       # part id -> part type
        self.subscribers = []
        self.max_queue = max_queue
        self.state_path = state_path
        self.connected = False
        self.log = open(log_path, "a", encoding="utf-8", buffering=1) if log_path else None  # one line per write

    # ---------------------------------------------------------------- bookkeeping
    def record(self, entry):
        if self.log:
            self.log.write(json.dumps({"t": round(self.clock(), 3), **entry}, ensure_ascii=False) + "\n")

    def emit(self, event):
        event = {"t": round(self.clock(), 3), **event}
        dead = []
        for q in self.subscribers:
            try:
                q.put_nowait(event)
            except queue.Full:  # a subscriber that falls behind is told so and dropped, never silently skipped
                try:
                    q.get_nowait()
                    q.put_nowait({"t": event["t"], "type": "events_lost"})
                except (queue.Empty, queue.Full):
                    pass
                dead.append(q)
        for q in dead:
            self.subscribers.remove(q)
        self.changed.notify_all()

    def subscribe(self):
        q = queue.Queue(self.max_queue)
        with self.lock:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def note(self, agent, key, text, tone=""):
        agent.activity[key] = (self.clock(), text, tone)
        agent.activity.move_to_end(key)
        while len(agent.activity) > 12:
            agent.activity.popitem(last=False)

    def refresh(self, agent, reason=""):
        """Record a state change (if any) and tell subscribers."""
        state = agent.state
        if not agent.history or agent.history[-1][1] != state:
            agent.seq += 1
            agent.since = self.clock()
            agent.history.append((agent.since, state))
            self.record({"hub": "state", "agent": agent.name, "state": state, "reason": reason})
            self.emit({"type": "agent.state", "agent": agent.name, "state": state, "reason": reason})
        else:
            self.emit({"type": "agent.update", "agent": agent.name})

    def save(self):
        if not self.state_path:
            return
        with self.lock:
            data = {"version": 1, "agents": [{"name": a.name, "session_id": a.session_id, "model": a.model, "budget_s": a.budget_s,
                                               "followups": a.followups, "turns": a.turns, "idles": a.idles, "created": a.created,
                                               "children": sorted(a.children), "past_sessions": a.past_sessions}
                                              for a in self.agents.values()]}
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=1)
        os.replace(tmp, self.state_path)

    def close(self):
        if self.log:
            self.log.close()
            self.log = None

    # ---------------------------------------------------------------- public API
    def agent(self, name):
        agent = self.agents.get(name)
        if agent is None:
            raise HubError(f"no agent named {name!r}")
        return agent

    def list(self):
        with self.lock:
            now = self.clock()
            return [a.view(now) for a in self.agents.values()]

    def get(self, name):
        with self.lock:
            return self.agent(name).view(self.clock())

    def start(self, name, prompt, budget_s=None, followups=(), model=None, title=None, files=(), fresh=False):
        """Create an agent (an OpenCode session) and send its first prompt. fresh=True with a name that exists gives that
        agent a new session instead: OpenCode compacts a long session at a moment nobody chooses, so a caller that puts
        everything the turn needs into the prompt can start every turn clean. The role's counters (tokens, turns,
        decisions) carry on; the old session and its subagents are dropped, and their late events are ignored."""
        with self.lock:
            old = self.agents.get(name)
            if old is not None:
                if not fresh:
                    raise HubError(f"agent {name!r} already exists")
                if old.state not in FINAL or old.awaiting_busy:
                    raise HubError(f"agent {name!r} is {old.state}: abort it or wait for it before giving it a new session")
            elif self.max_agents is not None and len(self.agents) >= self.max_agents:
                raise HubError(f"max_agents ({self.max_agents}) reached")
        session = self.client.create_session(title or f"herdr-py: {name}")
        with self.lock:
            agent = Agent(name, session["id"], model or (old.model if old else self.model), budget_s, followups, created=self.clock())
            if old is not None:
                agent.tokens, agent.turns, agent.idles, agent.decisions = old.tokens, old.turns, old.idles, old.decisions
                agent.seq, agent.history = old.seq, old.history
                agent.past_sessions = old.past_sessions + [old.session_id]
                for sid in [old.session_id] + sorted(old.children):
                    self.by_session.pop(sid, None)
                    self.child_root.pop(sid, None)
            self.agents[name] = agent
            self.by_session[agent.session_id] = name
            self.record({"hub": "start", "agent": name, "session": agent.session_id,
                         **({"renewed_from": old.session_id} if old is not None else {})})
        self.save()
        self.prompt(name, prompt, source="start", files=files)
        return self.get(name)

    def prompt(self, name, text, source="user", files=()):
        parts = file_parts(files)  # read before changing any state, so a bad path leaves the agent as it was
        with self.lock:
            agent = self.agent(name)
            if self.max_prompts is not None and agent.turns >= self.max_prompts:
                raise HubError(f"max_prompts ({self.max_prompts}) reached for {name}")
            agent.sticky = None
            agent.awaiting_busy = True
            agent.turns += 1
            if agent.base in FINAL or agent.base == "starting":
                agent.base = "starting"
            self.note(agent, f"prompt-{agent.turns}", f"prompt #{agent.turns} ({source}): {text[:60]}", "info")
            self.record({"hub": "prompt", "agent": name, "source": source, "text": text})
            self.refresh(agent, "prompt")
            session_id, model = agent.session_id, agent.model
        self.client.prompt(session_id, text, model=model, files=parts)
        self.save()
        return self.get(name)

    def abort(self, name, reason="user"):
        with self.lock:
            agent = self.agent(name)
            session_id = agent.session_id
        self.client.abort(session_id)
        with self.lock:
            agent.sticky = "aborted"
            agent.followups = []
            agent.working_since = None
            self.note(agent, f"abort-{self.clock()}", f"aborted ({reason})", "warn")
            self.record({"hub": "abort", "agent": name, "reason": reason})
            self.refresh(agent, f"abort: {reason}")
        self.save()
        return self.get(name)

    def reply(self, request_id, reply, message=None, by="human"):
        """Answer a pending permission ("once"/"always"/"reject") or dismiss a pending question ("reject")."""
        with self.lock:
            agent, pending = self.find_pending(request_id)
            if pending is None:
                raise HubError(f"no pending request {request_id!r}")
        if pending["kind"] == "question":
            if reply != "reject":
                raise HubError("questions can only be dismissed (reply 'reject') in herdr-py 0.1")
            self.client.reject_question(request_id)
        else:
            if reply not in ("once", "always", "reject"):
                raise HubError("reply must be once, always or reject")
            self.client.reply_permission(request_id, reply, message)
        with self.lock:
            agent.decisions.append((self.clock(), request_id, pending["description"], reply, by))
            verdict = {"once": "allowed once", "always": "allowed always", "reject": "rejected"}[reply]
            self.note(agent, request_id, f"{'child ' if pending['child'] else ''}{pending['description'][:48]} -> {verdict} ({by})",
                      "ok" if reply != "reject" else "bad")
            self.record({"hub": "decision", "agent": agent.name, "request": request_id, "reply": reply, "by": by,
                         "description": pending["description"]})
            self.emit({"type": "permission.decided", "agent": agent.name, "request": request_id, "reply": reply, "by": by})
        return {"agent": agent.name, "request": request_id, "reply": reply}

    def find_pending(self, request_id):
        for agent in self.agents.values():
            if request_id in agent.pending:
                return agent, agent.pending[request_id]
        return None, None

    def pending(self, name=None):
        with self.lock:
            agents = [self.agent(name)] if name else list(self.agents.values())
            return [{"agent": a.name, "id": rid, "kind": p["kind"], "child": p["child"], "description": p["description"]}
                    for a in agents for rid, p in a.pending.items()]

    def prompt_and_wait(self, name, text, until=("idle",), timeout=None, activity_s=5.0, source="user", files=()):
        """Send a prompt and wait for the turn it starts to finish (herdr's `agent prompt --wait`).

        Within `activity_s` OpenCode must report the session busy (or a request); otherwise HubError("prompt_stalled"):
        the prompt may still have been delivered, so read the agent before sending it again. A finished turn means
        the completion count went past its value before the prompt; an idle from an older turn never counts.
        """
        with self.lock:
            baseline = self.agent(name).idles
        self.prompt(name, text, source=source, files=files)
        deadline = None if timeout is None else time.time() + timeout
        activity_deadline = time.time() + activity_s
        until = set(until)
        with self.lock:
            while True:
                agent = self.agent(name)
                started = not agent.awaiting_busy or agent.pending or agent.sticky
                if not started and time.time() > activity_deadline:
                    raise HubError(f"prompt_stalled: {name} showed no activity within {activity_s}s; "
                                   f"the prompt may still be delivered, read it before sending again")
                # `idles > baseline` is a second guard: on_idle already ignores idles while awaiting_busy, so an idle
                # from an older turn cannot get here (mutation testing found `>=` equivalent for that reason)
                if started and (agent.state in ("aborted", "error") or
                                (agent.state in until and (agent.state != "idle" or agent.idles > baseline))):
                    return agent.view(self.clock())
                if deadline is not None and time.time() > deadline:
                    raise HubError(f"timeout waiting for {name} to finish (state: {agent.state})")
                self.changed.wait(0.25)

    def wait(self, name, until=("idle",), timeout=None):
        """Block until the agent reaches one of the states in `until` (a prompt still waiting for OpenCode to start does
        not count as idle). Returns the agent view, or raises HubError on timeout."""
        until = set(until)
        deadline = None if timeout is None else time.time() + timeout
        with self.lock:
            while True:
                agent = self.agent(name)
                if agent.state in until and not (agent.awaiting_busy and agent.state == "idle"):
                    return agent.view(self.clock())
                remaining = None if deadline is None else deadline - time.time()
                if remaining is not None and remaining <= 0:
                    raise HubError(f"timeout waiting for {name} to reach {sorted(until)} (state: {agent.state})")
                self.changed.wait(0.5 if remaining is None else min(0.5, remaining))

    def transcript(self, name, limit=20):
        with self.lock:
            session_id = self.agent(name).session_id
        out = []
        for message in (self.client.messages(session_id) or [])[-limit:]:
            role = (message.get("info") or {}).get("role", "?")
            for part in message.get("parts") or []:
                if part.get("type") == "text" and part.get("text"):
                    out.append({"role": role, "kind": "text", "text": part["text"]})
                elif part.get("type") == "tool":
                    state = part.get("state") or {}
                    out.append({"role": role, "kind": "tool", "tool": part.get("tool"), "status": state.get("status"),
                                "input": state.get("input"), "output": (state.get("output") or "")[-2000:]})
        return out

    # ---------------------------------------------------------------- automation (called every ~0.25 s)
    def tick(self):
        actions = []
        with self.lock:
            now = self.clock()
            for agent in self.agents.values():
                if agent.budget_s and agent.working_since and not agent.sticky and now - agent.working_since > agent.budget_s:
                    actions.append(("abort", agent.name, f"time budget {agent.budget_s}s"))
                    agent.working_since = None
                elif agent.followups and agent.state == "idle" and not agent.awaiting_busy and agent.idles >= agent.turns:
                    actions.append(("prompt", agent.name, agent.followups.pop(0)))
        for kind, name, arg in actions:
            try:
                if kind == "abort":
                    self.abort(name, reason=arg)
                else:
                    self.prompt(name, arg, source="follow-up")
            except Exception as exc:  # an automation that fails must be visible, not swallowed
                with self.lock:
                    agent = self.agents.get(name)
                    if agent:
                        self.note(agent, f"auto-error-{self.clock()}", f"automation failed: {exc}", "bad")
                        self.refresh(agent, "automation error")
                self.record({"hub": "automation_error", "agent": name, "action": kind, "error": str(exc)})

    # ---------------------------------------------------------------- OpenCode events
    def root_of(self, session_id, kind=None, props=None):
        """Root session id for one of our sessions or its subagents; None for sessions we do not own."""
        if session_id in self.by_session:
            return session_id
        if session_id in self.child_root:
            return self.child_root[session_id]
        props = props or {}
        info = props.get("info") or {}
        parent = info.get("parentID") if kind in ("session.created", "session.updated") and info.get("id") == session_id else None
        if parent is None and kind in CHILD_STATE_EVENTS:  # a subagent's request can arrive before session.created
            try:
                parent = (self.client.session(session_id) or {}).get("parentID")
            except Exception:
                parent = None
        seen = 0
        while parent is not None and seen < 8:  # subagents can start their own subagents
            if parent in self.by_session or parent in self.child_root:
                root = parent if parent in self.by_session else self.child_root[parent]
                self.child_root[session_id] = root
                agent = self.agents[self.by_session[root]]
                agent.children.add(session_id)
                self.note(agent, f"child-{session_id}", f"subagent started ({session_id[-8:]})", "info")
                return root
            try:
                parent = (self.client.session(parent) or {}).get("parentID")
            except Exception:
                return None
            seen += 1
        return None

    def on_event(self, event):
        kind, props = event.get("type", ""), event.get("properties") or {}
        if kind in ("server.heartbeat", "server.connected"):
            return
        part = props.get("part") or {}
        session_id = props.get("sessionID") or part.get("sessionID") or (props.get("info") or {}).get("sessionID") \
            or (props.get("info") or {}).get("id")
        with self.lock:
            root = self.root_of(session_id, kind, props) if session_id else None
            if root is None:
                return
            if self.log and kind != "message.part.delta":
                self.record({"opencode": event})
            agent = self.agents[self.by_session[root]]
            self.handle(agent, kind, props, part, child=session_id != root)

    def handle(self, agent, kind, props, part, child):
        if kind == "message.part.updated":
            self.part_kind[part.get("id")] = part.get("type")
            if part.get("type") == "tool":
                state = part.get("state") or {}
                inp = state.get("input") or {}
                target = inp.get("command") or inp.get("description") or state.get("title") or inp.get("filePath") or ""
                mark = {"completed": "done", "error": "failed"}.get(state.get("status"), "...")
                tone = {"completed": "", "error": "bad"}.get(state.get("status"), "dim")
                self.note(agent, part.get("callID"), f"{'child ' if child else ''}{part.get('tool')} {str(target)[:50]} {mark}", tone)
                self.emit({"type": "agent.update", "agent": agent.name})
            elif part.get("type") == "step-finish":
                agent.tokens += (part.get("tokens") or {}).get("total") or 0
            return
        if kind == "message.part.delta":
            if not child and props.get("field") == "text" and self.part_kind.get(props.get("partID")) in ("text", "reasoning"):
                if agent.stream["part"] != props["partID"]:
                    agent.stream = {"part": props["partID"], "text": "",
                                    "kind": "reasoning" if self.part_kind[props["partID"]] == "reasoning" else "reply"}
                agent.stream["text"] = (agent.stream["text"] + props.get("delta", ""))[-2000:]
                self.emit({"type": "agent.output", "agent": agent.name, "kind": agent.stream["kind"], "delta": props.get("delta", "")})
            return
        if kind == "permission.asked":
            self.on_request(agent, "permission", props, child)
            return
        if kind == "question.asked":
            self.on_request(agent, "question", props, child)
            return
        if kind in ("permission.replied", "question.replied", "question.rejected"):
            request_id = props.get("requestID") or props.get("id")
            if agent.pending.pop(request_id, None) is not None:
                self.refresh(agent, kind)
            return
        if child:
            return  # a subagent going busy or idle does not change its parent's state
        if kind == "session.status":
            status = (props.get("status") or {}).get("type")
            if status == "busy":
                self.on_busy(agent)
            elif status == "retry":
                agent.base = "retry"
                self.refresh(agent, "retry")
            elif status == "idle":
                self.on_idle(agent)
        elif kind == "session.idle":
            self.on_idle(agent)
        elif kind == "session.error":
            if agent.sticky != "aborted":
                agent.sticky = "error"
                error = props.get("error") or {}
                self.note(agent, f"error-{self.clock()}", f"error: {(error.get('data') or {}).get('message') or error.get('name') or error}"[:80], "bad")
            self.on_idle(agent, "session.error")  # an error ends the turn; the idle that usually follows is a duplicate

    def on_busy(self, agent):
        if agent.awaiting_busy or agent.base != "working":
            agent.awaiting_busy = False
            if agent.base != "retry":
                agent.working_since = self.clock()
            agent.base = "working"
            self.refresh(agent, "busy")

    def on_idle(self, agent, reason="idle"):
        if agent.awaiting_busy or agent.base == "idle":
            if agent.sticky and agent.history and agent.history[-1][1] != agent.state:
                self.refresh(agent, reason)
            return  # stale idle from before our prompt reached OpenCode, or a duplicate
        agent.base = "idle"
        agent.idles += 1
        agent.working_since = None
        self.refresh(agent, reason)
        self.save()

    def on_request(self, agent, kind, props, child):
        request_id = props.get("id")
        if not request_id or request_id in agent.pending:
            return
        description = describe(props) if kind == "permission" else "question: " + "; ".join(
            q.get("question", "") for q in props.get("questions") or [])[:80]
        if kind == "permission":
            action, message, rule = self.policy.decide(props, agent=agent.name)
        else:
            action, message, rule = ("deny", "dismissed", None) if self.questions == "reject" else ("ask", "", None)
        agent.pending[request_id] = {"kind": kind, "request": props, "child": child, "description": description,
                                     "asked": self.clock(), "action": action}
        self.note(agent, request_id, f"{'child ' if child else ''}asks: {description[:56]}", "warn")
        self.record({"hub": "asked", "agent": agent.name, "request": request_id, "kind": kind, "child": child,
                     "description": description, "policy": action, "rule": rule})
        self.emit({"type": "permission.asked", "agent": agent.name, "request": request_id, "kind": kind, "child": child,
                   "description": description, "policy": action})
        self.refresh(agent, f"{kind}.asked")
        if action != "ask":
            reply = REPLIES[action] if kind == "permission" else "reject"
            self.run(lambda: self.safe_reply(request_id, reply, message, f"policy rule {rule}" if rule else "policy default"))

    def safe_reply(self, request_id, reply, message, by):
        try:
            self.reply(request_id, reply, message, by=by)
        except Exception as exc:
            self.record({"hub": "reply_error", "request": request_id, "error": str(exc)})
            with self.lock:
                agent, _ = self.find_pending(request_id)
                if agent:
                    self.note(agent, f"reply-error-{request_id}", f"could not answer {request_id}: {exc}"[:80], "bad")
                    self.refresh(agent, "reply error")

    # ---------------------------------------------------------------- reconnect / restart
    def on_stream_state(self, state, detail):
        with self.lock:
            self.connected = state == "connected"
            self.record({"hub": "stream", "state": state, "detail": detail})
            self.emit({"type": "server.stream", "state": state, "detail": detail})
        if state == "connected":
            self.run(self.resync)

    def resync(self):
        """Rebuild what may have been missed while the event stream was down: session states and pending requests."""
        try:
            statuses = self.client.statuses()
            permissions = self.client.pending_permissions()
            questions = self.client.pending_questions()
        except Exception as exc:
            self.record({"hub": "resync_error", "error": str(exc)})
            return
        with self.lock:
            for agent in self.agents.values():
                status = (statuses.get(agent.session_id) or {}).get("type", "idle")
                if status == "busy" and agent.base != "working":
                    self.on_busy(agent)
                elif status == "idle" and agent.base in ("working", "retry") and not agent.awaiting_busy:
                    self.on_idle(agent)
            live = {p["id"] for p in permissions} | {q["id"] for q in questions}
            for agent in self.agents.values():
                for request_id in [r for r in agent.pending if r not in live]:
                    agent.pending.pop(request_id)
                    self.refresh(agent, "resync: request gone")
        for kind, items in (("permission.asked", permissions), ("question.asked", questions)):
            for item in items:
                self.on_event({"type": kind, "properties": item})

    def load(self):
        """Re-attach to the sessions in the state file (after a daemon restart)."""
        if not self.state_path or not os.path.exists(self.state_path):
            return 0
        with open(self.state_path, encoding="utf-8") as handle:
            data = json.load(handle)
        with self.lock:
            for item in data.get("agents", []):
                agent = Agent(item["name"], item["session_id"], item.get("model"), item.get("budget_s"),
                              item.get("followups") or [], item.get("created"))
                agent.turns, agent.idles = item.get("turns", 0), item.get("idles", 0)
                agent.base = "idle"
                agent.children = set(item.get("children") or [])
                agent.past_sessions = list(item.get("past_sessions") or [])
                self.agents[agent.name] = agent
                self.by_session[agent.session_id] = agent.name
                for child in agent.children:
                    self.child_root[child] = agent.session_id
                self.note(agent, "reattached", "re-attached after restart", "info")
                self.refresh(agent, "re-attached")
        return len(data.get("agents", []))
