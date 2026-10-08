"""Cooperative loops: several members work on one task in rounds, a program judges every answer, and the team
knowledge base (teamkb.py) carries the verified answers and the failures from one round to the next.

    python3 -m herdr_py.coop --task task.md --judge "python3 judge.py" --mode C --rounds 3 --out runs/c1 \\
        --member a=codex --member b=claude:haiku --member c=opencode:ollama/qwen3-8b-32k:latest --socket SOCK \\
        --member 'd=command:python3 my_loop.py'
    python3 -m herdr_py.teamkb runs/c1/kb        # what was found, judged, built on and repeated

A rule-guided loop (one agent, fixed rules, a check after every step) becomes a team loop: the rules stay as the
judge, and each member's next prompt also carries what its teammates found and the judge verified. Modes, with the
same number of member turns in each so they can be compared (definitions §17, §18):
  S  single        the first member gets every turn and sees its own earlier answers;
  I  independent   every member sees only its own earlier answers (private entries);
  C  cooperative   every member sees the team's verified answers (with the answers themselves) and failures.
Rounds are waves: every brief is built from what was judged before the round started, then the members run at the
same time, then the program judges every answer. Every prompt is assembled by code from recorded entries; no model
summarises for another, and a member can only name as parents the entries it was shown.

The judge is a command that gets the answer file as its last argument. --judge-mode json (default): it prints one
JSON line {"status": "valid" | "invalid", "score": number, "detail": "..."} and exits 0; a judge that crashes, times
out or prints anything else is a judge error, not an invalid answer. --judge-mode exit: exit code 0 passes (score 1)
and anything else fails, for a pass/fail check you already have (then a crash looks like a fail).
--stop-on-infra-error ends the run after the round in which a member's backend broke (state error or aborted: the
CLI failed, not the answer) or the judge broke; summary.json says where and why, and the command exits 3. Timeouts
and invalid or missing answers are the members' own problems and do not stop the run.
The output folder holds kb/ (the knowledge base), run.jsonl (every turn: state, seconds, tokens, entry, verdict,
problems), members/ (the backends' logs), summary.json and view.html: one page with every member's every turn
(coopview.py), written again after every round, so it can be opened while the run goes on.
"""
import argparse
import collections
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time

from . import coopview
from .members import MemberError, Members, parse_member
from .teamkb import TeamKB, one_line

MODES = ("S", "I", "C")
ID = re.compile(r"\bk[0-9a-f]{12}\b")
FENCE = re.compile(r"^[ \t]*(```|~~~~)[^\n]*\n(.*?)^[ \t]*\1[ \t]*$", re.S | re.M)

ALONE = ("You work on this task alone. Below are your own earlier answers and failures, as checked by the judge "
         "(a program, not a person). Do better than them.")
TEAM = ("You are {member}, one of {n} members working on this task at the same time. Below are the team's answers and "
        "failures so far, as checked by the judge (a program, not a person). Build on the best ones when that helps, "
        "and do not repeat what failed.")
REPLY = """Reply in this format:
SUMMARY: <one sentence: what you tried>
PARENTS: <ids of the answers above that you built on, comma separated, or none>
```
<your answer, exactly in the format the task asks for>
```
Only the judge's score counts. If you could not produce an answer, reply with one line instead:
FAILED: <what went wrong and what you learned>"""
PROMPT = "{intro} This is {unit} {wave} of {waves}.\n\nTask:\n{task}\n\n{brief}\n\n{reply}"


def labelled(text, label):
    """The text after LABEL: on its own line (markdown bold or quote marks around the label are fine)."""
    m = re.search(r"(?mi)^[ \t>*_]*" + label + r"[ \t]*:[*_]*[ \t]*(.*)$", text or "")
    return m.group(1).strip().strip("*_ ").strip() if m else None


def parse_reply(text):
    """A member's reply -> {"kind": "result" | "failure" | None, "summary", "answer", "parents"}. The answer is the last
    fenced block; without one, a FAILED line is a failure; without either, there is nothing to record."""
    blocks = [m.group(2) for m in FENCE.finditer(text or "")]
    parents = list(collections.OrderedDict.fromkeys(ID.findall(labelled(text, "PARENTS") or "")))
    if blocks:
        return {"kind": "result", "summary": labelled(text, "SUMMARY") or "(no summary given)", "answer": blocks[-1],
                "parents": parents}
    failed = labelled(text, "FAILED")
    if failed:
        return {"kind": "failure", "summary": failed, "answer": None, "parents": parents}
    return {"kind": None, "summary": None, "answer": None, "parents": []}


