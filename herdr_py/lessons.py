"""Lessons: corrections a person, or the agent that supervises a team, made to a team's work, kept so that the next team
that meets the same situation is told before it acts, and measured, so that a lesson that does not help is retired.
Standard library only; Python 3.6+.

A lessons file is an append-only JSON Lines log:

    {"type": "lesson", "id": "L...", "t", "by", "when": REGEX, "wrong": TEXT, "right": TEXT, "detect": REGEX or null,
     "evidence": TEXT, "source": {"run": FOLDER, "step": ID} or null}
    {"type": "review", "id", "t", "status": "hypothesis" | "confirmed" | "retired", "why", "counts": {...}}

- when: a regular expression searched, without regard to case, before an action: in the task and the todo a member
  takes, and in the task when the planner is woken. A match puts the lesson into that prompt, and the turn's record
  lists it ("lessons").
- detect (may be left out): a regular expression that recognizes the mistake itself in what a member hands in, or in
  the todos a planner adds. A turn that was shown the lesson and still matches it repeated the mistake
  ("lessons_repeated").
- review: over the runs given, the turns each lesson was shown in and the ones that repeated the mistake; and, from the
  turns before the lesson existed that its `when` matches, how often the mistake was made then. Status changes only
  by these rules, and only forward: repeated in RETIRE_AFTER or more turns after being shown -> retired, for good (to
  try again, write the lesson anew: it gets a new id); with detect, shown in CONFIRM_AFTER or more turns and never
  repeated -> confirmed. A review of fewer runs never moves a lesson back. A retired lesson is not shown.

    python3 -m herdr_py.lessons FILE add --when REGEX --wrong TEXT --right TEXT [--detect REGEX] [--evidence TEXT]
                                         [--run RUN_FOLDER --step ID] --by WHO
    python3 -m herdr_py.lessons FILE list
    python3 -m herdr_py.lessons FILE review RUN_FOLDER...    (records the status changes the rules make)
"""
import argparse
import collections
import hashlib
import json
import os
import re
import sys
import time

from .teamkb import TeamKB, artifact_path

RETIRE_AFTER = 2
CONFIRM_AFTER = 3
MAX_TEXT = 1000
STATUSES = ("hypothesis", "confirmed", "retired")
SHOWN = """
CORRECTIONS FROM EARLIER WORK (a person or the team's supervisor found these mistakes before; check that you are not
about to make them again):
{lines}
"""


class LessonError(Exception):
    """A lesson that cannot be added (the message says why), or a lessons file that cannot be read."""


def pattern(text, what):
    if not isinstance(text, str) or not text.strip():
        raise LessonError(f"{what}: give a regular expression")
    try:
        return re.compile(text, re.I)
    except re.error as exc:
        raise LessonError(f"{what} {text!r} is not a regular expression: {exc}")


def one_line(text, limit=MAX_TEXT):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


