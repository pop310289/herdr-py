"""Flight recorder and diagnosis of a team run: what went wrong with a member, and what to do about it.

Reads a run folder as examples/slide_team/run_demo.py leaves it (or the state or work folder itself):
  state/events.jsonl        the daemon's log (Hub.record): hub records and raw OpenCode events (OpenCode members)
  work/codex/events.jsonl   codex_agents.py: an argv record per turn, then Codex's own events (Codex members)
  work/chat.jsonl           who said what to whom; program checks come from check, build, lint, content and picture

turns(run_dir): one record per member turn (prompt head, reply, reasoning length, tool calls, tokens, finish reason,
duration, compactions, errors, the program checks that followed). findings(run_dir): the rules below over those records,
a list of dicts, each with evidence (member, turn, time, file:line of the record that proves it) and one suggestion.
A turn is everything from a prompt to the same member's next prompt; a chat message belongs to the turn that ended
last before it (the example teams run one member at a time).

usage: python3 -m herdr_py.diagnose RUN_DIR [--member NAME [--turn N]] [--json]
"""
import argparse
import bisect
import collections
import json
import os
import re
import sys
import time

from .policy import target_of

HEAD = 300                 # characters kept of a prompt, a tool input or output, an error
REPEAT = 3                 # the same tool error or permission rejection this often for one member is a pattern
REVISIONS = 2              # rejected revisions with none accepted before it counts as "no progress"
FINAL = ("idle", "aborted", "error")
ABORTED = "MessageAbortedError"  # OpenCode's error after herdr-py aborts a turn: a stop, not a failure
CHANGE_TOOLS = ("write", "edit", "patch", "multiedit", "apply_patch", "file_change")
CHANGE_ASKED = re.compile(r"(?i)\bwith the (write|edit) tool\b|\b(write|edit|rewrite|update|modify|create)\b[^\n.]{0,60}?"
                          r"[\w-]+\.(py|md|json|txt|toml|ya?ml|js|ts|html|css|sh)\b")
TIME_LIMIT = re.compile(r"stopped: over the (\d+)s turn limit")
TIME_ABORT = re.compile(r"time limit|time budget")
ROUND = re.compile(r"^round (\d+): row (\d+) (draft|revise)\b")
PICTURE = re.compile(r"^row (\d+): match ([\d.]+)(?: -> ([\d.]+))?, (\d+) labels? missing: (.*)")

# The program's messages in chat.jsonl: sender -> (what it checks, failed when the text matches, passed when it matches).
CHECKS = collections.OrderedDict([
    ("check", ("valid", r"^\d+ errors?\b", r"^OK\b")),
    ("build", ("valid", r"stopped with an error|did not finish|could not run|did not write", r"\bran\b")),
    ("lint", ("valid", r"^(?!OK\b|\d+ warning)", r"^OK\b|^\d+ warning")),
    ("content", ("quality", r"^\d+ missing\b", r"labels present")),
    ("picture", ("quality", r"\b[1-9]\d* labels? missing|\brejected\b", r"\b0 labels missing\b")),
])


def has_json(text):
    """A ```json block, or a bare [...] or {...}, that parses (what components.parse_reply accepts, objects too)."""
    for chunk in re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.S) + [text]:
        for opening, closing in ("[]", "{}"):
            start, end = chunk.find(opening), chunk.rfind(closing)
            if start != -1 and end > start:
                try:
                    json.loads(chunk[start:end + 1])
                    return True
                except ValueError:
                    pass
    return False


# Answer formats a prompt can ask for: (name, asked when the prompt matches, present in a reply,
# a program message that says the reply lacks it). Codex logs keep no prompt: there only the program's word counts.
FORMATS = (
    ("a ```json block", r"(?i)\b(reply|answer|respond)\b[^\n]{0,40}```json|```json block|\bjson (block|list)\b", has_json,
     r"no ```json block"),
    ("DIFF lines or SAME", r"(?m)^DIFF:", lambda text: bool(re.search(r"(?im)^\s*DIFF\b|\bSAME\b", text)), r"^\(no DIFF lines\)"),
    ("a SCORE: n/10 line", r"SCORE: n/10", lambda text: bool(re.search(r"SCORE:\s*\d+(\.\d+)?\s*/\s*10", text)), None),
    ("a VERDICT line", r"VERDICT: ACCEPT or VERDICT: REJECT", lambda text: bool(re.search(r"(?i)VERDICT:\s*(ACCEPT|REJECT)", text)),
     None),
)

# A member's own words that claim success (prose only, code blocks cut) and the checks that can prove them wrong.
CLAIMS = (
    (r"\b(all|every)\b[^.]*\blabels?\b[^.]*\b(added|present|included|in place)\b", ("content", "picture")),
    (r"\b(validation|validate[sd]?|lint|build|checks?|tests?)\b[^.]*\b(pass\w*|succeed\w*|ok|clean|without errors|no errors)\b",
     ("check", "build", "lint")),
    (r"\b(done|complete[ds]?|finished|successfully|fixed|all set)\b", tuple(CHECKS)),
)
NEGATION = re.compile(r"(?i)\b(not|no longer|never|cannot|can't|couldn't|could not|unable|fail\w*|still|missing)\b|n't\b")

# Known tool errors and what to do about them: (tool, error, suggestion); the last line catches the rest.
TOOL_FIXES = (
    (r"edit", r"(?i)could not find oldString|oldString not found|found multiple matches",
     "Rewrite whole files with the write tool instead of exact-match edits: say so in the prompt, or deny edit in the policy."),
    (r".*", r".*", "Put this error and how to avoid it at the top of the member's next prompt; if it keeps coming, change the tool set-up."),
)
REJECTED = re.compile(r"(?i)rejected permission|permission (was )?(denied|rejected)")


def hms(t):
    return time.strftime("%H:%M:%S", time.localtime(t)) if t else "-"


def head(text, n=HEAD):
    text = "" if text is None else str(text)
    return text if len(text) <= n else text[:n] + "..."


def one_line(text, n=90):
    return head(" ".join(str(text or "").split()), n)


