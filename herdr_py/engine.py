"""Event-driven team: a planner keeps a shared todo list, members take todos as soon as they are free, and a program
judges every answer (definitions §23).

    python3 -m herdr_py.engine --task task.md --judge "python3 judge.py" --planner plan=claude \\
        --member a=claude --member b=codex --about "a=layout and colour" --turns 6 --out runs/e1 [--target 90]

coop.py runs a team in rounds: every prompt of a round is built before the round starts, and the team waits for its
slowest member. Here events drive the team. When a todo has ended and a member is free with nothing it can take (or
as many todos have ended as there are members, a whole lap), the planner is woken with the team's state (built by code
from the team knowledge base: verified results, failures, todos, and who is working or free) and replies with todos to
add or drop, or says the task is done; a todo can come after others (it waits until they have ended), and a review todo
is never given to whoever made what it reviews; a reply that breaks the rules (an unknown member or entry, too many
open todos, a review for its own author) is sent back with the reasons. A member that is free takes the oldest open todo meant for it or for anyone
(teamkb.take_todo: never two members on one todo); its prompt is built by code from the state at that moment, and its
answer is judged like a coop answer (the same reply format and judge contract). While members work, TEAM_BOARD.md in
their folder shows the team's latest state, rewritten by the program after every event; members work in that folder
read-only. Only the judge's verdicts enter the shared state: the planner's todos are plans, not facts.

The run stops when the member turns are used up, the time limit is reached, the target score is reached, the
planner says done (running turns finish first), the planner's wakes are used up with nothing left to do, or --patience judged answers in a row did not
beat the best score. A person can steer a run that goes on: `python3 -m herdr_py.engine --control RUN_DIR pause` (no new
todo is taken and the planner is not woken; running turns finish), `resume`, `stop`, `turns N` or `time_limit S`
(engineview --serve offers the same as buttons); each command is appended to RUN_DIR/control.jsonl, read by the run
within a quarter second, and recorded in engine.jsonl. Records: kb/ (entries, verdicts and todos: python3 -m herdr_py.teamkb), run.jsonl (every member
turn: its todo, the board version and the hash of the prompt it was given, state, seconds, tokens, verdict),
engine.jsonl (every planner turn: why it was woken, what it changed or why it was sent back) and summary.json.
A run can start from an earlier one: --seed-from RUN_DIR copies that run's verified entries (with their verdicts and
files; --carry KIND keeps only artifacts that name one of these kinds on their first line, --seed-keep ID carries one
whatever its kind, --seed-skip ID leaves one out; --seed-entry RUN_DIR ID also carries one verified entry of another
run) into this run's knowledge base before the first prompt (TeamKB.seed, TeamKB.add_from), so the team starts with what it already
found and the skills it wrote; carried entries keep their ids and are marked seeded_from, the start record says what was
carried, and the summary counts only this run's answers.
Exit codes: 0 a valid answer was found, 1 none, 2 bad arguments, 3 stopped because the setup broke
(--stop-on-infra-error: a member's backend or the judge broke, not an answer).
"""
import argparse
import collections
import hashlib
import json
import os
import shlex
import sys
import threading
import time

from . import engineview
from .coop import FENCE, REPLY, command_judge, exit_judge, parse_reply, used
from .dag import resolve_args
from .members import BROKEN, MemberError, Members, parse_member
from .teamkb import MAX_SUMMARY, TeamKB, TeamKBError, one_line

BOARD = "TEAM_BOARD.md"
CONTROL = "control.jsonl"
COMMANDS = ("pause", "resume", "stop", "turns", "time_limit")


def send_control(out, command, value=None, by="cli"):
    """Append a person's command for the run in `out`: pause, resume, stop, turns N (the member turns in all, never
    fewer than used), time_limit S (seconds from the start). Raises ValueError for anything else."""
    if command not in COMMANDS:
        raise ValueError(f"command: one of {', '.join(COMMANDS)}, not {command!r}")
    if command in ("turns", "time_limit"):
        try:
            value = int(value) if command == "turns" else float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{command}: give a number") from None
        if value <= 0:
            raise ValueError(f"{command}: more than 0")
    elif value is not None:
        raise ValueError(f"{command} takes no value")
    if not os.path.isfile(os.path.join(out, "engine.jsonl")):
        raise ValueError(f"{out} holds no run (engine.jsonl)")
    rec = {"t": round(time.time(), 3), "command": command, "value": value, "by": str(by)[:40]}
    with open(os.path.join(out, CONTROL), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(rec) + "\n")
    return rec

PLANNER = """You plan the work of a team of {n} members on the task below. You do not do the task yourself: you keep \
the team's shared todo list. A program judges every answer (it is not a person); only its verdicts count.

Task:
{task}

Members:
{members}

Team state, as the program recorded it (board version {version}):
{board}

Budget: {turns_left} member turns left of {turns}; at most {max_open} todos may be open at a time.
You were woken because: {reason}.
Right now: {now}

Keep every free member busy: when you add todos, make sure each free member has one it can take now. Set "for" only
when the work needs that member's role; leave it null and whoever is free first takes it. If a todo needs another
todo's result first, list that todo in "after" (an id from the board, or "#2" for the second todo you add in this
reply): it cannot be taken until that one has ended. A todo that reviews or critiques work already made (not new
work) gets "review": true: it is never given to whoever made its parents or did the todos it comes after, so nobody
reviews their own work.

Reply with one fenced JSON block:
```json
{{"add": [{{"text": "what to do, in one or two sentences", "for": "a member's name, or null for anyone", \
"parents": ["ids of verified results it should build on"], "after": ["todos that must end first"], "review": false}}],
 "drop": ["ids of open todos no longer worth doing"],
 "done": false,
 "why": "one sentence"}}
```
Set "done" to true only when the best verified result is good enough and nothing more is worth trying.{problems}"""