class Lessons:
    def __init__(self, path):
        self.path = os.path.abspath(os.path.expanduser(path))
        self.lessons = collections.OrderedDict()
        self.bad_lines = 0
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        self._apply(line)

    def _apply(self, line):
        try:
            e = json.loads(line)
            if e.get("type") == "lesson":
                lesson = dict(e, status="hypothesis", _when=pattern(e["when"], "when"),
                              _detect=pattern(e["detect"], "detect") if e.get("detect") else None)
                self.lessons.setdefault(e["id"], lesson)
            elif e.get("type") == "review" and e.get("id") in self.lessons and e.get("status") in STATUSES:
                self.lessons[e["id"]]["status"] = e["status"]
            else:
                self.bad_lines += 1
        except (ValueError, KeyError, TypeError, AttributeError, LessonError):
            self.bad_lines += 1

    def _write(self, event):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._apply(json.dumps(event))

    def add(self, when, wrong, right, by, detect=None, evidence="", source=None):
        """Record a lesson (a hypothesis); the same when, wrong and right again is the lesson already there."""
        pattern(when, "when")
        if detect:
            pattern(detect, "detect")
        if not str(wrong or "").strip() or not str(right or "").strip():
            raise LessonError("a lesson needs what went wrong (wrong) and the right way (right)")
        if not str(by or "").strip():
            raise LessonError("say who made the correction (by)")
        lid = "L" + hashlib.sha256(f"{when}\0{wrong}\0{right}".encode("utf-8")).hexdigest()[:8]
        if lid not in self.lessons:
            self._write({"type": "lesson", "id": lid, "t": round(time.time(), 3), "by": one_line(by, 200),
                         "when": when, "wrong": one_line(wrong), "right": one_line(right), "detect": detect or None,
                         "evidence": one_line(evidence), "source": source or None})
        return self.lessons[lid]

    def active(self):
        return [l for l in self.lessons.values() if l["status"] != "retired"]

    def matching(self, text):
        """The lessons (not retired) whose `when` is found in this text."""
        return [l for l in self.active() if l["_when"].search(text or "")]

    def review(self, runs, write=True):
        """[(lesson, counts, status, why)] for every lesson; with write, status changes are recorded."""
        turns = list(read_turns(runs))
        out = []
        for lesson in self.lessons.values():
            counts = measure(lesson, turns)
            status, why = decide(lesson, counts)
            if write and status != lesson["status"]:
                self._write({"type": "review", "id": lesson["id"], "t": round(time.time(), 3), "status": status,
                             "why": why, "counts": counts})
            out.append((lesson, counts, status, why))
        return out


def shown_text(lessons):
    """The part of a prompt that tells a member or the planner what was corrected before."""
    lines = [f"- {l['id']}: wrong: {l['wrong']}\n  right: {l['right']}" for l in lessons]
    return SHOWN.format(lines="\n".join(lines)) if lines else ""


def repeated(lessons, texts):
    """The ids of the lessons whose `detect` is found in any of these texts (what a turn handed in)."""
    return [l["id"] for l in lessons if l.get("_detect") and any(l["_detect"].search(t or "") for t in texts)]


def read_turns(runs):
    """Every member turn and planner wake of these run folders: {"t", "where", "text" (what `when` is matched against),
    "made" (what `detect` is matched against), "shown", "repeated"}."""
    for run in runs:
        task = ""
        try:
            with open(os.path.join(run, "task.md"), encoding="utf-8", errors="replace") as handle:
                task = handle.read()
        except OSError:
            pass  # a run from before task.md was kept: its todos are matched on their own
        kb = TeamKB(os.path.join(run, "kb")) if os.path.isdir(os.path.join(run, "kb")) else None
        todos = {t["id"]: t for t in kb.todo_list()} if kb else {}
        entries = {e["id"]: e for e in kb.entries()} if kb else {}
        for rec in records(os.path.join(run, "run.jsonl")):
            if "member" not in rec or "todo" not in rec:
                continue
            todo = todos.get(rec["todo"]) or {}
            made = [answer(kb, entries.get(eid)) for eid in [rec.get("entry")] + [r.get("entry") for r in rec.get("repairs") or []]
                    if eid]
            yield {"t": rec.get("start") or rec.get("t") or 0, "where": f"{run} turn {rec.get('turn')}",
                   "text": task + "\n" + (todo.get("text") or ""), "made": made,
                   "shown": rec.get("lessons") or [], "repeated": rec.get("lessons_repeated") or []}
        for rec in records(os.path.join(run, "engine.jsonl")):
            if rec.get("kind") != "wake":
                continue
            yield {"t": rec.get("t") or 0, "where": f"{run} wake {rec.get('wake')}", "text": task,
                   "made": [(todos.get(tid) or {}).get("text") or "" for tid in rec.get("added") or []],
                   "shown": rec.get("lessons") or [], "repeated": rec.get("lessons_repeated") or []}