def iter_jsonl(path, stats):
    """(line number, dict) for each readable line; unreadable ones (a half-written last line) are counted in stats."""
    with open(path, encoding="utf-8", errors="replace") as handle:
        for n, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                row = None
            if isinstance(row, dict):
                stats["lines"] += 1
                yield n, row
            else:
                stats["bad"] += 1


def locate(run_dir):
    """The logs of a run folder; a state or work folder given directly works too. Missing ones are None."""
    def first(*options):
        for parts in options:
            path = os.path.join(run_dir, *parts)
            if os.path.isfile(path):
                return path
        return None
    return collections.OrderedDict([("events", first(("state", "events.jsonl"), ("events.jsonl",))),
                                    ("codex", first(("work", "codex", "events.jsonl"), ("codex", "events.jsonl"))),
                                    ("chat", first(("work", "chat.jsonl"), ("chat.jsonl",)))])


def summary_of(inp):
    """A tool call's input in one line: the command, the file, or the arguments."""
    if not isinstance(inp, dict):
        return one_line(inp, 160)
    for key in ("command", "filePath", "path", "pattern", "url", "query", "description"):
        if inp.get(key):
            extra = f" (oldString {len(inp['oldString'])} chars)" if key == "filePath" and inp.get("oldString") else ""
            return one_line(inp[key], 160) + extra
    return one_line(json.dumps(inp, ensure_ascii=False), 160)


def shape(command):
    """'python3 make_deck.py && python3 -m x validate deck.json' -> 'python3 ... && python3 ...': programs and operators."""
    out = []
    for piece in re.split(r"\s*(&&|\|\||;|\|)\s*", str(command).strip()):
        if piece in ("&&", "||", ";", "|"):
            out.append(piece)
        elif piece:
            words = piece.split()
            out.append(os.path.basename(words[0]) + (" ..." if len(words) > 1 else ""))
    return " ".join(out)


def signature(error):
    """An error's first line with the varying parts (quotes, paths, numbers) blanked, so repeats group together."""
    text = str(error or "").strip().splitlines()[0] if str(error or "").strip() else ""
    text = re.sub(r"'[^']*'|\"[^\"]*\"|`[^`]*`", "'_'", text)
    text = re.sub(r"(/[\w.-]+)+", "PATH", text)
    return re.sub(r"\d+", "N", text)[:120]


class Turn:
    """One prompt to one member and everything recorded until that member's next prompt."""

    def __init__(self, member, backend, t, ref):
        self.member, self.backend, self.start, self.ref = member, backend, t, ref
        self.number, self.session, self.last = 0, None, t
        self.prompt, self.source, self.files = None, None, []
        self.end = self.end_state = self.end_ref = None
        self.texts = collections.OrderedDict()       # part or item id -> {"message", "text", "t", "ref", "child"}
        self.reasoning = collections.OrderedDict()   # part or item id -> characters
        self.tools = collections.OrderedDict()       # call or item id -> {"tool", "status", "input", "output", ...}
        self.steps = collections.OrderedDict()       # step-finish part id -> {"reason", "in", "out", "reasoning", ...}
        self.usage = None                            # Codex turn.completed usage
        self.compacted, self.compaction_parts = [], collections.OrderedDict()
        self.errors, self.aborts, self.permissions = [], [], collections.OrderedDict()
        self.exit = None
        self.chat, self.checks = [], []
        self.reply, self.reply_ref, self.reply_summary = "", None, False

    # ---- derived facts (filled in by close)
    def close(self, roles, summaries):
        replies = [p for p in self.texts.values() if p["text"].strip() and not p["child"]
                   and roles.get(p["message"], "assistant") == "assistant" and p["text"] != self.prompt]
        if replies:
            self.reply, self.reply_ref = replies[-1]["text"], replies[-1]["ref"]
            self.reply_summary = replies[-1]["message"] in summaries
        self.compactions = self.compacted or list(self.compaction_parts.values())
        steps = list(self.steps.values())
        self.tokens_in = sum(s["in"] for s in steps) if self.usage is None else int(self.usage.get("input_tokens") or 0)
        self.tokens_out = sum(s["out"] for s in steps) if self.usage is None else int(self.usage.get("output_tokens") or 0)
        self.reasoning_tokens = (sum(s["reasoning"] for s in steps) if self.usage is None
                                 else int(self.usage.get("reasoning_output_tokens") or 0))
        root = [s for s in steps if not s["child"]]
        self.last_step = root[-1] if root else None
        self.finish = root[-1]["reason"] if root else ("completed" if self.usage is not None else None)
        self.length_step = next((s for s in root if s["reason"] == "length"), None)

    @property
    def duration(self):
        return round(self.end - self.start, 1) if self.end is not None else None

    def change_attempts(self):
        return [c for c in self.tools.values() if c["tool"] in CHANGE_TOOLS] + \
               [p for p in self.permissions.values() if p["permission"] in ("edit", "write")]

    def record(self):
        """The flight recorder's view of this turn, JSON-ready."""
        return collections.OrderedDict([
            ("member", self.member), ("turn", self.number), ("backend", self.backend), ("session", self.session),
            ("start", self.start), ("end", self.end), ("duration_s", self.duration), ("end_state", self.end_state),
            ("prompt_head", head(self.prompt)), ("prompt_chars", None if self.prompt is None else len(self.prompt)),
            ("prompt_source", self.source), ("files", self.files),
            ("reply", self.reply), ("reply_is_compaction_summary", self.reply_summary),
            ("reasoning_chars", sum(self.reasoning.values())), ("reasoning_tokens", self.reasoning_tokens),
            ("tools", [collections.OrderedDict([("tool", c["tool"]), ("status", c["status"]), ("input", summary_of(c["input"])),
                                                ("output", head(c.get("error") or c.get("output"))), ("t", c["t"]),
                                                ("child", c["child"]), ("ref", c["ref"])]) for c in self.tools.values()]),
            ("tokens", {"in": self.tokens_in, "out": self.tokens_out}), ("finish", self.finish),
            ("compactions", [c["t"] for c in self.compactions]),
            ("errors", [collections.OrderedDict([("t", e["t"]), ("name", e["name"]), ("message", head(e["message"])),
                                                 ("ref", e["ref"])]) for e in self.errors]),
            ("aborts", [{"t": a["t"], "reason": a["reason"]} for a in self.aborts]),
            ("permissions", [collections.OrderedDict([("permission", p["permission"]), ("target", p["target"]),
                                                      ("reply", p.get("reply")), ("by", p.get("by")), ("t", p["t"])])
                             for p in self.permissions.values()]),
            ("checks", [collections.OrderedDict([("t", m["t"]), ("from", m["from"]), ("text", m["text"]), ("ok", m["ok"]),
                                                 ("ref", m["ref"])]) for m in self.checks]),
            ("ref", self.ref)])