MEMBER = """You are {member}, one of {n} members of a team working on the task below. A planner keeps the team's todo \
list and you took one todo. A program judges every answer (it is not a person); only its verdicts count.

Task:
{task}

Your todo ({tid}): {text}{parents}

The team so far, as judged by the program:
{brief}

The file {board} in your folder always shows the team's latest results, failures and todos: the program rewrites it \
whenever something changes, so read it again before you answer if your turn takes long. Your folder is read-only; \
your answer goes in your reply.

{reply}"""


def sha_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def ratio(a, b):
    return round(a / b, 3) if b else None


def todo_line(todo):
    who = f"for {todo['for']}" if todo["for"] else "for anyone"
    state = todo["state"]
    if state == "taken":
        state = f"taken by {todo['taken_by']}"
    elif state == "done":
        state = f"done by {todo['taken_by']}: {todo['entry']} scored {todo['score']:.6g}"
    elif state == "failed":
        state = f"failed ({todo['taken_by']}): {one_line(todo['detail'] or todo['status'] or '', 120)}"
    parents = f" (builds on {', '.join(todo['parents'])})" if todo["parents"] else ""
    after = f" (after {', '.join(todo['after'])})" if todo.get("after") else ""
    review = ""
    if todo.get("review"):
        review = f" (a review, not for {', '.join(todo['not_for'])})" if todo.get("not_for") else " (a review)"
    return f"- {todo['id']} [{state}] {who}: {one_line(todo['text'], 300)}{parents}{after}{review}"