def records(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    yield rec
    except OSError:
        return


def answer(kb, entry):
    if not kb or not entry or not entry.get("artifact"):
        return (entry or {}).get("summary") or ""
    path = artifact_path(kb.folder, entry["artifact"])
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except (OSError, TypeError):
        return ""


def measure(lesson, turns):
    """{"shown", "repeated", "before", "before_made"}: turns shown the lesson and those of them that repeated the
    mistake; turns from before the lesson whose `when` matches, and those of them that made the mistake."""
    c = {"shown": 0, "repeated": 0, "before": 0, "before_made": 0}
    for turn in turns:
        if lesson["id"] in turn["shown"]:
            c["shown"] += 1
            c["repeated"] += lesson["id"] in turn["repeated"]
        elif turn["t"] < lesson["t"] and lesson["_when"].search(turn["text"]):
            c["before"] += 1
            c["before_made"] += bool(lesson.get("_detect") and any(lesson["_detect"].search(m) for m in turn["made"]))
    return c


def decide(lesson, c):
    """(status, why) by the rules in the module notes."""
    now = lesson.get("status", "hypothesis")
    if c["repeated"] >= RETIRE_AFTER:
        return "retired", f"shown in {c['shown']} turns and the mistake came back in {c['repeated']}"
    if now == "retired":
        return "retired", "retired by an earlier review (write the lesson anew to try it again)"
    if lesson.get("_detect") and c["shown"] >= CONFIRM_AFTER and c["repeated"] == 0:
        return "confirmed", f"shown in {c['shown']} turns and the mistake did not come back"
    if not lesson.get("_detect"):
        return now, "no detect: nothing to measure it by"
    return now, f"shown in {c['shown']} turns, the mistake came back in {c['repeated']}"


def rate(made, of):
    return "-" if not of else f"{made}/{of}"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m herdr_py.lessons", description=__doc__.split("\n\n")[0])
    ap.add_argument("file", metavar="FILE", help="the lessons file (JSON Lines; made when the first lesson is added)")
    ap.add_argument("command", choices=["add", "list", "review"])
    ap.add_argument("runs", nargs="*", metavar="RUN_FOLDER", help="review: the runs to measure the lessons on")
    ap.add_argument("--when", help="add: a regular expression; the lesson is shown where it is found")
    ap.add_argument("--wrong", help="add: what the team did wrong")
    ap.add_argument("--right", help="add: the right way")
    ap.add_argument("--detect", help="add: a regular expression that finds the mistake in what a member hands in")
    ap.add_argument("--evidence", default="", help="add: what shows it (a run, a file, a test)")
    ap.add_argument("--run", help="add: the run the mistake was made in")
    ap.add_argument("--step", help="add: the turn, todo or entry in that run")
    ap.add_argument("--by", help="add: who made the correction")
    a = ap.parse_args(argv)
    try:
        book = Lessons(a.file)
        if a.command == "add":
            source = {"run": os.path.abspath(a.run), "step": a.step} if a.run else None
            lesson = book.add(a.when, a.wrong, a.right, a.by, detect=a.detect, evidence=a.evidence, source=source)
            print(f"{lesson['id']} ({lesson['status']}): shown where /{lesson['when']}/ is found")
        elif a.command == "list":
            for l in book.lessons.values():
                print(f"{l['id']}  {l['status']:<10}  when /{l['when']}/  detect "
                      + (f"/{l['detect']}/" if l.get("detect") else "-") + f"\n    wrong: {l['wrong']}\n    right: {l['right']}")
        else:
            if not a.runs:
                raise LessonError("review: give the run folders to measure the lessons on")
            for lesson, c, status, why in book.review(a.runs):
                print(f"{lesson['id']}  {status:<10}  shown {c['shown']}, repeated {c['repeated']}; mistake before the "
                      f"lesson {rate(c['before_made'], c['before'])}  ({why})")
        if book.bad_lines:
            print(f"warning: {book.bad_lines} lines of {a.file} could not be read", file=sys.stderr)
    except (LessonError, OSError) as exc:
        print(f"herdr-py lessons: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