class Recorder:
    """Builds Turn records from the daemon's log (OpenCode members) and from codex_agents.py's log (Codex members)."""

    def __init__(self):
        self.turns, self.current = [], {}            # (backend, member) -> the open turn
        self.owner, self.parent = {}, {}             # OpenCode session -> member; subagent session -> parent session
        self.requests, self.roles, self.summaries = {}, {}, set()
        self.unattributed = 0

    def feed(self, rel, n, row):
        ref, t = f"{rel}:{n}", row.get("t") or 0
        if "hub" in row:
            self.hub(ref, t, row)
        elif isinstance(row.get("opencode"), dict):
            self.opencode(ref, t, row["opencode"])
        elif row.get("agent") and ("argv" in row or "event" in row or "exit" in row):
            self.codex(ref, t, row)

    def open(self, member, backend, t, ref):
        turn = Turn(member, backend, t, ref)
        self.turns.append(turn)
        self.current[(backend, member)] = turn
        return turn

    # ---- OpenCode (state/events.jsonl)
    def hub(self, ref, t, row):
        kind, member = row.get("hub"), row.get("agent")
        if kind == "start":
            self.owner[row.get("session")] = member
            return
        if kind == "prompt":
            turn = self.open(member, "opencode", t, ref)
            turn.prompt, turn.source = row.get("text") or "", row.get("source")
            turn.session = next((s for s, m in reversed(list(self.owner.items())) if m == member), None)
            return
        turn = self.current.get(("opencode", member))
        if turn is None:
            return
        turn.last = max(turn.last, t)
        if kind == "state" and row.get("state") in FINAL and turn.end is None:
            turn.end, turn.end_state, turn.end_ref = t, row["state"], ref
        elif kind == "abort":
            turn.aborts.append({"t": t, "reason": row.get("reason") or "", "ref": ref})
        elif kind == "asked" and row.get("kind") == "permission":
            request = self.requests.get(row.get("request")) or {}
            description = row.get("description") or ""
            target = target_of(request) if request else re.sub(r"^run ", "", description)
            turn.permissions[row.get("request")] = {"t": t, "ref": ref, "target": target, "child": row.get("child"),
                                                    "permission": request.get("permission") or ("bash" if description.startswith("run ") else "?")}
        elif kind == "decision" and row.get("request") in turn.permissions:
            turn.permissions[row["request"]].update(reply=row.get("reply"), by=row.get("by"), decided=t, decided_ref=ref)
        elif kind in ("automation_error", "reply_error"):
            turn.errors.append({"t": t, "ref": ref, "name": kind, "message": row.get("error") or ""})

    def member_of(self, session):
        child, seen = False, 0
        while session and seen < 10:
            if session in self.owner:
                return self.owner[session], child
            session, child, seen = self.parent.get(session), True, seen + 1
        return None, False

    def opencode(self, ref, t, event):
        kind, props = event.get("type") or "", event.get("properties") or {}
        part, info = props.get("part") or {}, props.get("info") or {}
        session = props.get("sessionID") or part.get("sessionID") or info.get("sessionID") or info.get("id")
        if kind in ("session.created", "session.updated") and info.get("id") and info.get("parentID"):
            self.parent[info["id"]] = info["parentID"]
        elif kind == "permission.asked":
            self.requests[props.get("id")] = props
        elif kind == "message.updated" and info.get("id"):
            self.roles[info["id"]] = info.get("role")
            if info.get("summary") is True or info.get("mode") == "compaction":
                self.summaries.add(info["id"])
        member, child = self.member_of(session)
        turn = self.current.get(("opencode", member))
        if turn is None:  # a session no hub record names (counted), or a member's events before its first prompt
            self.unattributed += member is None
            return
        turn.last = max(turn.last, t)
        if kind == "message.part.updated":
            self.part(turn, ref, t, part, child)
        elif kind == "message.updated" and info.get("role") == "assistant" and info.get("error"):
            self.error(turn, ref, t, info["error"])
        elif kind == "session.error":
            self.error(turn, ref, t, props.get("error") or {})
        elif kind == "session.compacted":
            turn.compacted.append({"t": t, "ref": ref})

    def error(self, turn, ref, t, error):
        name = error.get("name") or "error"
        message = (error.get("data") or {}).get("message") or error.get("message") or ""
        if name != ABORTED and not any(e["name"] == name and e["message"] == message for e in turn.errors):
            turn.errors.append({"t": t, "ref": ref, "name": name, "message": message})

    def part(self, turn, ref, t, part, child):
        kind = part.get("type")
        key = part.get("id") or f"{part.get('messageID')}:{kind}"
        if kind == "text":
            turn.texts[key] = {"message": part.get("messageID"), "text": part.get("text") or "", "t": t, "ref": ref, "child": child}
        elif kind == "reasoning":
            turn.reasoning[key] = len(part.get("text") or "")
        elif kind == "tool":
            state = part.get("state") or {}
            call = turn.tools.setdefault(part.get("callID") or key, {"tool": part.get("tool"), "t": t, "child": child, "input": {}})
            call.update(status=state.get("status"), input=state.get("input") or call["input"], output=state.get("output"),
                        error=state.get("error"), ref=ref)
        elif kind == "step-finish":
            tokens = part.get("tokens") or {}
            turn.steps[key] = {"reason": part.get("reason"), "in": int(tokens.get("input") or 0), "out": int(tokens.get("output") or 0),
                               "reasoning": int(tokens.get("reasoning") or 0), "t": t, "ref": ref, "child": child}
        elif kind == "compaction":
            turn.compaction_parts.setdefault(key, {"t": t, "ref": ref})
        elif kind == "file" and part.get("filename") and part.get("filename") not in turn.files:
            turn.files.append(part["filename"])

    # ---- Codex (work/codex/events.jsonl)
    def codex(self, ref, t, row):
        member = row["agent"]
        if "argv" in row:
            turn = self.open(member, "codex", t, ref)
            argv = [str(a) for a in row.get("argv") or []]
            turn.files = [os.path.basename(argv[i + 1]) for i, a in enumerate(argv[:-1]) if a in ("-i", "--image")]
            turn.session = argv[argv.index("resume") + 1] if "resume" in argv[:-1] else None
            return
        turn = self.current.get(("codex", member))
        if turn is None:
            self.unattributed += 1
            return
        turn.last = max(turn.last, t)
        if "exit" in row:
            turn.exit = row.get("exit")
            code = turn.exit if isinstance(turn.exit, int) else 0
            turn.end, turn.end_ref = t, ref
            turn.end_state = "aborted" if code < 0 else "error"
            if code >= 0:
                stderr = [l for l in str(row.get("stderr") or "").splitlines() if l.strip()]
                turn.errors.append({"t": t, "ref": ref, "name": f"exit {turn.exit}", "message": stderr[-1] if stderr else ""})
            return
        event = row.get("event") or {}
        kind, item = event.get("type"), event.get("item") or {}
        if kind == "thread.started":
            turn.session = event.get("thread_id") or turn.session
        elif kind in ("item.started", "item.updated", "item.completed") and item:
            self.item(turn, ref, t, kind, item)
        elif kind == "turn.completed":
            turn.usage = event.get("usage") or {}
            turn.end, turn.end_state, turn.end_ref = t, turn.end_state or "idle", ref
        elif kind in ("error", "turn.failed"):
            error = event.get("error") or {}
            turn.errors.append({"t": t, "ref": ref, "name": kind, "message": error.get("message") or event.get("message") or ""})
            turn.end, turn.end_state, turn.end_ref = t, "error", ref

    def item(self, turn, ref, t, kind, item):
        key, itype = item.get("id") or str(len(turn.tools) + len(turn.texts)), item.get("type")
        if itype == "agent_message" and kind == "item.completed":
            turn.texts[key] = {"message": None, "text": item.get("text") or "", "t": t, "ref": ref, "child": False}
        elif itype == "reasoning":
            turn.reasoning[key] = len(item.get("text") or "")
        elif itype == "error":
            turn.errors.append({"t": t, "ref": ref, "name": "error item", "message": item.get("message") or ""})
        elif itype in ("command_execution", "file_change", "mcp_tool_call", "web_search"):
            status = {"in_progress": "running", "failed": "error"}.get(item.get("status"), item.get("status") or "completed")
            if itype == "command_execution":
                tool, inp, out = "bash", {"command": item.get("command")}, item.get("aggregated_output")
            elif itype == "file_change":
                tool, out = "file_change", None
                inp = {"path": ", ".join(f"{c.get('kind')} {c.get('path')}" for c in item.get("changes") or [])}
            elif itype == "mcp_tool_call":
                tool, inp, out = f"{item.get('server')}.{item.get('tool')}", item.get("arguments") or {}, item.get("result")
            else:
                tool, inp, out = "web_search", {"query": item.get("query")}, None
            error = None
            if status == "error":
                lines = str(out or "").strip().splitlines()
                error = item.get("error") or f"exit {item.get('exit_code')}: " + one_line(lines[-1] if lines else "", 200)
            call = turn.tools.setdefault(key, {"tool": tool, "t": t, "child": False, "input": inp})
            call.update(status=status, input=inp, output=out if isinstance(out, str) else (json.dumps(out) if out else None),
                        error=error, ref=ref)
            if itype == "command_execution" and status == "declined":
                turn.permissions[key] = {"t": t, "ref": ref, "target": item.get("command") or "", "permission": "bash",
                                         "reply": "reject", "by": "codex"}

    def close(self):
        numbers = collections.Counter()
        for turn in sorted(self.turns, key=lambda x: x.start):
            numbers[turn.member] += 1
            turn.number = numbers[turn.member]
            turn.close(self.roles, self.summaries)
        return sorted(self.turns, key=lambda x: x.start)


