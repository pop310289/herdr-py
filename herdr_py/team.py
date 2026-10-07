"""Team runs: one task, one condition, driven through the daemon's socket API like any other client.

Conditions (the executor gets the same instructions in all of them; only what happens after it stops differs):
  S  single     the executor works until it goes idle; that is the result.
  N  nudged     after each stop the program sends the same generic reminder (re-read, self-check, finish, say DONE),
                up to `rounds` times.
  T  team       after each stop the supervisor (this program) runs the visible check. If it fails, a read-only
                verifier explains why; if it passes, a read-only validator compares the result with the original task.
                Unless the check passes and the validator accepts, the supervisor sends a "continue from here" prompt
                built from the check output, the role's evidence, the executor's NOTES.md and its recent tool log,
                up to `rounds` times. It also watches for stalls (no progress for `stall_s` while working): it aborts
                that executor session and starts a new one with a hand-off summary, so the work continues across
                sessions through NOTES.md. And it checkpoints: an executor that has worked for `checkpoint_s` without
                stopping (often spinning on the same mistake) is interrupted and checked as if it had stopped.
The supervisor never asks an LLM to summarise: every prompt it sends is assembled by code from recorded facts.
"""
import json
import os
import re
import subprocess
import time

VERDICT = re.compile(r"VERDICT:\s*(ACCEPT|REJECT)", re.I)
EVIDENCE = re.compile(r"EVIDENCE:\s*\S", re.I)
DONE = re.compile(r"\bDONE\b")
FINAL = ("idle", "aborted", "error")

EXECUTOR = """You are the executor in a small team. Work only inside the current folder.
Keep a file NOTES.md with two short sections, "Done" and "Next", and update it as you go (it is how others pick up your work).
You can check your work with: python3 check.py
Task:
{task}"""

NUDGE = """Re-read the task. Check your work yourself (run what you wrote, and python3 check.py).
If anything is missing or wrong, fix it and update NOTES.md. When the task is completely done, reply with the word DONE."""

VERIFIER = """You are the verifier. You must not edit files. The executor stopped, but the acceptance check below did not pass.
Find out why: read the relevant files and run read-only commands such as python3 check.py.
Reply with at most 5 lines, each in the form: PROBLEM: <what is wrong> | EVIDENCE: <file, line or command output>.
Task:
{task}
Acceptance check output:
{check}"""

VALIDATOR = """You are the validator. You must not edit files. The acceptance check passed, but checks can be incomplete.
Decide whether the result really does what the original task asks. Read the files you need.
End your reply with a line VERDICT: ACCEPT or VERDICT: REJECT. For REJECT add EVIDENCE: lines (file, line or command output).
Task:
{task}"""

CONTINUE = """Supervisor: the task is not finished yet. Continue from where you stopped.

Acceptance check (python3 check.py) {status}:
{check}
{opinion}
Your NOTES.md:
{notes}

What you did so far (tool log):
{log}

Fix the problems above, run python3 check.py yourself, update NOTES.md, and stop when the check passes."""

HANDOFF = """You are taking over this task from a previous executor session that got stuck. Continue its work; do not start over.

Its NOTES.md:
{notes}

Its recent tool log:
{log}

Last acceptance check output:
{check}

""" + EXECUTOR


