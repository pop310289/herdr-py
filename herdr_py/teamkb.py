"""Team knowledge base: what the members of a team found, judged by a program and shared with the team.

    python3 -m herdr_py.teamkb FOLDER                  # entries, verdicts, adoption and duplicates (--json for programs)
    python3 -m herdr_py.teamkb FOLDER --brief drawA    # what the program would show drawA in its next prompt

Members propose entries (a result, a method, a failure or a note, with an optional artifact and the entries it builds
on); only the program judges them, with a check that returns valid / invalid and a score. A member's own claimed score is
kept but never used as a score. Everything is an event appended to FOLDER/events.jsonl and artifacts are stored by
content hash under FOLDER/artifacts, so:
- the same entry sent twice (a member killed after submitting, then retried) gets the same id and is recorded once;
- a verdict is bound to the artifact's hash: a file changed after it was proposed is not judged as if it were the same;
- every brief records which entries it showed to whom, so adoption can be traced to what a member was shown;
- entries can be private ("private" scope: only their author sees them), for teams whose members must not share;
- a team can keep a shared todo list in the same log (engine.py): a todo is added, taken by one member at a time, then
  ended (done or failed, with the entry it produced) or dropped; taking chooses and records under the file lock, so two
  members never take the same todo. A todo can come after others: it cannot be taken until they have ended. A review
  todo is never taken by whoever made what it reviews (the members of its parents, whoever took the todos it comes
  after): nobody reviews their own work.
Several threads or processes may write at once: appends take a file lock and read what others wrote first.
Standard library only; Python 3.6+.
"""
import argparse
import collections
import hashlib
import json
import math
import os
import sys
import threading
import time

try:
    import fcntl
except ImportError:  # not POSIX: one process at a time
    fcntl = None

KINDS = ("result", "method", "failure", "note")
TODO_STATES = ("open", "taken", "done", "failed", "dropped")
STATUSES = ("valid", "invalid", "infra_error")
SCOPES = ("team", "private")
MAX_SUMMARY = 4000