# ---------------------------------------------------------------- reading a run
def read_chat(path, rel):
    stats = collections.Counter()
    messages = []
    for n, row in iter_jsonl(path, stats):
        sender = str(row.get("from") or "")
        message = {"t": row.get("t") or 0, "from": sender, "to": str(row.get("to") or ""), "kind": row.get("kind"),
                   "text": str(row.get("text") or ""), "ref": f"{rel}:{n}", "ok": None}
        if sender in CHECKS:
            _, failed, passed = CHECKS[sender]
            message["ok"] = False if re.search(failed, message["text"]) else (True if re.search(passed, message["text"]) else None)
        messages.append(message)
    return messages, stats


def load(run_dir):
    """Everything diagnose needs from a run folder: turns, chat, notes about what could not be read."""
    paths, notes = locate(run_dir), []
    recorder = Recorder()
    state = os.path.join(os.path.dirname(paths["events"]), "state.json") if paths["events"] else None
    if state and os.path.isfile(state):  # the daemon's agents: sessions a restarted daemon re-attached have no start record
        try:
            with open(state, encoding="utf-8") as handle:
                for agent in json.load(handle).get("agents") or []:
                    for session in [agent.get("session_id")] + list(agent.get("past_sessions") or []):
                        recorder.owner.setdefault(session, agent.get("name"))
        except (OSError, ValueError, AttributeError) as exc:
            notes.append(f"{os.path.relpath(state, run_dir)} could not be read: {exc}")
    for key in ("events", "codex"):
        if paths[key]:
            rel, stats = os.path.relpath(paths[key], run_dir), collections.Counter()
            for n, row in iter_jsonl(paths[key], stats):
                recorder.feed(rel, n, row)
            if stats["bad"]:
                notes.append(f"{rel}: {stats['bad']} unreadable line(s) skipped")
    if recorder.unattributed:
        notes.append(f"{recorder.unattributed} event(s) of sessions or members no start or argv record names were left out")
    turns = recorder.close()
    chat = []
    if paths["chat"]:
        rel = os.path.relpath(paths["chat"], run_dir)
        chat, stats = read_chat(paths["chat"], rel)
        if stats["bad"]:
            notes.append(f"{rel}: {stats['bad']} unreadable line(s) skipped")
    attach(turns, chat)
    if not paths["events"] and not paths["codex"]:
        notes.append("no member log (state/events.jsonl or work/codex/events.jsonl): only the chat-based rules ran")
    for turn in turns:
        if turn.end is None:
            notes.append(f"{turn.member} turn {turn.number} has no end in the log (still running, or the log was cut)")
    return {"paths": paths, "turns": turns, "chat": chat, "notes": notes}