def command_judge(command, timeout=120):
    """A judge command that prints a JSON verdict (see the module notes)."""
    argv = shlex.split(command) if isinstance(command, str) else list(command)

    def check(path):
        if path is None:
            return "invalid", None, "no answer"
        try:
            p = subprocess.run(argv + [path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                               timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"the judge did not finish within {timeout}s")
        if p.returncode != 0:
            raise RuntimeError(f"the judge exited with code {p.returncode}: {one_line(p.stderr or p.stdout, 300)}")
        lines = [line for line in p.stdout.splitlines() if line.strip()]
        if not lines:
            raise RuntimeError("the judge printed nothing")
        verdict = json.loads(lines[-1])
        if not isinstance(verdict, dict):
            raise RuntimeError(f"the judge printed {lines[-1][:200]!r}, not a JSON object")
        return verdict.get("status"), verdict.get("score"), verdict.get("detail", "")
    return check


def exit_judge(command, timeout=120):
    """A pass/fail judge: exit code 0 is valid with score 1, anything else invalid; its output is the detail."""
    argv = shlex.split(command) if isinstance(command, str) else list(command)

    def check(path):
        if path is None:
            return "invalid", None, "no answer"
        try:
            p = subprocess.run(argv + [path], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
                               timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"the judge did not finish within {timeout}s")
        tail = "\n".join(p.stdout.strip().splitlines()[-8:])
        return ("valid", 1, tail) if p.returncode == 0 else ("invalid", None, tail or f"exit code {p.returncode}")
    return check


def used(before, after):
    return after - before if isinstance(before, int) and isinstance(after, int) else None


class CoopRun:
    """One run of one mode. members: an object with run_turn(name, prompt, timeout=...) -> (reply, state) and,
    optionally, tokens(name) (members.Members, or anything shaped like it)."""

    def __init__(self, task, judge, members, names, out, mode="C", rounds=3, results=3, failures=3, answer_bytes=6000,
                 answer_name="answer.txt", turn_timeout=900, judge_name="judge", stop_on_infra_error=False):
        if mode not in MODES:
            raise ValueError(f"mode: one of {', '.join(MODES)}")
        if not names:
            raise ValueError("no members")
        if not (isinstance(rounds, int) and rounds >= 1):
            raise ValueError("rounds: 1 or more")
        self.task, self.judge, self.members, self.names, self.mode, self.rounds = task, judge, members, list(names), mode, rounds
        self.results, self.failures, self.answer_bytes, self.answer_name = results, failures, answer_bytes, answer_name
        self.turn_timeout, self.judge_name, self.out = turn_timeout, judge_name, out
        self.stop_on_infra_error, self.stopped = stop_on_infra_error, None
        os.makedirs(out, exist_ok=True)
        self.kb = TeamKB(os.path.join(out, "kb"))
        self.scope = "team" if mode == "C" else "private"
        self.turns, self.progress = [], []
        self.log = open(os.path.join(out, "run.jsonl"), "a", encoding="utf-8", buffering=1)

    def waves(self):
        """S: one member, one turn per wave, as many turns as the team would get; I and C: every member every round."""
        if self.mode == "S":
            return [[self.names[0]] for _ in range(len(self.names) * self.rounds)]
        return [list(self.names) for _ in range(self.rounds)]

    def prompt(self, member, wave):
        brief, shown = self.kb.brief(member, self.results, self.failures, round=wave, answer_bytes=self.answer_bytes,
                                     with_ids=True)
        intro = TEAM.format(member=member, n=len(self.names)) if self.mode == "C" else ALONE
        unit = "turn" if self.mode == "S" else "round"
        return PROMPT.format(intro=intro, unit=unit, wave=wave, waves=len(self.waves()), task=self.task.strip(), brief=brief,
                             reply=REPLY), shown

    def turn(self, member, prompt, out):
        before, start = self.tokens(member), time.time()
        try:
            text, state = self.members.run_turn(member, prompt, timeout=self.turn_timeout)
        except Exception as exc:  # a member backend that breaks fails its turn; the run goes on
            text, state = f"({type(exc).__name__}: {exc})", "error"
        out[member] = (text, state, round(time.time() - start, 2), used(before, self.tokens(member)))

    def tokens(self, member):
        try:
            return self.members.tokens(member) if hasattr(self.members, "tokens") else None
        except Exception:  # counting tokens must never fail a turn
            return None

    def run(self):
        started = time.time()
        for wave, members in enumerate(self.waves(), 1):
            prompts = {m: self.prompt(m, wave) for m in members}  # built before anyone in this round runs
            replies = {}
            threads = [threading.Thread(target=self.turn, args=(m, prompts[m][0], replies)) for m in members]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            done = [self.record(m, wave, prompts[m][1], *replies[m]) for m in members]  # in member order, whoever finished first
            self.progress.append({"round": wave, "turns": len(self.turns), "best": self.kb.stats()["best"]})
            self.view()
            broken = [infra_problem(t) for t in done if infra_problem(t)]
            if broken and self.stop_on_infra_error:
                self.stopped = {"round": wave, "of": len(self.waves()), "why": broken}
                break
        self.log.close()
        summary = self.summary(time.time() - started)
        tmp = os.path.join(self.out, "summary.json.tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, ensure_ascii=False, indent=1)
        os.replace(tmp, os.path.join(self.out, "summary.json"))
        self.view()
        return summary

    def view(self):
        """Write view.html; a page that cannot be written is reported, it never ends the run."""
        try:
            coopview.save(self.out)
        except Exception as exc:  # noqa: BLE001 - the page is for people; the run's records are what count
            print(f"herdr-py coop: warning: view.html not written: {type(exc).__name__}: {exc}", file=sys.stderr)

    def record(self, member, wave, shown, text, state, seconds, tokens):
        turn = {"t": round(time.time(), 3), "round": wave, "member": member, "state": state, "seconds": seconds,
                "tokens": tokens}
        parsed = parse_reply(text) if state == "idle" else None
        if parsed is None:
            turn["problem"] = f"the turn ended {state}" + (f": {one_line(text, 300)}" if text else "")
        elif parsed["kind"] is None:
            turn["problem"] = "no fenced answer and no FAILED line"
            turn["reply_tail"] = text[-300:]
        else:
            parents = [p for p in parsed["parents"] if p in shown]
            before = len(self.kb)
            eid = self.kb.propose(member, parsed["kind"], one_line(parsed["summary"], 500), artifact=parsed["answer"],
                                  name=self.answer_name, parents=parents, round=wave, scope=self.scope)
            turn.update(entry=eid, kind=parsed["kind"], parents=parents, repeat=len(self.kb) == before)
            dropped = [p for p in parsed["parents"] if p not in shown]
            if dropped:
                turn["parents_dropped"] = dropped  # named but never shown to this member: not counted as built on
            if parsed["kind"] == "result":
                known = self.kb.verdict(eid) if turn["repeat"] else None
                status, score = known or self.kb.judge(eid, self.judge, judge=self.judge_name)
                turn.update(status=status, score=score)
        self.turns.append(turn)
        self.log.write(json.dumps(turn, ensure_ascii=False) + "\n")
        return turn

    def summary(self, seconds):
        s = self.kb.stats()
        valid = [e for e in self.kb.entries() if e["status"] == "valid"]
        best = max(valid, key=lambda e: (e["score"], -e["t"]), default=None)
        known = [t["tokens"] for t in self.turns if t["tokens"] is not None]
        summary = {"mode": self.mode, "members": self.names[:1] if self.mode == "S" else self.names, "rounds": self.rounds,
                   "turns": len(self.turns), "turn_states": dict(collections.Counter(t["state"] for t in self.turns)),
                   "answers": sum(1 for t in self.turns if t.get("kind") == "result"),
                   "failures_reported": sum(1 for t in self.turns if t.get("kind") == "failure"),
                   "repeats": sum(1 for t in self.turns if t.get("repeat")),
                   "no_answer": sum(1 for t in self.turns if t["state"] == "idle" and "problem" in t),
                   "valid": s["valid"], "invalid": s["invalid"], "judge_errors": s["infra_error"],
                   "best": best["score"] if best else None, "best_entry": best["id"] if best else None,
                   "best_member": best["member"] if best else None, "best_by_member": s["best_by_member"],
                   "adoption_rate": s["adoption_rate"], "improved_after_adoption": s["improved_after_adoption"],
                   "duplicate_rate": s["duplicate_rate"],
                   "parents_dropped": sum(len(t.get("parents_dropped", [])) for t in self.turns),
                   "progress": self.progress, "stopped": self.stopped, "seconds": round(seconds, 1),
                   "tokens": sum(known) if known else None, "tokens_known_for_turns": len(known)}
        if hasattr(self.members, "summary"):
            summary["member_stats"] = self.members.summary()
        return summary


def infra_problem(turn):
    """Why this turn shows that the setup broke (a member's backend or the judge), or None."""
    if turn["state"] in ("error", "aborted"):
        return f"{turn['member']} round {turn['round']}: the member's backend ended {turn['state']}"
    if turn.get("status") == "infra_error":
        return f"{turn['member']} round {turn['round']}: the judge failed on entry {turn['entry']}"
    return None


def report(summary, out):
    s = summary
    rate = lambda v: "-" if v is None else f"{v:.2f}"  # noqa: E731
    lines = [f"mode {s['mode']}: {s['turns']} turns ({', '.join(f'{n} {k}' for k, n in sorted(s['turn_states'].items()))}); "
             f"{s['answers']} answers: {s['valid']} valid, {s['invalid']} invalid, {s['judge_errors']} judge errors; "
             f"{s['failures_reported']} failures reported, {s['no_answer']} replies without an answer, "
             f"{s['repeats']} repeats of the member's own earlier entry",
             (f"best {s['best']:.10g} ({s['best_entry']} by {s['best_member']})" if s["best"] is not None else "no valid answer")
             + f"; adoption {rate(s['adoption_rate'])}, improved after adoption {rate(s['improved_after_adoption'])}, "
               f"duplicates {rate(s['duplicate_rate'])}, parents dropped {s['parents_dropped']}",
             "best after each round: " + ", ".join("-" if p["best"] is None else f"{p['best']:.6g}" for p in s["progress"]),
             f"tokens: {s['tokens'] if s['tokens'] is not None else 'not reported'}; {s['seconds']} s",
             f"entries: python3 -m herdr_py.teamkb {os.path.join(out, 'kb')}",
             f"every turn on one page: {os.path.join(out, 'view.html')}"]
    if s.get("stopped"):
        lines.insert(0, f"STOPPED after round {s['stopped']['round']} of {s['stopped']['of']}: " + "; ".join(s["stopped"]["why"]))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m herdr_py.coop", description=__doc__.split("\n\n")[0])
    ap.add_argument("--task", required=True, metavar="FILE", help="the task, as text the members read")
    ap.add_argument("--judge", required=True, metavar="COMMAND", help="the judge; the answer file is added as its last argument")
    ap.add_argument("--judge-mode", choices=["json", "exit"], default="json", help="how the judge reports (see above)")
    ap.add_argument("--judge-timeout", type=int, default=120, metavar="S")
    ap.add_argument("--member", action="append", default=[], metavar="NAME=BACKEND[:MODEL]",
                    help="a member: NAME=opencode|codex|claude[:MODEL] or NAME=command:COMMAND (repeat for each member)")
    ap.add_argument("--mode", choices=MODES, default="C", help="S single, I independent, C cooperative (default)")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--out", required=True, metavar="DIR", help="a new folder for this run")
    ap.add_argument("--socket", help="the herdr-py daemon's socket (opencode members)")
    ap.add_argument("--sessions", choices=["fresh", "keep"], default="fresh",
                    help="fresh (default): every turn is a new conversation; keep: members keep theirs")
    ap.add_argument("--turn-timeout", type=int, default=900, metavar="S")
    ap.add_argument("--show-results", type=int, default=3, metavar="N", help="verified answers in each brief")
    ap.add_argument("--show-failures", type=int, default=3, metavar="N", help="failures in each brief")
    ap.add_argument("--answer-bytes", type=int, default=6000, metavar="N", help="show answers up to this size (0: never)")
    ap.add_argument("--stop-on-infra-error", action="store_true",
                    help="end the run after a round in which a member's backend or the judge broke (exit code 3)")
    ap.add_argument("--answer-name", default="answer.txt", help="the answer file's name (its extension matters to some judges)")
    a = ap.parse_args(argv)
    problems = []
    if a.rounds < 1:
        problems.append("--rounds: 1 or more")
    if not a.member:
        problems.append("--member: give at least one")
    for path in ("run.jsonl", "kb", "summary.json"):
        if os.path.exists(os.path.join(a.out, path)):
            problems.append(f"--out: {a.out} already holds a run ({path}); give a new folder")
            break
    try:
        specs = [parse_member(m) for m in a.member]
        with open(a.task, encoding="utf-8") as handle:
            task = handle.read()
    except (MemberError, OSError) as exc:
        problems.append(str(exc))
    if problems:
        print("herdr-py coop: " + "; ".join(problems), file=sys.stderr)
        return 2
    try:
        members = Members(specs, os.path.join(a.out, "members"), socket=a.socket, sessions=a.sessions)
    except MemberError as exc:
        print(f"herdr-py coop: {exc}", file=sys.stderr)
        return 2
    judge = (command_judge if a.judge_mode == "json" else exit_judge)(a.judge, timeout=a.judge_timeout)
    names = members.names()
    if a.mode == "S" and len(names) > 1:
        print(f"mode S: {names[0]} gets all {len(names) * a.rounds} turns; {', '.join(names[1:])} do not run")
    run = CoopRun(task, judge, members, names, a.out, mode=a.mode, rounds=a.rounds, results=a.show_results,
                  failures=a.show_failures, answer_bytes=a.answer_bytes, answer_name=a.answer_name,
                  turn_timeout=a.turn_timeout, judge_name=a.judge, stop_on_infra_error=a.stop_on_infra_error)
    try:
        summary = run.run()
    finally:
        members.close()
    print(report(summary, a.out))
    return 3 if summary["stopped"] else 0


if __name__ == "__main__":
    sys.exit(main())