class TeamKBError(ValueError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


class TeamKB:
    def __init__(self, folder):
        self.folder = os.path.abspath(folder)
        os.makedirs(os.path.join(self.folder, "artifacts"), exist_ok=True)
        self.path = os.path.join(self.folder, "events.jsonl")
        self.lock_path = os.path.join(self.folder, ".lock")
        self.lock = threading.RLock()
        self.offset = 0
        self.bad_lines = 0
        self.proposals = collections.OrderedDict()  # id -> the proposal event
        self.verdicts = {}                           # id -> the latest verdict event
        self.reads = []                              # brief events: who was shown which entries
        self.todos = collections.OrderedDict()       # todo id -> its current state (the todo events, applied in order)
        self.version = 0                             # events applied so far: the state a prompt was built from
        self.double_takes = 0                        # a todo taken while not open: must stay 0
        with self.lock:
            self._catch_up()

    # ---- the log
    def _catch_up(self):
        """Apply events written since the last look (by this object, another one or another process)."""
        if not os.path.exists(self.path):
            return
        with open(self.path, "rb") as handle:
            handle.seek(self.offset)
            chunk = handle.read()
        end = chunk.rfind(b"\n") + 1  # a line still being written is left for next time
        for raw in chunk[:end].splitlines():
            try:
                event = json.loads(raw.decode("utf-8"))
            except ValueError:
                self.bad_lines += 1
                continue
            self._apply(event)
        self.offset += end

    def _apply(self, event):
        kind = event.get("type")
        self.version += 1
        if kind == "propose" and event.get("id") not in self.proposals:
            self.proposals[event["id"]] = event
        elif kind == "verdict" and event.get("id") in self.proposals:
            self.verdicts[event["id"]] = event
        elif kind == "read":
            self.reads.append(event)
        elif kind == "todo":
            self._apply_todo(event)

    def _apply_todo(self, event):
        op, tid = event.get("op"), event.get("id")
        todo = self.todos.get(tid)
        if op == "add" and todo is None:
            self.todos[tid] = {"id": tid, "text": event.get("text"), "for": event.get("for"), "parents": event.get("parents") or [],
                               "after": event.get("after") or [], "review": bool(event.get("review")),
                               "by": event.get("by"), "t": event.get("t"), "state": "open", "taken_by": None, "takes": 0,
                               "entry": None, "status": None, "score": None, "detail": None}
        elif todo is None:
            return
        elif op == "take":
            if todo["state"] != "open":
                self.double_takes += 1
            todo.update(state="taken", taken_by=event.get("member"), takes=todo["takes"] + 1, taken_t=event.get("t"))
        elif op == "end" and todo["state"] == "taken":
            todo.update(state="done" if event.get("outcome") == "done" else "failed", entry=event.get("entry"),
                        status=event.get("status"), score=event.get("score"), detail=event.get("detail"), ended_t=event.get("t"))
        elif op == "drop" and todo["state"] == "open":
            todo.update(state="dropped", dropped_by=event.get("by"))

    def _write(self, event, unless=None):
        """Append one event under the file lock, after reading what others appended; unless(): skip when it says so.
        event may be a function: it is called under the lock, after catching up, and returns the event or None."""
        with self.lock:
            with open(self.lock_path, "a") as lock:
                if fcntl:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    self._catch_up()
                    if unless is not None and unless():
                        return False
                    if callable(event):
                        event = event()
                        if event is None:
                            return None
                    line = (json.dumps(event, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
                    with open(self.path, "ab") as handle:
                        handle.write(line)
                        handle.flush()
                        os.fsync(handle.fileno())
                    self._catch_up()
                    return event
                finally:
                    if fcntl:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _store(self, data, name):
        """Keep the artifact under its content hash; the same bytes are stored once."""
        sha = digest(data)
        ext = os.path.splitext(name or "")[1][:12] if name else ""
        rel = os.path.join("artifacts", sha + ext)
        path = os.path.join(self.folder, rel)
        if not os.path.exists(path):
            tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(tmp, "wb") as handle:
                handle.write(data)
            os.replace(tmp, path)
        return sha, rel

    # ---- what members do
    def propose(self, member, kind, summary, artifact=None, name=None, path=None, parents=(), round=None, session=None,
                claim=None, scope="team"):
        """Record a member's entry and return its id. The artifact is bytes or text (artifact=, with an optional file
        name for its extension) or a file to copy (path=; never guessed from text, which may be a model's output).
        The same member, kind, artifact and summary give the same id: recorded once."""
        if not isinstance(member, str) or not member.strip():
            raise TeamKBError("member: a nonempty name")
        if kind not in KINDS:
            raise TeamKBError(f"kind: one of {', '.join(KINDS)}")
        if not isinstance(summary, str) or not summary.strip():
            raise TeamKBError("summary: say in a sentence what this is")
        if len(summary) > MAX_SUMMARY:
            raise TeamKBError(f"summary: at most {MAX_SUMMARY} characters; put the rest in the artifact")
        if scope not in SCOPES:
            raise TeamKBError(f"scope: one of {', '.join(SCOPES)}")
        if artifact is not None and path is not None:
            raise TeamKBError("give the artifact as artifact= or path=, not both")
        parents = list(parents or ())
        sha = rel = None
        if path is not None:
            name = name or os.path.basename(path)
            with open(path, "rb") as handle:
                artifact = handle.read()
        if artifact is not None:
            data = artifact.encode("utf-8") if isinstance(artifact, str) else bytes(artifact)
            sha, rel = self._store(data, name)
        eid = "k" + digest(f"{member}\0{kind}\0{sha}\0{summary}".encode("utf-8"))[:12]
        with self.lock:
            self._catch_up()
            missing = [p for p in parents if p not in self.proposals]
            if missing:
                raise TeamKBError(f"parents: no entry {', '.join(missing)} (build only on entries that exist)")
            event = {"type": "propose", "id": eid, "t": round_t(), "member": member, "session": session, "round": round,
                     "kind": kind, "summary": summary, "artifact": rel, "sha": sha, "parents": parents, "claim": claim,
                     "scope": scope}
            self._write(event, unless=lambda: eid in self.proposals)
        return eid

    # ---- the shared todo list (the program writes it: the planner's todos after checking them, members' takes and ends)
    def add_todo(self, text, for_member=None, parents=(), by=None, wake=None, after=(), review=False):
        """Add an open todo and return its id. parents: entries the todo builds on (they must exist); after: todos that
        must have ended (done, failed or dropped) before this one can be taken (they must exist); review: it reviews
        other work, so whoever made that never takes it (authors())."""
        if not isinstance(text, str) or not text.strip():
            raise TeamKBError("todo: say what to do")
        if len(text) > MAX_SUMMARY:
            raise TeamKBError(f"todo: at most {MAX_SUMMARY} characters")
        parents, after = list(parents or ()), list(after or ())
        with self.lock:
            self._catch_up()
            missing = [p for p in parents if p not in self.proposals]
            if missing:
                raise TeamKBError(f"parents: no entry {', '.join(missing)}")
            unknown = [a for a in after if a not in self.todos]
            if unknown:
                raise TeamKBError(f"after: no todo {', '.join(unknown)}")
            tid = "t" + digest(f"{len(self.todos)}\0{text}\0{for_member}\0{round_t()}".encode("utf-8"))[:12]
            event = {"type": "todo", "op": "add", "id": tid, "t": round_t(), "text": text, "for": for_member,
                     "parents": parents, "by": by, "wake": wake, "after": after}
            if review:
                event["review"] = True
            self._write(event)
        return tid

    def take_todo(self, member, turn=None):
        """Take the oldest open todo meant for this member or for anyone: chosen and recorded under the file lock, so
        no two members take the same one. Returns the todo, or None when there is none."""
        def choose():
            for todo in self.todos.values():
                if self._takeable(todo, member):
                    return {"type": "todo", "op": "take", "id": todo["id"], "t": round_t(), "member": member, "turn": turn}
            return None
        event = self._write(choose)
        return dict(self.todos[event["id"]]) if event else None

    def _takeable(self, todo, member):
        return (todo["state"] == "open" and todo["for"] in (None, member)
                and all(self.todos[a]["state"] in ("done", "failed", "dropped") for a in todo.get("after") or () if a in self.todos)
                and member not in self._authors(todo))

    def _authors(self, todo):
        """Who made what a review todo reviews: the members of its parents and whoever took the todos it comes after
        (as far as known now). Empty for a todo that is not a review."""
        if not todo.get("review"):
            return set()
        return ({self.proposals[p]["member"] for p in todo["parents"] if p in self.proposals}
                | {self.todos[a]["taken_by"] for a in todo.get("after") or () if a in self.todos and self.todos[a]["taken_by"]})

    def takeable(self, member):
        """The ids of the todos this member could take now."""
        with self.lock:
            self._catch_up()
            return [t["id"] for t in self.todos.values() if self._takeable(t, member)]

    def end_todo(self, tid, member, outcome, entry=None, status=None, score=None, detail=None):
        """End a taken todo: outcome "done" (a valid answer) or "failed" (anything else), with what it produced."""
        if outcome not in ("done", "failed"):
            raise TeamKBError("outcome: done or failed")
        self._write({"type": "todo", "op": "end", "id": tid, "t": round_t(), "member": member, "outcome": outcome,
                     "entry": entry, "status": status, "score": score, "detail": None if detail is None else str(detail)[:300]})

    def drop_todo(self, tid, by=None):
        """Drop an open todo (a taken one runs to its end). Returns whether it was dropped."""
        def choose():
            todo = self.todos.get(tid)
            return {"type": "todo", "op": "drop", "id": tid, "t": round_t(), "by": by} if todo and todo["state"] == "open" else None
        return bool(self._write(choose))

    def todo_list(self):
        """Every todo, with "not_for": the members a review todo will not be given to (its authors so far)."""
        with self.lock:
            self._catch_up()
            return [dict(t, not_for=sorted(self._authors(t))) for t in self.todos.values()]

    # ---- what only the program does
    def judge(self, eid, check, judge="check"):
        """Run the program's check on an entry and record the verdict. check(artifact_path or None) returns
        (status, score, detail) with status valid / invalid. The artifact is hashed again first: bytes that changed since
        the proposal are not judged. A check that raises, or returns something else, is infra_error, not invalid."""
        with self.lock:
            self._catch_up()
            entry = self.proposals.get(eid)
        if entry is None:
            raise TeamKBError(f"no entry {eid}")
        path = os.path.join(self.folder, entry["artifact"]) if entry.get("artifact") else None
        status, score, detail = "infra_error", None, ""
        try:
            if path is not None:
                with open(path, "rb") as handle:
                    if digest(handle.read()) != entry["sha"]:
                        raise RuntimeError("the artifact changed since it was proposed")
            got = check(path)
            status, score, detail = got if isinstance(got, (tuple, list)) and len(got) == 3 else ("?", None, repr(got))
            if status not in ("valid", "invalid"):
                status, score, detail = "infra_error", None, f"the check returned {status!r}"
            elif status == "valid" and not (isinstance(score, (int, float)) and not isinstance(score, bool) and math.isfinite(score)):
                status, score, detail = "infra_error", None, f"a valid verdict needs a finite score, not {score!r}"
        except Exception as exc:  # the judge failed, not the entry
            status, score, detail = "infra_error", None, f"{type(exc).__name__}: {exc}"
        if status != "valid":
            score = None
        self._write({"type": "verdict", "id": eid, "t": round_t(), "status": status, "score": score, "judge": judge,
                     "detail": str(detail)[:1000], "sha": entry.get("sha")})
        return status, score

    # ---- reading
    def __len__(self):
        with self.lock:
            self._catch_up()
            return len(self.proposals)

    def verdict(self, eid):
        """(status, score) of an entry's latest verdict, or None when it has not been judged."""
        with self.lock:
            self._catch_up()
            v = self.verdicts.get(eid)
        return (v["status"], v["score"]) if v else None

    def entries(self):
        """Every entry with its latest verdict, who built on it, whether it repeats an earlier one, and how often shown."""
        with self.lock:
            self._catch_up()
            proposals, verdicts, reads = list(self.proposals.values()), dict(self.verdicts), list(self.reads)
        first_by_sha, adopted, shown = {}, collections.defaultdict(list), collections.Counter()
        for p in proposals:
            for parent in p["parents"]:
                adopted[parent].append(p["id"])
        for r in reads:
            shown.update(r.get("ids") or [])
        out = []
        for p in proposals:
            v = verdicts.get(p["id"]) or {}
            dup = first_by_sha.get(p["sha"]) if p["sha"] else None
            if p["sha"] and dup is None:
                first_by_sha[p["sha"]] = p["id"]
            out.append(dict(p, status=v.get("status", "unjudged"), score=v.get("score"), judge=v.get("judge"),
                            detail=v.get("detail", ""), adopted_by=adopted.get(p["id"], []), duplicate_of=dup,
                            shown=shown.get(p["id"], 0)))
        return out

    def visible(self, member, entry):
        return entry["scope"] == "team" or entry["member"] == member

    def brief(self, member, results=3, failures=3, round=None, record=True, answer_bytes=0, with_ids=False):
        """The text for a member's next prompt: the best verified results and the latest distinct failures it may see
        (never claimed scores). answer_bytes > 0 also shows each result's artifact (text up to that size), so the member
        can build on it. Recorded, so later entries can be traced to what this member was shown; a person looking
        (record=False) is not. with_ids=True returns (text, ids shown)."""
        seen = [e for e in self.entries() if self.visible(member, e)]
        best = sorted((e for e in seen if e["status"] == "valid" and e["kind"] in ("result", "method")),
                      key=lambda e: (-e["score"], e["t"]))[:results]
        fails, said = [], set()
        for e in reversed(seen):
            if (e["kind"] == "failure" or e["status"] == "invalid") and e["summary"] not in said:
                said.add(e["summary"])
                fails.append(e)
            if len(fails) == failures:
                break
        lines = []
        if best:
            lines.append("Verified results so far (best first; build on one by naming its id as a parent):")
            for e in best:
                lines.append(f"- {e['id']} by {e['member']}: score {e['score']:.10g}: {one_line(e['summary'])}")
                if answer_bytes:
                    lines += self.answer_lines(e, answer_bytes)
        if fails:
            lines.append("Tried and failed (do not repeat these):")
            lines += [f"- {e['id']} by {e['member']}: {one_line(e['summary'])}"
                      + (f" ({one_line(e['detail'], 160)})" if e["detail"] else "") for e in fails]
        if not lines:
            lines.append("Nothing verified or failed yet.")
        ids = [e["id"] for e in best + fails]
        if record:
            self._write({"type": "read", "t": round_t(), "member": member, "round": round, "ids": ids})
        text = "\n".join(lines)
        return (text, ids) if with_ids else text

    def answer_lines(self, entry, limit):
        """An entry's artifact as a fenced block for a brief, or a line saying why it is not shown."""
        if not entry.get("artifact"):
            return []
        with open(os.path.join(self.folder, entry["artifact"]), "rb") as handle:
            data = handle.read(limit + 1)
        if len(data) > limit:
            return [f"  (its answer is longer than {limit} bytes, so it is not shown here)"]
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return [f"  (its answer is binary, {len(data)} bytes)"]
        fence = "~~~~" if "```" in text else "```"
        return [fence, text.rstrip("\n"), fence]

    def stats(self):
        """The §18.2 numbers, computed from the log."""
        ents = self.entries()
        by_id = {e["id"]: e for e in ents}
        valid = [e for e in ents if e["status"] == "valid"]
        foreign = [e for e in valid if any(by_id[p]["member"] != e["member"] for p in e["parents"])]
        improved = [e for e in foreign if beats_parents(e, by_id)]
        adopted = [e for e in valid if any(by_id[c]["member"] != e["member"] for c in e["adopted_by"])]
        best = {}
        for e in valid:
            if e["member"] not in best or e["score"] > best[e["member"]]:
                best[e["member"]] = e["score"]
        count = collections.Counter(e["status"] for e in ents)
        return {"entries": len(ents), "valid": count["valid"], "invalid": count["invalid"], "infra_error": count["infra_error"],
                "unjudged": count["unjudged"], "duplicates": sum(1 for e in ents if e["duplicate_of"]),
                "duplicate_rate": ratio(sum(1 for e in ents if e["duplicate_of"]), len(ents)),
                "adoption_rate": ratio(len(adopted), len(valid)), "improved_after_adoption": ratio(len(improved), len(foreign)),
                "best": max((e["score"] for e in valid), default=None), "best_by_member": best,
                "briefs": len(self.reads), "bad_lines": self.bad_lines}


def beats_parents(entry, by_id):
    """Scored higher than every parent that has a score (and there is at least one)."""
    scores = [by_id[p]["score"] for p in entry["parents"] if by_id[p]["score"] is not None]
    return bool(scores) and entry["score"] > max(scores)


def round_t():
    return round(time.time(), 3)


def ratio(a, b):
    return round(a / b, 4) if b else None


def one_line(text, n=200):
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 1] + "…"


def report(kb):
    s = kb.stats()
    out = [f"{s['entries']} entries: {s['valid']} valid, {s['invalid']} invalid, {s['infra_error']} judge errors, "
           f"{s['unjudged']} unjudged; {s['duplicates']} repeat an earlier artifact; {s['briefs']} briefs given",
           f"best verified score: {s['best'] if s['best'] is not None else '-'}; adoption rate {fmt(s['adoption_rate'])}, "
           f"improved after adoption {fmt(s['improved_after_adoption'])}, duplicate rate {fmt(s['duplicate_rate'])}", ""]
    for e in kb.entries():
        flags = [f"score {e['score']:g}" if e["score"] is not None else e["status"]]
        if e["parents"]:
            flags.append("from " + ",".join(e["parents"]))
        if e["adopted_by"]:
            flags.append("used by " + ",".join(e["adopted_by"]))
        if e["duplicate_of"]:
            flags.append("repeats " + e["duplicate_of"])
        if e["scope"] != "team":
            flags.append(e["scope"])
        out.append(f"{e['id']}  {e['member']:<10} {e['kind']:<7} {'; '.join(flags)}: {one_line(e['summary'], 90)}")
    return "\n".join(out)


def fmt(value):
    return "-" if value is None else f"{value:.2f}"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m herdr_py.teamkb", description=__doc__.split("\n\n")[0])
    ap.add_argument("folder", metavar="FOLDER", help="the knowledge base folder (events.jsonl and artifacts/)")
    ap.add_argument("--json", action="store_true", help="entries and numbers as JSON")
    ap.add_argument("--brief", metavar="MEMBER", help="print what the program would show this member next (not recorded)")
    a = ap.parse_args(argv)
    if not os.path.isfile(os.path.join(a.folder, "events.jsonl")):
        print(f"herdr-py teamkb: no knowledge base in {a.folder} (events.jsonl is missing)", file=sys.stderr)
        return 2
    kb = TeamKB(a.folder)
    if a.brief:
        print(kb.brief(a.brief, record=False))
    elif a.json:
        print(json.dumps({"stats": kb.stats(), "entries": kb.entries()}, ensure_ascii=False, indent=1))
    else:
        print(report(kb))
    return 0


if __name__ == "__main__":
    sys.exit(main())