def attach(turns, chat):
    """A chat message goes to the turn that most recently ended at or before it: the program writes a turn's checks
    after the turn ends, and the next prompt can carry the same rounded time (chat keeps 2 decimals, the daemon 3).
    The supervisor's words before a Codex turn become its prompt head (Codex logs keep no prompt)."""
    ended = sorted(turns, key=lambda x: x.end if x.end is not None else x.last)
    ends = [x.end if x.end is not None else x.last for x in ended]
    for message in chat:
        i = bisect.bisect_right(ends, message["t"] + 0.01) - 1
        if i >= 0:
            ended[i].chat.append(message)
            if message["ok"] is not None:
                ended[i].checks.append(message)
    for k, turn in enumerate(turns):
        if turn.prompt is None:  # Codex: the log keeps no prompt; say what the supervisor said it asked for
            earlier = [t.start for t in turns[:k] if t.member == turn.member]
            since = earlier[-1] if earlier else float("-inf")
            asked = [m for m in chat if m["to"] == turn.member and since < m["t"] <= turn.start + 0.01 and m["from"] != turn.member]
            if asked:
                turn.prompt, turn.source = asked[-1]["text"], "chat (" + asked[-1]["from"] + ")"


def turns(run_dir):
    """Flight recorder: one JSON-ready record per member turn, in time order."""
    return [t.record() for t in load(run_dir)["turns"]]


# ---------------------------------------------------------------- rules
def asked_format(turn):
    if turn.prompt and turn.backend != "codex":
        for name, asked, present, _ in FORMATS:
            if re.search(asked, turn.prompt):
                return name, present
    return None, None


def format_missing(turn):
    """(format name, records that prove it, detail) when the reply lacks the format the prompt asked for, or the program
    said so; (None, None, None) otherwise."""
    said = next(((name, m) for name, _, _, pattern in FORMATS if pattern for m in turn.chat
                 if (m["from"] in CHECKS or m["from"] == turn.member) and re.search(pattern, m["text"])), (None, None))
    name, present = asked_format(turn)
    if name and not present(turn.reply):
        refs = [turn.ref, turn.reply_ref or turn.end_ref] + ([said[1]["ref"]] if said[0] == name else [])
        return name, refs, f"asked for {name}; the reply ({len(turn.reply)} chars) has none: {one_line(turn.reply, 80)!r}"
    if said[0] and not name:  # no prompt in the log (Codex): the program's word
        return said[0], [said[1]["ref"]], f"{said[1]['from']}: {one_line(said[1]['text'], 80)!r}"
    return None, None, None


def turn_error(turn):
    if turn.errors:
        e = turn.errors[0]
        return [e["ref"]], f"{e['name']}: {one_line(e['message'], 160)}"


def compacted_reply(turn):
    if turn.compactions:
        name, _, _ = format_missing(turn)
        if turn.reply_summary or name:
            what = "the compaction summary" if turn.reply_summary else f"not {name}"
            return [turn.compactions[0]["ref"], turn.reply_ref], (f"compacted at {hms(turn.compactions[0]['t'])}; the reply after "
                                                                  f"it is {what}: {one_line(turn.reply, 80)!r}")


def output_limit(turn):
    if turn.length_step and not turn.reply.strip():
        return [turn.length_step["ref"]], (f"step-finish reason length after {turn.length_step['out']:,} output tokens; "
                                           f"{sum(turn.reasoning.values()):,} chars of reasoning, no text")


def reasoning_only(turn):
    chars = sum(turn.reasoning.values())
    thought = chars or turn.reasoning_tokens
    if turn.end_state == "idle" and thought and not turn.reply.strip() and not turn.tools and not turn.length_step:
        return [turn.last_step["ref"] if turn.last_step else None, turn.end_ref], (
            f"ended idle (finish {turn.finish}) after {thought:,} {'chars' if chars else 'tokens'} of reasoning; no text, no tool call")


def time_limit(turn):
    said = next((m for m in turn.chat if m["to"] == turn.member and TIME_LIMIT.search(m["text"])), None)
    abort = next((a for a in turn.aborts if TIME_ABORT.search(a["reason"])), None)
    if said or abort:
        return [abort and abort["ref"], said and said["ref"]], (f"{said['from']}: {said['text']}" if said else
                                                                f"aborted: {abort['reason']} after {abort['t'] - turn.start:.0f} s")
    if turn.backend == "codex" and isinstance(turn.exit, int) and turn.exit < 0:
        return [turn.end_ref], f"killed (exit {turn.exit}) after {turn.last - turn.start:.0f} s: codex_agents.py kills a turn only at its time limit"


def wrong_format(turn):
    name, refs, detail = format_missing(turn)
    if name:
        return refs, detail


def no_change(turn):
    asked = turn.prompt and turn.backend != "codex" and CHANGE_ASKED.search(turn.prompt)
    if turn.end_state == "idle" and asked and not turn.change_attempts():
        used = collections.Counter(c["tool"] for c in turn.tools.values())
        tools = ", ".join(f"{k} x{v}" for k, v in used.items()) or "no tool calls"
        nudge = next((m for m in turn.chat if m["from"] == "supervisor" and "unchanged" in m["text"]), None)
        return [turn.ref, turn.end_ref, nudge and nudge["ref"]], (
            f"ended idle after {tools}; no write or edit (asked: {one_line(asked.group(0), 60)!r})"
            + (f"; supervisor: {one_line(nudge['text'], 60)!r}" if nudge else ""))