class EngineRun:
    """One run. members: an object with run_turn(name, prompt, timeout=..., workdir=..., access=...) -> (reply, state)
    and, optionally, tokens(name) (members.Members, or anything shaped like it); planner is one of its names."""

    def __init__(self, task, judge, members, names, planner, out, turns, planner_wakes=None, max_open=None, max_todos=None,
                 target=None, patience=0, about=None, results=3, failures=3, answer_bytes=6000, answer_name="answer.txt",
                 turn_timeout=900, judge_name="judge", stop_on_infra_error=False, planner_tries=2, member_access="read",
                 time_limit=None, seeded=None):
        if not names:
            raise ValueError("no members")
        if planner in names:
            raise ValueError("the planner is not one of the members")
        if not (isinstance(turns, int) and turns >= 1):
            raise ValueError("turns: 1 or more")
        self.task, self.judge, self.members, self.names, self.planner = task, judge, members, list(names), planner
        self.out, self.turns = out, turns
        self.planner_wakes = planner_wakes if planner_wakes is not None else turns + 1
        self.max_open = max_open if max_open is not None else max(2, 2 * len(self.names))
        self.max_todos = max_todos if max_todos is not None else 3 * turns
        self.target, self.patience, self.about = target, patience, dict(about or {})
        self.results, self.failures, self.answer_bytes, self.answer_name = results, failures, answer_bytes, answer_name
        self.turn_timeout, self.judge_name, self.stop_on_infra_error = turn_timeout, judge_name, stop_on_infra_error
        self.planner_tries = planner_tries
        if member_access not in ("read", "research"):
            raise ValueError("member_access: read or research")
        self.member_access = member_access  # research: read the folder and search the web (Claude members)
        os.makedirs(out, exist_ok=True)
        self.kb = TeamKB(os.path.join(out, "kb"))
        self.board_dir = os.path.join(out, "board")
        os.makedirs(self.board_dir, exist_ok=True)
        self.board_lock = threading.Lock()
        self.cond = threading.Condition()
        now = time.monotonic()  # durations use the monotonic clock: a VM's wall clock can jump backwards
        self.free = {n: True for n in self.names}
        self.idle_since = {n: now for n in self.names}
        self.idle = {n: 0.0 for n in self.names}
        self.turns_used, self.running, self.planner_running, self.wakes = 0, 0, False, 0
        self.wake_reasons = ["the run started"]  # wake now: the start, or no todo left with nobody working
        self.ended_since_wake = []  # todos ended since the planner was last woken: a wake when someone is free, or a whole lap
        self.time_limit, self.started = time_limit, time.monotonic()
        self.seeded = seeded  # what the knowledge base was started with (TeamKB.seed), recorded in the start record
        self.paused, self.paused_at, self.paused_seconds, self.control_at = False, None, 0.0, 0  # a person's commands
        self.said_done, self.stopping, self.broken = False, None, []
        self.best, self.since_best, self.progress = None, 0, []
        self.turn_records, self.wake_records = [], []
        self.log = open(os.path.join(out, "run.jsonl"), "a", encoding="utf-8", buffering=1)
        self.elog = open(os.path.join(out, "engine.jsonl"), "a", encoding="utf-8", buffering=1)
        self.record_lock = threading.Lock()
        self.view_lock = threading.Lock()

    # ---- the shared state, as text
    def board_text(self):
        brief = self.kb.brief("(board)", self.results + 2, self.failures + 2, record=False)
        todos = self.kb.todo_list()[-30:]
        valid = sorted((e for e in self.kb.entries() if e["status"] == "valid" and e.get("artifact")),
                       key=lambda e: (-e["score"], e["t"]))
        files = [f"- artifacts/{e['id']}.txt: {e['id']} by {e['member']}, score {e['score']:.6g}: {one_line(e['summary'], 120)}"
                 for e in valid[:40]]
        lines = [f"Board version {self.kb.version}.", "", brief, ""]
        if files:
            lines += ["Every verified result is a file in artifacts/ (open it with your Read tool):"] + files + [""]
        lines += ["Todos:" if todos else "No todos yet."]
        lines += [todo_line(t) for t in todos]
        return "\n".join(lines)

    def write_board(self):
        with self.board_lock:
            files = os.path.join(self.board_dir, "artifacts")
            os.makedirs(files, exist_ok=True)
            for e in self.kb.entries():  # every verified result as a file members can open, however long it is
                target = os.path.join(files, e["id"] + ".txt")
                if e["status"] == "valid" and e.get("artifact") and not os.path.exists(target):
                    with open(os.path.join(self.kb.folder, e["artifact"]), "rb") as src, open(target + ".tmp", "wb") as dst:
                        dst.write(src.read())
                    os.replace(target + ".tmp", target)
            path = os.path.join(self.board_dir, BOARD)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write("# Team board\n\nRewritten by the program after every event; read-only for members.\n\n"
                             + self.board_text() + "\n")
            os.replace(tmp, path)

    def view(self):
        """Write view.html (engineview.py) after every event; a page that cannot be written never ends the run."""
        with self.view_lock:
            try:
                engineview.save(self.out)
            except Exception as exc:  # noqa: BLE001 - the page is for people; the run's records are what count
                print(f"herdr-py engine: warning: view.html not written: {type(exc).__name__}: {exc}", file=sys.stderr)

    def tokens(self, name):
        try:
            return self.members.tokens(name) if hasattr(self.members, "tokens") else None
        except Exception:  # counting tokens must never fail a turn
            return None

    def record(self, log, records, rec):
        with self.record_lock:
            records.append(rec)
            log.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # ---- the planner
    def planner_prompt(self, reason, problems):
        version = self.kb.version
        members = "\n".join(f"- {n}" + (f": {self.about[n]}" if self.about.get(n) else "") for n in self.names)
        with self.cond:
            left = self.turns - self.turns_used
            free = [n for n in self.names if self.free[n]]
        working = {t["taken_by"]: t for t in self.kb.todo_list() if t["state"] == "taken"}
        now = "; ".join([f"{n} works on {working[n]['id']} ({one_line(working[n]['text'], 60)})" for n in self.names if n in working]
                        + ([f"free and waiting for a todo: {', '.join(free)}"] if free else ["nobody is free"]))
        prompt = PLANNER.format(n=len(self.names), task=self.task.strip(), members=members, version=version,
                                board=self.board_text(), turns_left=left, turns=self.turns, max_open=self.max_open,
                                reason=reason, now=now, problems=problems)
        return prompt, version

    def check_plan(self, reply):
        """The planner's reply -> (plan, problems). Every rule is checked and every broken one is named."""
        blocks = [m.group(2) for m in FENCE.finditer(reply or "")]
        if not blocks:
            return None, ["no fenced JSON block in the reply"]
        try:
            plan = json.loads(blocks[-1])
        except ValueError as exc:
            return None, [f"the JSON does not parse: {exc}"]
        if not isinstance(plan, dict):
            return None, ["the JSON is not an object"]
        add, drop, done = plan.get("add") or [], plan.get("drop") or [], plan.get("done", False)
        problems = []
        if not isinstance(add, list) or not all(isinstance(a, dict) for a in add):
            return None, ['"add": a list of objects']
        if not isinstance(drop, list) or not all(isinstance(d, str) for d in drop):
            return None, ['"drop": a list of todo ids']
        if not isinstance(done, bool):
            problems.append('"done": true or false')
        todos = {t["id"]: t for t in self.kb.todo_list()}
        entries = {e["id"]: e["member"] for e in self.kb.entries()}
        clean = []
        for i, item in enumerate(add, 1):
            text, who, parents, after = item.get("text"), item.get("for") or None, item.get("parents") or [], item.get("after") or []
            review = item.get("review", False)
            if isinstance(who, str) and who.strip().lower() in ("null", "none", "anyone", "any"):
                who = None
            if not isinstance(text, str) or not text.strip():
                problems.append(f"add {i}: \"text\" says what to do")
            elif len(text) > MAX_SUMMARY:
                problems.append(f"add {i}: \"text\" is at most {MAX_SUMMARY} characters")
            if who is not None and who not in self.names:
                problems.append(f"add {i}: \"for\" is one of {', '.join(self.names)}, or null (no member is called {who!r})")
            if not isinstance(parents, list) or not all(isinstance(p, str) for p in parents):
                problems.append(f"add {i}: \"parents\" is a list of entry ids")
                parents = []
            missing = [p for p in parents if p not in entries and p not in todos]
            if missing:
                problems.append(f"add {i}: no entry {', '.join(missing)} (build only on entries on the board)")
            wrong = [p for p in parents if p in todos and p not in entries]  # a weaker model mixes the two kinds of id
            if wrong:
                problems.append(f"add {i}: {', '.join(wrong)} is a todo, not an entry: \"parents\" names entries (k...) a todo "
                                f"builds on; to wait for a todo, name it in \"after\"")
            if not isinstance(after, list) or not all(isinstance(a, str) for a in after):
                problems.append(f"add {i}: \"after\" is a list of todo ids")
                after = []
            for a in after:
                if a.startswith("#"):
                    k = a[1:]
                    if not (k.isdigit() and 1 <= int(k) < i):
                        problems.append(f"add {i}: \"after\" {a} must point to an earlier todo in this reply (#1 to #{i - 1})")
                elif a not in todos:
                    problems.append(f"add {i}: \"after\" names no todo {a}")
            if not isinstance(review, bool):
                problems.append(f"add {i}: \"review\" is true or false")
                review = False
            if review:  # who is known to have made what it reviews: its parents' members, who will do the todos it is after
                made = {entries[p] for p in parents if p in entries}
                for a in after:
                    if a.startswith("#") and a[1:].isdigit() and 1 <= int(a[1:]) < i:
                        made.add(clean[int(a[1:]) - 1]["for"])
                    elif a in todos:
                        made.add(todos[a]["taken_by"] or todos[a]["for"])
                made.discard(None)
                if who in made:
                    problems.append(f"add {i}: a review for {who}, who makes what it reviews: leave \"for\" null or give it "
                                    "to someone else")
                elif made >= set(self.names):
                    problems.append(f"add {i}: a review nobody could take: {', '.join(sorted(made))} make what it reviews")
            clean.append({"text": text, "for": who, "parents": parents, "after": after, "review": review})
        for tid in drop:
            if tid not in todos:
                problems.append(f"drop: no todo {tid}")
            elif todos[tid]["state"] != "open":
                problems.append(f"drop: {tid} is {todos[tid]['state']}, only open todos can be dropped")
        open_now = sum(1 for t in todos.values() if t["state"] == "open")
        open_after = open_now - len(set(drop) & {t for t in todos if todos[t]["state"] == "open"}) + len(add)
        if open_after > self.max_open:
            problems.append(f"this would leave {open_after} todos open; at most {self.max_open}")
        if len(todos) + len(add) > self.max_todos:
            problems.append(f"this would make {len(todos) + len(add)} todos in all; at most {self.max_todos} in a run")
        if problems:
            return None, problems
        return {"add": clean, "drop": list(drop), "done": done, "why": plan.get("why")}, []

    def plan(self, wake, reason):
        problems = ""
        try:
            for attempt in range(1, self.planner_tries + 1):
                prompt, version = self.planner_prompt(reason, problems)
                before, start, clock = self.tokens(self.planner), time.time(), time.monotonic()
                try:
                    reply, state = self.members.run_turn(self.planner, prompt, timeout=self.turn_timeout)
                except Exception as exc:  # the planner's backend broke: no plan this wake
                    reply, state = f"({type(exc).__name__}: {exc})", "error"
                end, took = time.time(), time.monotonic() - clock
                rec = {"t": round(end, 3), "start": round(start, 3), "end": round(end, 3), "kind": "wake", "wake": wake,
                       "attempt": attempt, "reason": reason, "board_version": version, "prompt_sha": sha_text(prompt),
                       "state": state, "seconds": round(took, 2), "tokens": used(before, self.tokens(self.planner))}
                if state != "idle":
                    rec["problems"] = [f"the planner's turn ended {state}: {one_line(reply, 200)}"]
                    if state in BROKEN:
                        with self.cond:
                            self.broken.append(f"planner wake {wake}: the backend ended {state}")
                    self.record(self.elog, self.wake_records, rec)
                    return
                plan, found = self.check_plan(reply)
                if found:
                    rec["problems"] = found
                    self.record(self.elog, self.wake_records, rec)
                    problems = ("\n\nYour last reply was sent back:\n" + "\n".join("- " + p for p in found)
                                + "\nReply again with a corrected JSON block.")
                    continue
                dropped = [tid for tid in plan["drop"] if self.kb.drop_todo(tid, by=self.planner)]
                added = []
                for item in plan["add"]:
                    after = [added[int(a[1:]) - 1] if a.startswith("#") else a for a in item["after"]
                             if not a.startswith("#") or int(a[1:]) <= len(added)]
                    try:
                        added.append(self.kb.add_todo(item["text"], for_member=item["for"], parents=item["parents"],
                                                      by=self.planner, wake=wake, after=after, review=item["review"]))
                    except TeamKBError as exc:  # checked above; a race with another writer is reported, not hidden
                        rec.setdefault("problems", []).append(str(exc))
                        added.append(None)
                added = [a for a in added if a]
                rec.update(added=added, dropped=dropped, done=plan["done"], why=one_line(plan.get("why") or "", 300))
                self.record(self.elog, self.wake_records, rec)
                if plan["done"]:
                    with self.cond:
                        self.said_done = True
                return
        finally:
            self.write_board()
            self.view()
            with self.cond:
                self.planner_running = False
                self.cond.notify_all()

    # ---- a member's turn
    def member_prompt(self, name, todo, turn):
        version = self.kb.version
        brief, shown = self.kb.brief(name, self.results, self.failures, round=turn, answer_bytes=self.answer_bytes,
                                     with_ids=True)
        by_id = {e["id"]: e for e in self.kb.entries()}
        parents = ""
        for pid in todo["parents"]:
            e = by_id[pid]
            score = f"score {e['score']:.10g}" if e["score"] is not None else e["status"]
            parents += f"\nIt builds on {pid} by {e['member']} ({score}): {one_line(e['summary'])}"
            if self.answer_bytes:
                parents += "\n" + "\n".join(self.kb.answer_lines(e, self.answer_bytes))
        prompt = MEMBER.format(member=name, n=len(self.names), task=self.task.strip(), tid=todo["id"], text=todo["text"],
                               parents=parents, brief=brief, board=BOARD, reply=REPLY)
        return prompt, set(shown) | set(todo["parents"]), version

    def member_turn(self, name, todo, turn):
        rec = {"t": round(time.time(), 3), "start": round(time.time(), 3), "turn": turn, "member": name, "todo": todo["id"]}
        outcome, status, score, entry, detail = "failed", None, None, None, None
        try:
            prompt, shown, version = self.member_prompt(name, todo, turn)
            rec.update(board_version=version, prompt_sha=sha_text(prompt))
            before, clock = self.tokens(name), time.monotonic()
            try:
                text, state = self.members.run_turn(name, prompt, timeout=self.turn_timeout, workdir=self.board_dir,
                                                    access=self.member_access)
            except Exception as exc:  # a member backend that breaks fails its turn
                text, state = f"({type(exc).__name__}: {exc})", "error"
            rec.update(state=state, seconds=round(time.monotonic() - clock, 2), tokens=used(before, self.tokens(name)))
            parsed = parse_reply(text) if state == "idle" else None
            if parsed is None:
                detail = rec["problem"] = f"the turn ended {state}" + (f": {one_line(text, 300)}" if text else "")
            elif parsed["kind"] is None:
                detail = rec["problem"] = "no fenced answer and no FAILED line"
                rec["reply_tail"] = text[-300:]
            else:
                parents = list(collections.OrderedDict.fromkeys(todo["parents"] + [p for p in parsed["parents"] if p in shown]))
                before_len = len(self.kb)
                entry = self.kb.propose(name, parsed["kind"], one_line(parsed["summary"], 500), artifact=parsed["answer"],
                                        name=self.answer_name, parents=parents, round=turn, scope="team")
                rec.update(entry=entry, kind=parsed["kind"], parents=parents, repeat=len(self.kb) == before_len)
                dropped = [p for p in parsed["parents"] if p not in shown]
                if dropped:
                    rec["parents_dropped"] = dropped
                if parsed["kind"] == "result":
                    known = self.kb.verdict(entry) if rec["repeat"] else None
                    status, score = known or self.kb.judge(entry, self.judge, judge=self.judge_name)
                    rec.update(status=status, score=score)
                    outcome = "done" if status == "valid" else "failed"
                    detail = None if status == "valid" else next((e["detail"] for e in self.kb.entries() if e["id"] == entry), None)
                else:
                    detail = parsed["summary"]
        except Exception as exc:  # the engine's own mistake: recorded and the todo ended, never left taken
            rec["problem"] = detail = f"engine error: {type(exc).__name__}: {exc}"
        finally:
            try:
                self.kb.end_todo(todo["id"], name, outcome, entry=entry, status=status, score=score, detail=detail)
            finally:
                rec["end"] = round(time.time(), 3)
                self.record(self.log, self.turn_records, rec)
                self.write_board()
                self.view()
                with self.cond:
                    self.free[name] = True
                    self.idle_since[name] = None if self.paused else time.monotonic()
                    self.running -= 1
                    if rec.get("state") in BROKEN:
                        self.broken.append(f"{name} turn {turn}: the backend ended {rec['state']}")
                    if status == "infra_error":
                        self.broken.append(f"{name} turn {turn}: the judge failed on {entry}")
                    if rec.get("kind") == "result" and status in ("valid", "invalid"):
                        if status == "valid" and (self.best is None or score > self.best):
                            self.best, self.since_best = score, 0
                        else:
                            self.since_best += 1
                        self.progress.append({"turn": turn, "t": round(time.time(), 3), "best": self.best})
                    self.ended_since_wake.append(f"{name} ended todo {todo['id']}: {outcome}")
                    self.cond.notify_all()

    # ---- the engine
    def read_control(self):
        """Called with the condition held: apply the commands appended to control.jsonl since the last call, and record
        each in engine.jsonl. A line that is not a known command is recorded as refused."""
        path = os.path.join(self.out, CONTROL)
        if not os.path.isfile(path) or os.path.getsize(path) <= self.control_at:
            return
        with open(path, encoding="utf-8", errors="replace") as handle:
            handle.seek(self.control_at)
            chunk = handle.read()
        done = chunk[:chunk.rfind("\n") + 1]  # a line still being written waits for the next call
        self.control_at += len(done.encode("utf-8"))
        for line in done.splitlines():
            try:
                cmd = json.loads(line)
                command, value = cmd["command"], cmd.get("value")
            except (ValueError, KeyError, TypeError):
                cmd, command, value = {}, None, None
            now = time.monotonic()
            rec = {"t": round(time.time(), 3), "kind": "control", "command": command, "value": value, "by": cmd.get("by")}
            if command == "pause" and not self.paused:
                self.paused, self.paused_at = True, now
                for name in self.names:  # time spent paused is not time a member waited for work
                    if self.free[name] and self.idle_since[name] is not None:
                        self.idle[name] += now - self.idle_since[name]
                        self.idle_since[name] = None
            elif command == "resume" and self.paused:
                self.paused, self.paused_seconds = False, self.paused_seconds + now - self.paused_at
                for name in self.names:
                    if self.free[name]:
                        self.idle_since[name] = now
            elif command == "stop":
                self.stopping = self.stopping or "stopped by a person"
            elif command == "turns" and isinstance(value, int) and value > 0:
                self.turns = max(value, self.turns_used)
                rec["applied"] = self.turns
            elif command == "time_limit" and isinstance(value, (int, float)) and value > 0:
                self.time_limit = float(value)
            elif command not in ("pause", "resume"):
                rec["refused"] = "not a command: " + line[:120]
            self.record(self.elog, self.wake_records, rec)

    def nothing_to_take(self):
        """With nobody working: None when some member can take a todo now; otherwise why not, in a few words (then
        nothing changes until the planner adds or drops todos)."""
        if any(self.kb.takeable(n) for n in self.names):
            return None
        stuck = [t["id"] for t in self.kb.todo_list() if t["state"] == "open"]
        return f"nobody can take the open todos ({', '.join(stuck)})" if stuck else "no todo is left"

    def why_stop(self):
        """Called with the condition held: why no new work should start, or None."""
        if self.time_limit and time.monotonic() - self.started >= self.time_limit:
            return f"the time limit of {self.time_limit:g} s was reached"
        if self.target is not None and self.best is not None and self.best >= self.target:
            return f"the target {self.target:g} was reached"
        if self.patience and self.since_best >= self.patience:
            return f"{self.patience} judged answers in a row did not beat the best"
        if self.broken and self.stop_on_infra_error:
            return "the setup broke: " + "; ".join(self.broken[:3])
        if self.said_done:
            return "the planner says the task is done"
        if self.turns_used >= self.turns:
            return "the member turns are used up"
        if self.paused:
            return None  # a person holds the run: nothing is a stall until it resumes
        why = None if self.running or self.planner_running or self.wake_reasons else self.nothing_to_take()
        if why:
            if self.wakes >= self.planner_wakes:
                return f"the planner's wakes are used up and {why}"
            self.wake_reasons.append("no todo is open and no one is working" if why == "no todo is left"
                                     else f"{why} and no one is working")
        return None

    def run(self):
        started, self.started = time.time(), time.monotonic()
        self.write_board()
        self.record(self.elog, self.wake_records, {"t": round(started, 3), "kind": "start", "members": self.names,
                                                  "planner": self.planner, "turns": self.turns,
                                                  "planner_wakes": self.planner_wakes, "max_open": self.max_open,
                                                  "time_limit": self.time_limit, "seeded": self.seeded})
        threads = []
        with self.cond:
            while True:
                self.read_control()
                if self.stopping is None:
                    self.stopping = self.why_stop()
                if self.stopping is None and not self.paused:
                    for name in self.names:
                        if not self.free[name] or self.turns_used >= self.turns:
                            continue
                        todo = self.kb.take_todo(name, turn=self.turns_used + 1)
                        if todo is None:
                            continue
                        self.turns_used += 1
                        self.free[name], self.running = False, self.running + 1
                        self.idle[name] += time.monotonic() - self.idle_since[name]
                        threads.append(threading.Thread(target=self.member_turn, args=(name, todo, self.turns_used), daemon=True))
                        threads[-1].start()
                if self.stopping is None and not self.paused and not self.planner_running and self.wakes < self.planner_wakes:
                    # wake the planner for the start or a stall, or when todos have ended and someone is free with
                    # nothing to take (or a whole lap of todos has ended): not after every single result
                    starving = [n for n in self.names if self.free[n]]  # (once the turns are used up the run is stopping)
                    reasons = list(self.wake_reasons)
                    if self.ended_since_wake and (starving or len(self.ended_since_wake) >= len(self.names)):
                        reasons += self.ended_since_wake
                        if starving:
                            reasons.append("free with nothing to take: " + ", ".join(starving))
                    if reasons:
                        reason = "; ".join(reasons[:6]) + (f" (and {len(reasons) - 6} more)" if len(reasons) > 6 else "")
                        self.wake_reasons, self.ended_since_wake = [], []
                        self.planner_running, self.wakes = True, self.wakes + 1
                        threads.append(threading.Thread(target=self.plan, args=(self.wakes, reason), daemon=True))
                        threads[-1].start()
                if self.stopping is not None and self.running == 0 and not self.planner_running:
                    break
                if self.stopping is None and not self.paused and not self.planner_running and self.running == 0 \
                        and self.wakes >= self.planner_wakes:
                    why = self.nothing_to_take()
                    if why:
                        self.stopping = f"the planner's wakes are used up and {why}"
                        continue
                self.cond.wait(timeout=0.25)  # a person's command (control.jsonl) is read within a quarter second
        for t in threads:
            t.join()
        ended, took = time.time(), time.monotonic() - self.started
        for name in self.names:  # time a member spent free with nothing to take, up to the end
            if self.free[name] and self.idle_since[name] is not None:
                self.idle[name] += time.monotonic() - self.idle_since[name]
        if self.paused:
            self.paused_seconds += time.monotonic() - self.paused_at
        self.record(self.elog, self.wake_records, {"t": round(ended, 3), "kind": "stop", "why": self.stopping})
        self.write_board()
        self.log.close()
        self.elog.close()
        summary = self.summary(took)
        tmp = os.path.join(self.out, "summary.json.tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, ensure_ascii=False, indent=1)
        os.replace(tmp, os.path.join(self.out, "summary.json"))
        self.view()
        return summary

    def board_reads(self):
        """Member tool events that name TEAM_BOARD.md (Codex, Claude and OpenCode logs under members/); command members
        keep no such log, so they are not measured."""
        count, measured = collections.Counter(), []
        for backend in ("codex", "claude", "opencode"):
            path = os.path.join(self.out, "members", backend, "events.jsonl")
            if not os.path.isfile(path):
                continue
            measured.append(backend)
            with open(path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict) and row.get("agent") in self.names and BOARD in json.dumps(row.get("event")):
                        count[row["agent"]] += 1
        return {"by_member": dict(count), "measured": measured}

    def summary(self, seconds):
        s = self.kb.stats()
        mine = [e for e in self.kb.entries() if not e.get("seeded_from")]  # entries carried in from an earlier run are not this run's
        valid = [e for e in mine if e["status"] == "valid"]
        best = max(valid, key=lambda e: (e["score"], -e["t"]), default=None)
        statuses = collections.Counter(e["status"] for e in mine)
        turns, wakes = self.turn_records, [w for w in self.wake_records if w.get("kind") == "wake"]
        member_tokens = [t["tokens"] for t in turns if t.get("tokens") is not None]
        planner_tokens = [w["tokens"] for w in wakes if w.get("tokens") is not None]
        total = sum(member_tokens) + sum(planner_tokens) if (member_tokens or planner_tokens) else None
        traceable = sum(1 for t in turns if t.get("todo") and t.get("prompt_sha") and t.get("board_version") is not None)
        todos = self.kb.todo_list()
        return {"members": self.names, "planner": self.planner, "turn_budget": self.turns, "turns": len(turns),
                "turn_states": dict(collections.Counter(t.get("state", "engine_error") for t in turns)),
                "planner_wakes": self.wakes, "planner_turns": len(wakes),
                "planner_refused": sum(1 for w in wakes if w.get("problems") and w.get("state") == "idle"),
                "answers": sum(1 for t in turns if t.get("kind") == "result"),
                "valid": statuses["valid"], "invalid": statuses["invalid"], "judge_errors": statuses["infra_error"],
                "seeded": len(self.kb.entries()) - len(mine),
                "best": best["score"] if best else None, "best_entry": best["id"] if best else None,
                "best_member": best["member"] if best else None, "progress": self.progress,
                "todos": dict(collections.Counter(t["state"] for t in todos)), "todos_total": len(todos),
                "double_takes": self.kb.double_takes, "traceable": ratio(traceable, len(turns)),
                "idle_seconds": {n: round(v, 1) for n, v in self.idle.items()}, "seconds": round(seconds, 1),
                "tokens": total, "tokens_members": sum(member_tokens) if member_tokens else None,
                "tokens_planner": sum(planner_tokens) if planner_tokens else None,
                "planner_share": ratio(sum(planner_tokens), total) if total else None,
                "board_reads": self.board_reads(), "adoption_rate": s["adoption_rate"], "paused_seconds": round(self.paused_seconds, 1),
                "duplicate_rate": s["duplicate_rate"], "stopped": self.stopping, "broken": self.broken}


def report(s, out):
    lines = [f"stopped: {s['stopped']}",
             f"{s['turns']} member turns of {s['turn_budget']} ({', '.join(f'{n} {k}' for k, n in sorted(s['turn_states'].items()))}); "
             f"{s['planner_turns']} planner turns in {s['planner_wakes']} wakes ({s['planner_refused']} sent back)",
             f"{s['answers']} answers: {s['valid']} valid, {s['invalid']} invalid, {s['judge_errors']} judge errors; "
             + (f"best {s['best']:.10g} ({s['best_entry']} by {s['best_member']})" if s["best"] is not None else "no valid answer"),
             f"todos: {s['todos_total']} ({', '.join(f'{n} {k}' for k, n in sorted(s['todos'].items()))}); "
             f"taken twice {s['double_takes']}; turns traceable {s['traceable']}",
             f"{s['seconds']} s; members free with nothing to take: "
             + ", ".join(f"{n} {v} s" for n, v in s["idle_seconds"].items()),
             f"tokens: {s['tokens'] if s['tokens'] is not None else 'not reported'}"
             + (f" (planner {s['planner_share']:.0%})" if s["planner_share"] is not None else ""),
             f"board reads: {s['board_reads']['by_member'] or 'none'} (measured for: {', '.join(s['board_reads']['measured']) or 'no member logs'})",
             f"entries and todos: python3 -m herdr_py.teamkb {os.path.join(out, 'kb')}"]
    if s["broken"]:
        lines.insert(1, "the setup broke: " + "; ".join(s["broken"][:5]))
    return "\n".join(lines)


def control_main(argv):
    """--control RUN_DIR pause|resume|stop|turns N|time_limit S"""
    if len(argv) not in (2, 3):
        print("usage: python3 -m herdr_py.engine --control RUN_DIR pause|resume|stop|turns N|time_limit S", file=sys.stderr)
        return 2
    try:
        rec = send_control(argv[0], argv[1].replace("-", "_"), argv[2] if len(argv) == 3 else None, by=f"cli:{os.getpid()}")
    except ValueError as exc:
        print(f"herdr-py engine: {exc}", file=sys.stderr)
        return 2
    print(f"sent {rec['command']}" + (f" {rec['value']}" if rec["value"] is not None else "")
          + f" to {argv[0]} (the run reads it within a quarter second; engine.jsonl records it)")
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["--control"]:
        return control_main(argv[1:])
    ap = argparse.ArgumentParser(prog="python3 -m herdr_py.engine", description=__doc__.split("\n\n")[0])
    ap.add_argument("--task", required=True, metavar="FILE", help="the task, as text the members read")
    ap.add_argument("--judge", required=True, metavar="COMMAND", help="the judge; the answer file is added as its last argument")
    ap.add_argument("--judge-mode", choices=["json", "exit"], default="json", help="as in herdr_py.coop")
    ap.add_argument("--judge-timeout", type=int, default=120, metavar="S")
    ap.add_argument("--planner", required=True, metavar="NAME=BACKEND[:MODEL]", help="who keeps the todo list")
    ap.add_argument("--member", action="append", default=[], metavar="NAME=BACKEND[:MODEL]",
                    help="a member: NAME=opencode|codex|claude[:MODEL] or NAME=command:COMMAND (repeat for each member)")
    ap.add_argument("--about", action="append", default=[], metavar="NAME=TEXT", help="what a member is good at (for the planner)")
    ap.add_argument("--turns", type=int, metavar="N", help="member turns in all (default: 3 per member)")
    ap.add_argument("--planner-wakes", type=int, metavar="N", help="planner wakes at most (default: turns + 1)")
    ap.add_argument("--max-open", type=int, metavar="N", help="open todos at a time (default: 2 per member)")
    ap.add_argument("--target", type=float, help="stop when a valid answer scores this much")
    ap.add_argument("--patience", type=int, default=0, metavar="K",
                    help="stop after K judged answers in a row that did not beat the best (0: never)")
    ap.add_argument("--out", required=True, metavar="DIR", help="a new folder for this run")
    ap.add_argument("--socket", help="the herdr-py daemon's socket (opencode members)")
    ap.add_argument("--turn-timeout", type=int, default=900, metavar="S")
    ap.add_argument("--show-results", type=int, default=3, metavar="N")
    ap.add_argument("--show-failures", type=int, default=3, metavar="N")
    ap.add_argument("--answer-bytes", type=int, default=6000, metavar="N", help="show answers up to this size (0: never)")
    ap.add_argument("--answer-name", default="answer.txt", help="the answer file's name (its extension matters to some judges)")
    ap.add_argument("--stop-on-infra-error", action="store_true",
                    help="stop when a member's backend or the judge breaks (exit code 3)")
    ap.add_argument("--time-limit", type=float, metavar="S", help="start no new work after this many seconds (running turns finish)")
    ap.add_argument("--seed-from", metavar="RUN_DIR", help="start the knowledge base from an earlier run's verified entries")
    ap.add_argument("--carry", action="append", default=[], metavar="KIND",
                    help="with --seed-from: carry only artifacts of this kind (repeat; default: every verified entry)")
    ap.add_argument("--seed-skip", action="append", default=[], metavar="ENTRY", help="with --seed-from: never carry this entry")
    ap.add_argument("--seed-keep", action="append", default=[], metavar="ENTRY",
                    help="with --seed-from: carry this entry whatever its kind (a version a person picked)")
    ap.add_argument("--seed-entry", action="append", default=[], nargs=2, metavar=("RUN_DIR", "ENTRY"),
                    help="also carry this verified entry of that run (a picked version made in an earlier run)")
    ap.add_argument("--member-access", choices=["read", "research"], default="read",
                    help="read (default): members read the board folder; research: and search the web (Claude members)")
    a = ap.parse_args(argv)
    problems = []
    if not a.member:
        problems.append("--member: give at least one")
    for path in ("run.jsonl", "kb", "summary.json"):
        if os.path.exists(os.path.join(a.out, path)):
            problems.append(f"--out: {a.out} already holds a run ({path}); give a new folder")
            break
    about = {}
    for item in a.about:
        name, sep, text = item.partition("=")
        if not sep:
            problems.append(f"--about {item!r}: NAME=TEXT")
        about[name.strip()] = text.strip()
    specs, task = [], None
    try:
        here = os.getcwd()
        for raw in a.member + [a.planner]:
            spec = parse_member(raw)
            if spec["backend"] == "command":  # members work in the board folder: their files are named from here
                spec = dict(spec, command=" ".join(shlex.quote(x) for x in resolve_args(spec["command"], here)))
            specs.append(spec)
        with open(a.task, encoding="utf-8") as handle:
            task = handle.read()
    except (MemberError, OSError) as exc:
        problems.append(str(exc))
    names = [s["name"] for s in specs[:-1]]
    planner = specs[-1]["name"] if specs else None
    if planner in names:
        problems.append(f"--planner: {planner} is also a member; give it its own name")
    unknown = sorted(set(about) - set(names))
    if unknown:
        problems.append(f"--about: no member called {', '.join(unknown)}")
    turns = a.turns if a.turns is not None else 3 * max(1, len(names))
    if turns < 1:
        problems.append("--turns: 1 or more")
    source = None
    if a.seed_from:
        source = next((d for d in (os.path.join(a.seed_from, "kb"), a.seed_from)
                       if os.path.isfile(os.path.join(d, "events.jsonl"))), None)
        if source is None:
            problems.append(f"--seed-from: no knowledge base in {a.seed_from} (neither kb/events.jsonl nor events.jsonl)")
    elif a.carry or a.seed_skip or a.seed_keep:
        problems.append("--carry, --seed-skip and --seed-keep go with --seed-from")
    for run_dir, _ in a.seed_entry:
        if not os.path.isfile(os.path.join(run_dir, "kb", "events.jsonl")):
            problems.append(f"--seed-entry: no knowledge base in {run_dir} (kb/events.jsonl)")
    if problems:
        print("herdr-py engine: " + "; ".join(problems), file=sys.stderr)
        return 2
    seeded = None
    if source is not None or a.seed_entry:
        kb = TeamKB(os.path.join(a.out, "kb"))
        try:
            seeded = (kb.seed(source, kinds=a.carry or None, skip=a.seed_skip, keep=a.seed_keep, origin=os.path.abspath(a.seed_from))
                      if source is not None else {"from": None, "carried": [], "kinds": {}, "parents_dropped": 0, "left_out": {}})
            picked = collections.OrderedDict()
            for run_dir, eid in a.seed_entry:
                picked.setdefault(os.path.abspath(run_dir), []).append(eid)
            seeded["picked"] = []
            for run_dir, ids in picked.items():
                more = kb.add_from(os.path.join(run_dir, "kb"), ids, origin=run_dir)
                seeded["picked"] += more["carried"]
                seeded["carried"] += more["carried"]
                for k, n in more["kinds"].items():
                    seeded["kinds"][k] = seeded["kinds"].get(k, 0) + n
        except TeamKBError as exc:
            print(f"herdr-py engine: --seed-from: {exc}", file=sys.stderr)
            return 2
        print(f"carried {len(seeded['carried'])} verified entries from {a.seed_from or 'earlier runs'}"
              + (f" ({', '.join(f'{n} {k}' for k, n in sorted(seeded['kinds'].items()))})" if seeded["kinds"] else "")
              + (f"; left out {', '.join(f'{n} {k}' for k, n in sorted(seeded['left_out'].items()))}" if seeded["left_out"] else ""),
              flush=True)
    judge = (exit_judge if a.judge_mode == "exit" else command_judge)(resolve_args(a.judge, os.getcwd()), timeout=a.judge_timeout)
    members = Members(specs, os.path.join(os.path.abspath(a.out), "members"), socket=a.socket, sessions="fresh", cwd=os.getcwd())
    try:
        run = EngineRun(task, judge, members, names, planner, a.out, turns, planner_wakes=a.planner_wakes, max_open=a.max_open,
                        target=a.target, patience=a.patience, about=about, results=a.show_results,
                        failures=a.show_failures, answer_bytes=a.answer_bytes, answer_name=a.answer_name,
                        turn_timeout=a.turn_timeout, stop_on_infra_error=a.stop_on_infra_error,
                        member_access=a.member_access, time_limit=a.time_limit, seeded=seeded)
        summary = run.run()
    finally:
        members.close()
    print(report(summary, a.out))
    if summary["broken"] and a.stop_on_infra_error:
        return 3
    return 0 if summary["best"] is not None else 1


if __name__ == "__main__":
    sys.exit(main())