class TeamRun:
    def __init__(self, client, task, condition, workdir, rounds=3, wall_s=720, stall_s=120, poll_s=1.0, check_timeout=120,
                 log_path=None, clock=time.time, sleep=time.sleep, checkpoint_s=240):
        if condition not in ("S", "N", "T"):
            raise ValueError("condition must be S, N or T")
        self.client, self.task, self.condition, self.workdir = client, task, condition, workdir
        self.rounds, self.wall_s, self.stall_s, self.poll_s = rounds, wall_s, stall_s, poll_s
        self.checkpoint_s = checkpoint_s
        self.check_timeout = check_timeout
        self.clock, self.sleep = clock, sleep
        self.log = open(log_path, "a", encoding="utf-8", buffering=1) if log_path else None
        self.started = None
        self.events = []          # supervisor decisions, in order
        self.checks = []          # (round, ok, output tail)
        self.executors = []       # executor agent names, in order (a stall starts a new one)

    # ------------------------------------------------------------ helpers
    def note(self, kind, **data):
        entry = {"t": round(self.clock() - self.started, 2), "kind": kind, **data}
        self.events.append(entry)
        if self.log:
            self.log.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def left(self):
        return self.wall_s - (self.clock() - self.started)

    def wait_turn(self, name, watch_stall=False):
        """Poll until the agent stops. Returns "idle"/"aborted"/"error", "stalled", "checkpoint" or "timeout"."""
        last_sig, last_change, turn_start = None, self.clock(), self.clock()
        while True:
            view = self.client.call("agent.get", name=name)
            if view["state"] in FINAL and view["followups_left"] == 0:
                return view["state"]
            sig = (view["state"], view["tokens"], len(view["stream"]["text"]), view["stream"]["text"][-40:],
                   tuple(a["t"] for a in view["activity"]), len(view["pending"]))
            if sig != last_sig:
                last_sig, last_change = sig, self.clock()
            elif watch_stall and view["state"] in ("working", "retry", "starting") and self.clock() - last_change > self.stall_s:
                return "stalled"
            if watch_stall and self.checkpoint_s and view["state"] in ("working", "retry") and self.clock() - turn_start > self.checkpoint_s:
                return "checkpoint"
            if self.left() <= 0:
                return "timeout"
            self.sleep(self.poll_s)

    def run_check(self, round_no):
        cmd = self.task.get("check") or ["python3", "check.py"]
        cmd = cmd.split() if isinstance(cmd, str) else cmd
        try:
            p = subprocess.run(cmd, cwd=self.workdir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               universal_newlines=True, timeout=self.check_timeout)
            ok, out = p.returncode == 0, p.stdout
        except subprocess.TimeoutExpired:
            ok, out = False, f"(the check did not finish within {self.check_timeout}s)"
        except OSError as exc:
            ok, out = False, f"(could not run the check: {exc})"
        tail = "\n".join(out.strip().splitlines()[-30:]) or "(no output)"
        self.checks.append((round_no, ok, tail))
        self.note("check", round=round_no, ok=ok, output=tail[-600:])
        return ok, tail

    def last_reply(self, name):
        texts = [m["text"] for m in self.client.call("agent.read", name=name, limit=6)["messages"]
                 if m["kind"] == "text" and m["role"] == "assistant"]
        return texts[-1].strip() if texts else ""

    def tool_log(self, names, limit=10):
        lines = []
        for name in names:
            for m in self.client.call("agent.read", name=name, limit=40)["messages"]:
                if m["kind"] != "tool":
                    continue
                inp = m.get("input") or {}
                if m["tool"] in ("write", "edit"):
                    lines.append(f"- {m['tool']} {inp.get('filePath', '?')} ({m.get('status')})")
                elif m["tool"] == "bash":
                    out = (m.get("output") or "").strip().splitlines()
                    lines.append(f"- ran `{inp.get('command', '?')}` -> {m.get('status')}: {(out[-1] if out else '')[:160]}")
                else:
                    lines.append(f"- {m['tool']} ({m.get('status')})")
        return "\n".join(lines[-limit:]) or "(no tool calls)"

    def notes(self):
        path = os.path.join(self.workdir, "NOTES.md")
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                text = handle.read().strip()
        except OSError:
            return "(NOTES.md does not exist)"
        return text[-1500:] or "(NOTES.md is empty)"

    def start_executor(self, prompt):
        name = "exec" if not self.executors else f"exec{len(self.executors) + 1}"
        self.executors.append(name)
        self.client.call("agent.start", name=name, prompt=prompt)
        self.note("start", agent=name)
        return name

    def ask_role(self, role, prompt):
        """Start (or re-prompt) a read-only role and return its final reply."""
        names = {a["name"] for a in self.client.call("agent.list")["agents"]}
        if role in names:
            self.client.call("agent.prompt", name=role, text=prompt)
        else:
            self.client.call("agent.start", name=role, prompt=prompt)
        state = self.wait_turn(role)
        reply = self.last_reply(role)
        self.note("role", agent=role, state=state, reply=reply[-800:])
        return state, reply

    # ------------------------------------------------------------ conditions
    def run(self):
        self.started = self.clock()
        task_text = self.task["prompt"]
        exec_name = self.start_executor(EXECUTOR.format(task=task_text))
        outcome = None
        state = self.wait_turn(exec_name, watch_stall=self.condition == "T")
        if self.condition == "S":
            outcome = "single-finished" if state in FINAL else state
        elif self.condition == "N":
            for i in range(1, self.rounds + 1):
                if state in ("timeout",):
                    break
                if DONE.search(self.last_reply(exec_name)) and i > 1:
                    outcome = "said-done"
                    break
                self.client.call("agent.prompt", name=exec_name, text=NUDGE)
                self.note("nudge", round=i)
                state = self.wait_turn(exec_name)
            outcome = outcome or ("rounds-used" if state != "timeout" else "timeout")
        else:
            outcome = self.supervise(exec_name, state, task_text)
        if self.left() <= 0:
            for a in self.client.call("agent.list")["agents"]:
                if a["state"] not in FINAL:
                    self.client.call("agent.abort", name=a["name"], reason="wall-clock limit")
        final_ok, _ = self.run_check("final")
        agents = self.client.call("agent.list")["agents"]
        summary = {"condition": self.condition, "task": self.task.get("name"), "outcome": outcome,
                   "seconds": round(self.clock() - self.started, 1), "final_check": final_ok,
                   "first_check": next((ok for r, ok, _ in self.checks if r == 1), None),
                   "checks": [{"round": r, "ok": ok} for r, ok, _ in self.checks],
                   "tokens": sum(a["tokens"] for a in agents), "tokens_by_agent": {a["name"]: a["tokens"] for a in agents},
                   "executors": self.executors,
                   "interventions": [e for e in self.events if e["kind"] in ("feedback", "nudge", "stall", "checkpoint")]}
        self.note("summary", **{k: v for k, v in summary.items() if k != "interventions"})
        if self.log:
            self.log.close()
        return summary

    def supervise(self, exec_name, state, task_text):
        for round_no in range(1, self.rounds + 2):
            if state == "timeout" or self.left() <= 0:
                return "timeout"
            if state == "checkpoint":
                self.client.call("agent.abort", name=exec_name, reason="checkpoint")
                self.note("checkpoint", agent=exec_name)
                state = "idle"  # checked below exactly like a stop
            if state == "stalled":
                self.client.call("agent.abort", name=exec_name, reason="stalled")
                check = self.checks[-1][2] if self.checks else "(not run yet)"
                self.note("stall", agent=exec_name)
                exec_name = self.start_executor(HANDOFF.format(notes=self.notes(), log=self.tool_log(self.executors), check=check, task=task_text))
                state = self.wait_turn(exec_name, watch_stall=True)
                continue
            ok, check = self.run_check(round_no)
            if ok:
                role_state, reply = self.ask_role("val", VALIDATOR.format(task=task_text))
                verdict = VERDICT.findall(reply)
                rejected = bool(verdict) and verdict[-1].upper() == "REJECT" and EVIDENCE.search(reply) is not None
                self.note("verdict", agent="val", verdict=verdict[-1].upper() if verdict else "none", counted=rejected)
                if not rejected:
                    return "accepted"  # a REJECT without evidence is only an opinion (definitions §11.1)
                opinion = f"\nThe validator rejected the result:\n{reply[-1200:]}\n"
            else:
                role_state, reply = self.ask_role("ver", VERIFIER.format(task=task_text, check=check))
                problems = [l for l in reply.splitlines() if "PROBLEM:" in l.upper() and "EVIDENCE:" in l.upper()]
                opinion = ("\nThe verifier found:\n" + "\n".join(problems[:5]) + "\n") if problems else ""
            if round_no > self.rounds:
                return "rounds-used"
            prompt = CONTINUE.format(status="passed" if ok else "FAILED", check=check, opinion=opinion,
                                     notes=self.notes(), log=self.tool_log(self.executors))
            self.client.call("agent.prompt", name=exec_name, text=prompt)
            self.note("feedback", round=round_no, check_ok=ok, with_opinion=bool(opinion.strip()))
            state = self.wait_turn(exec_name, watch_stall=True)
        return "rounds-used"


def load_task(path):
    from .policy import load_config
    task = load_config(path)
    for key in ("name", "prompt"):
        if key not in task:
            raise ValueError(f"{path}: missing {key!r}")
    return task