# Turn outcomes: why a turn gave nothing usable. One per turn, the first that applies (the most specific cause first).
OUTCOMES = (
    ("turn_error", turn_error, "the turn failed with an error",
     "Fix the error shown before the next run: the member did no work in this turn."),
    ("compacted_reply", compacted_reply, "the session was compacted during the turn and the reply after it is not the asked format",
     "Start every turn in a fresh session (agent.start with fresh=true; layout_team.py --sessions fresh) and put everything "
     "the turn needs into its prompt."),
    ("output_limit", output_limit, "output stopped at the token limit with no text",
     "Raise the model's output limit (limit.output in the OpenCode model config) or ask for a shorter answer."),
    ("reasoning_only", reasoning_only, "the turn ended with reasoning only: no text and no tool call",
     "Send it back once with the answer format restated ('reply now with ...'); if it repeats, turn the model's thinking down or off."),
    ("time_limit", time_limit, "stopped at the turn time limit",
     "Give the member a smaller piece of work per turn, or raise the turn limit if its output was nearly done."),
    ("wrong_format", wrong_format, "reply without the asked format",
     "End the prompt with the exact format and a one-line example; when a reply lacks it, send it back once with the reason."),
    ("no_change", no_change, "stopped without changing anything",
     "Send it back with the reason ('make_deck.py is unchanged'), as slide_team.py's nudge does, and ask for the whole file "
     "to be rewritten with the write tool."),
)


def claims_of(text):
    """Sentences of the member's prose that claim success: (sentence, the check senders that can prove it wrong).
    Code blocks and markdown headings ('### Completed') are not claims."""
    prose = re.sub(r"```.*?(```|$)", " ", text, flags=re.S)
    prose = re.sub(r"(?m)^\s*#+ .*$", " ", prose)
    out = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", prose):
        if NEGATION.search(sentence):
            continue
        for pattern, senders in CLAIMS:
            if re.search(pattern, sentence, flags=re.I):
                out.append((sentence.strip(), senders))
                break
    return out


def claimed_success(turn):
    if turn.reply_summary:  # OpenCode's compaction agent wrote it, not the member
        return None
    for sentence, senders in claims_of(turn.reply):
        failed = next((m for m in turn.checks if m["ok"] is False and m["from"] in senders), None)
        if failed:
            return [turn.reply_ref or turn.ref, failed["ref"]], (f"said {one_line(sentence, 80)!r}; {failed['from']} said "
                                                                  f"{one_line(failed['text'], 90)!r}")


def evidence(turn, refs, detail):
    refs = refs if isinstance(refs, list) else [refs]
    return collections.OrderedDict([("member", turn.member), ("turn", turn.number), ("t", turn.start),
                                    ("refs", [r for r in refs if r]), ("detail", detail)])


def finding(code, member, title, items, suggestion, row=None):
    times = [e["t"] for e in items]
    return collections.OrderedDict([("code", code), ("member", member), ("row", row), ("title", title), ("count", len(items)),
                                    ("turns", [e["turn"] for e in items]), ("first", min(times) if times else None),
                                    ("evidence", items), ("suggestion", suggestion)])


def turn_findings(turns):
    grouped = collections.OrderedDict()
    for turn in turns:
        if turn.end is None:  # still running or the log was cut: nothing to judge yet (load() notes it)
            continue
        for code, rule, title, suggestion in OUTCOMES:
            hit = rule(turn)
            if hit:
                grouped.setdefault((code, turn.member), (title, suggestion, []))[2].append(evidence(turn, *hit))
                break
        hit = claimed_success(turn)
        if hit:
            grouped.setdefault(("claimed_success", turn.member), (
                "claimed success while the program check of the same round failed",
                "Trust the program, not the member: decide from the check output, and quote it in the member's next prompt.",
                []))[2].append(evidence(turn, *hit))
    return [finding(code, member, title, items, suggestion) for (code, member), (title, suggestion, items) in grouped.items()]


def repeat_findings(turns):
    errors, rejections = collections.OrderedDict(), collections.OrderedDict()
    for turn in turns:
        for call in turn.tools.values():
            if call["status"] == "error" and call.get("error") and not REJECTED.search(str(call["error"])):
                errors.setdefault((turn.member, call["tool"], signature(call["error"])), []).append(evidence(
                    turn, call["ref"], f"{call['tool']} {summary_of(call['input'])[:60]}: {one_line(call['error'], 100)}"))
        for p in turn.permissions.values():
            if p.get("reply") == "reject":
                what = shape(p["target"]) if p["permission"] == "bash" else p["permission"]
                rejections.setdefault((turn.member, p["permission"], what), []).append(evidence(
                    turn, [p.get("decided_ref") or p["ref"]], f"{p['permission']} {one_line(p['target'], 100)} rejected by {p.get('by')}"))
    out = []
    for (member, tool, sig), items in errors.items():
        if len(items) >= REPEAT:
            fix = next(s for t, e, s in TOOL_FIXES if re.fullmatch(t, str(tool)) and re.search(e, items[0]["detail"]))
            out.append(finding("repeated_tool_error", member, f"the same {tool} error {len(items)} times: {one_line(sig, 80)}", items, fix))
    for (member, permission, what), items in rejections.items():
        if len(items) >= REPEAT:
            chained = any(op in what for op in ("&&", "||", ";", "|"))
            fix = (f"Allow `{what}` in the policy if it is safe, or tell the member to run one command per call (no `&&`, `;` or `|`)."
                   if chained else f"Allow `{what}` in the policy if it is safe, or tell the member which commands it may use.")
            out.append(finding("repeated_rejection", member, f"the same {permission} request rejected {len(items)} times: {what}",
                               items, fix))
    return out


def revisions(chat, turns):
    """Revisions the program judged (picture checks): who made them, the row, accepted or rejected, and the notes it got."""
    out, current = [], None
    by_member = collections.defaultdict(list)
    for turn in turns:
        by_member[turn.member].append(turn)
    for m in chat:
        found = ROUND.match(m["text"]) if m["from"] in ("manager", "supervisor") else None
        if found:
            notes = re.search(r"(\d+) art notes", m["text"])
            current = {"member": m["to"], "row": int(found.group(2)), "kind": found.group(3), "notes": int(notes.group(1)) if notes else None}
            continue
        found = PICTURE.match(m["text"]) if m["from"] == "picture" else None
        if found and current and found.group(3):  # "a -> b": a revision (a draft has one number)
            member = current["member"]
            mine = [t for t in by_member.get(member, []) if t.start <= m["t"] + 0.01]
            out.append({"member": member, "row": int(found.group(1)), "accepted": "accepted" in found.group(5),
                        "t": m["t"], "ref": m["ref"], "turn": mine[-1].number if mine else None, "notes": current["notes"],
                        "detail": f"row {found.group(1)}: match {found.group(2)} -> {found.group(3)}, {found.group(5)}"})
    return out


def revision_findings(chat, turns):
    items = revisions(chat, turns)
    out, used = [], set()

    def item_evidence(r):
        notes = "" if r["notes"] is None else f" ({r['notes']} art notes)"
        return collections.OrderedDict([("member", r["member"]), ("turn", r["turn"]), ("t", r["t"]), ("refs", [r["ref"]]),
                                        ("detail", r["detail"] + notes)])

    def blank(group):
        n = sum(1 for r in group if r["notes"] == 0)
        return f" {n} of these {len(group)} revisions came with no art notes." if n else ""

    for row in sorted({r["row"] for r in items}):
        group = [r for r in items if r["row"] == row]
        if len(group) >= REVISIONS and not any(r["accepted"] for r in group):
            used.update(id(r) for r in group)
            out.append(finding("revisions_rejected", None, f"all {len(group)} revisions rejected (no progress)",
                               [item_evidence(r) for r in group],
                               "Stop revising a row after one rejection (keep the first version), or change what the reviser is "
                               "told." + blank(group), row=row))
    for member in sorted({r["member"] for r in items}):
        mine = [r for r in items if r["member"] == member]
        rest = [r for r in mine if id(r) not in used]
        if len(rest) >= REVISIONS and not any(r["accepted"] for r in mine):
            others = sorted({r["member"] for r in items if r["accepted"] and r["member"] != member})
            who = f"give the revisions to {', '.join(others)}" if others else "give the revisions to another member or model"
            out.append(finding("revisions_rejected", member, f"all {len(mine)} of {member}'s revisions rejected (no progress)",
                               [item_evidence(r) for r in rest], f"Its revisions never beat the kept version: {who}, or stop "
                               "after one rejection." + blank(rest)))
    return out


def diagnose(run_dir):
    """{"turns": [records], "members": [table rows], "findings": [...], "notes": [...], "logs": {...}} for a run folder."""
    run = load(run_dir)
    found = turn_findings(run["turns"]) + repeat_findings(run["turns"]) + revision_findings(run["chat"], run["turns"])
    found.sort(key=lambda f: f["first"] if f["first"] is not None else float("inf"))
    return {"logs": {k: (os.path.relpath(v, run_dir) if v else None) for k, v in run["paths"].items()},
            "members": members(run["turns"], run["chat"]), "findings": found, "notes": run["notes"],
            "turns": run["turns"]}


def findings(run_dir):
    """What went wrong in a run: a list of JSON-ready dicts (code, member, row, title, count, turns, first, evidence,
    suggestion). For the dashboard."""
    return diagnose(run_dir)["findings"]


def valid(turn):
    """True / False when the program judged this turn's output (validity checks, else the asked format), None otherwise."""
    verdicts = [m["ok"] for m in turn.checks if CHECKS[m["from"]][0] == "valid" and m["ok"] is not None]
    if verdicts:
        return all(verdicts)
    name, present = asked_format(turn)
    if name:
        return bool(present(turn.reply))
    if format_missing(turn)[0]:
        return False
    return None


def members(turns, chat):
    revised = revisions(chat, turns)
    rows = collections.OrderedDict()
    for turn in turns:
        row = rows.setdefault(turn.member, collections.OrderedDict([
            ("member", turn.member), ("backend", turn.backend), ("turns", 0), ("tokens_in", 0), ("tokens_out", 0),
            ("valid", 0), ("judged", 0), ("revisions", 0), ("accepted", 0)]))
        row["turns"] += 1
        row["tokens_in"] += turn.tokens_in
        row["tokens_out"] += turn.tokens_out
        verdict = valid(turn)
        if verdict is not None:
            row["judged"] += 1
            row["valid"] += 1 if verdict else 0
    for r in revised:
        row = rows.setdefault(r["member"], collections.OrderedDict([
            ("member", r["member"]), ("backend", "-"), ("turns", 0), ("tokens_in", 0), ("tokens_out", 0), ("valid", 0),
            ("judged", 0), ("revisions", 0), ("accepted", 0)]))
        row["revisions"] += 1
        row["accepted"] += 1 if r["accepted"] else 0
    return list(rows.values())


# ---------------------------------------------------------------- output
def report(result, run_dir):
    lines = [f"run {run_dir}", "logs: " + (", ".join(v for v in result["logs"].values() if v) or "none found")]
    table = [("member", "backend", "turns", "tokens in/out", "valid", "revisions accepted")]
    for m in result["members"]:
        table.append((m["member"], m["backend"], str(m["turns"]), f"{m['tokens_in']:,}/{m['tokens_out']:,}",
                      f"{m['valid']}/{m['judged']}" if m["judged"] else "-",
                      f"{m['accepted']}/{m['revisions']}" if m["revisions"] else "-"))
    widths = [max(len(r[i]) for r in table) for i in range(len(table[0]))]
    lines.append("")
    lines += ["  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in table]
    lines.append("")
    found = result["findings"]
    lines.append(f"{len(found)} finding{'' if len(found) == 1 else 's'}" + (":" if found else " (nothing matched the rules)"))
    per_turn = {o[0] for o in OUTCOMES} | {"claimed_success"}  # the other titles already say how often
    for i, f in enumerate(found, 1):
        who = f["member"] or f"row {f['row']}"
        lines.append(f"[{i}] {f['code']}  {who}: {f['title']}" + (f" ({f['count']} turns)" if f["count"] > 1 and f["code"] in per_turn else ""))
        for e in f["evidence"][:6]:
            lines.append(f"    {e['member']} turn {e['turn'] or '-'}  {hms(e['t'])}  {', '.join(e['refs'])}  {e['detail']}")
        if len(f["evidence"]) > 6:
            lines.append(f"    ... and {len(f['evidence']) - 6} more")
        lines.append(f"    do: {f['suggestion']}")
    for note in result["notes"]:
        lines.append(f"note: {note}")
    return "\n".join(lines)


def member_report(result, member):
    mine = [t for t in result["turns"] if t.member == member]
    lines = [f"{member}: {len(mine)} turns"]
    for t in mine:
        state = t.end_state or "unfinished"
        lines.append(f"  turn {t.number:<3} {hms(t.start)}  {t.duration if t.duration is not None else '-':>7} s  {state:<8} "
                     f"finish {t.finish or '-':<10} tools {len(t.tools):<3} reply {len(t.reply):>6} chars  {one_line(t.prompt, 50)!r}")
    for f in result["findings"]:
        if f["member"] == member or any(e["member"] == member for e in f["evidence"]):
            numbers = [e["turn"] for e in f["evidence"] if e["member"] == member]
            numbers = [str(x) for i, x in enumerate(numbers) if x is not None and x not in numbers[:i]]
            lines.append(f"  finding {f['code']}: {f['title']} (turns {', '.join(numbers) or '-'})")
    return "\n".join(lines)


def turn_report(turn, result):
    r = turn.record()
    lines = [f"{r['member']} turn {r['turn']} ({r['backend']}, session {r['session'] or '-'}), log {r['ref']}",
             f"started {hms(r['start'])}, {r['duration_s'] if r['duration_s'] is not None else '?'} s, ended {r['end_state'] or '-'}, "
             f"finish {r['finish'] or '-'}, tokens in {r['tokens']['in']:,} out {r['tokens']['out']:,}",
             f"prompt ({r['prompt_source'] or '-'}, {r['prompt_chars'] if r['prompt_chars'] is not None else '?'} chars"
             + (f", files {', '.join(r['files'])}" if r["files"] else "") + "):"]
    lines += ["  " + l for l in (r["prompt_head"] or "(not in the log)").splitlines()]
    lines.append(f"reply ({len(r['reply'])} chars" + (", the compaction summary" if r["reply_is_compaction_summary"] else "") + "):")
    lines += ["  " + l for l in (r["reply"] or "(no text)").splitlines()]
    lines.append(f"reasoning: {r['reasoning_chars']:,} chars" + (f", {r['reasoning_tokens']:,} tokens" if r["reasoning_tokens"] else ""))
    lines.append(f"tools ({len(r['tools'])}):")
    for i, c in enumerate(r["tools"], 1):
        lines.append(f"  {i}. {c['tool']} {c['status']}{' (subagent)' if c['child'] else ''}  {c['input']}")
        if c["output"]:
            lines.append(f"     -> {one_line(c['output'], 200)}")
    for label, items in (("compactions", [hms(t) for t in r["compactions"]]),
                         ("errors", [f"{hms(e['t'])} {e['name']}: {one_line(e['message'], 160)}" for e in r["errors"]]),
                         ("aborts", [f"{hms(a['t'])} {a['reason']}" for a in r["aborts"]]),
                         ("permissions", [f"{hms(p['t'])} {p['permission']} {one_line(p['target'], 100)} -> {p['reply'] or 'pending'}"
                                          f" ({p['by'] or '-'})" for p in r["permissions"]]),
                         ("checks", [f"{hms(c['t'])} {c['from']}: {one_line(c['text'], 160)} [{c['ref']}]" for c in r["checks"]])):
        lines.append(f"{label}: " + ("-" if not items else ""))
        lines += ["  " + x for x in items]
    mine = [f for f in result["findings"] if any(e["member"] == turn.member and e["turn"] == turn.number for e in f["evidence"])]
    lines.append("findings: " + ("-" if not mine else ""))
    for f in mine:
        lines += [f"  {f['code']}: {f['title']}", f"    do: {f['suggestion']}"]
    return "\n".join(lines)


def jsonable(result):
    return {"logs": result["logs"], "members": result["members"], "findings": result["findings"], "notes": result["notes"]}


def main(argv=None, out=None):
    ap = argparse.ArgumentParser(prog="python3 -m herdr_py.diagnose", description="What went wrong with a member, and what to do about it.")
    ap.add_argument("run_dir", help="a run folder (state/ and work/ inside), or a state or work folder")
    ap.add_argument("--member", help="only this member: its turns and findings")
    ap.add_argument("--turn", type=int, help="with --member: that turn in full (1 is the member's first turn)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    a = ap.parse_args(argv)
    if out is None:  # replies hold characters an ASCII-only terminal (Python 3.6 under LANG=C) cannot write
        from .cli import utf8_stdout
        utf8_stdout()
        out = sys.stdout
    if a.turn is not None and not a.member:
        ap.error("--turn needs --member")
    if not os.path.isdir(a.run_dir):
        ap.error(f"{a.run_dir} is not a folder")
    result = diagnose(a.run_dir)
    if not any(result["logs"].values()):
        out.write(f"no logs in {a.run_dir}: expected state/events.jsonl, work/codex/events.jsonl or work/chat.jsonl\n")
        return 1
    if a.member:
        mine = [t for t in result["turns"] if t.member == a.member]
        if not mine:
            out.write(f"no turns for {a.member!r}; members: {', '.join(m['member'] for m in result['members']) or 'none'}\n")
            return 1
        if a.turn is not None:
            pick = [t for t in mine if t.number == a.turn]
            if not pick:
                out.write(f"{a.member} has turns 1 to {len(mine)}\n")
                return 1
            if a.json:
                record = pick[0].record()
                record["findings"] = [f for f in result["findings"] if any(e["member"] == a.member and e["turn"] == a.turn
                                                                           for e in f["evidence"])]
                out.write(json.dumps(record, indent=1) + "\n")
            else:
                out.write(turn_report(pick[0], result) + "\n")
            return 0
        if a.json:
            out.write(json.dumps({"turns": [t.record() for t in mine], "findings": [
                f for f in result["findings"] if f["member"] == a.member or any(e["member"] == a.member for e in f["evidence"])]},
                indent=1) + "\n")
        else:
            out.write(member_report(result, a.member) + "\n")
        return 0
    out.write((json.dumps(jsonable(result), indent=1) if a.json else report(result, a.run_dir)) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
