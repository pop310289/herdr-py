"""An organizer proposes the plan, the program checks it, and only a plan that passes the checks can run (dag.py).

    python3 -m herdr_py.organize team.json --organizer org=claude --out plans/p1 [--tries 3]
    python3 -m herdr_py.dag plans/p1/plan.json --out runs/p1

team.json (paths are relative to its folder):
    {"goal": "goal.md",                         a file, or the goal as text
     "repo": "../deck", "base": "HEAD",         optional: the steps work in clones of this repository (dag.py)
     "members": [{"name": "designer", "member": "designer=claude", "about": "lays out slides"}, ...],
     "judges":  [{"name": "looks", "command": "python3 judge.py", "about": "what it checks", "mode": "json"}, ...],
     "limits":  {"max_steps": 5, "max_turns": 8, "max_retries": 2}}

The organizer is any member (members.py: claude, codex, a program...). Its prompt holds the goal, the members it may
use (with what each is for), the judges it may use and the limits; it replies with a fenced JSON block:
    {"name": "...", "why": "...", "steps": [{"id": "draft", "member": "designer", "judge": "looks",
     "task": "everything this step's member needs to know", "needs": [], "start": "base", "retries": 1}]}
The program writes plan.json and tasks/<id>.md and checks them: dag.load_plan's rules (ids, needs, no cycle...), plus
the team's limits: only listed members and judges (the person running the team lists the judges; an organizer never
picks a judge of its own), at most max_steps steps, at most max_turns member turns counting retries (1 + retries per
step), retries at most max_retries. A refused plan goes back to the organizer with every reason, at most --tries times;
its files stay in proposal-<n>/ for review. organize.jsonl records each proposal (the reply stored by hash in
replies/), its problems, and the accepted plan's hash and the organizer's reasons. Exit codes: 0 a plan was accepted, 1 every proposal was refused, 2 bad arguments.
"""
import argparse
import collections
import hashlib
import json
import os
import re
import shlex
import shutil
import sys
import time

from . import dag
from .members import BROKEN, MemberError, Members, parse_member
from .teamkb import one_line

FENCE = re.compile(r"^[ \t]*```[ \t]*(?:json)?[ \t]*\n(.*?)^[ \t]*```[ \t]*$", re.S | re.M)
STEP_FIELDS = {"id", "member", "judge", "task", "needs", "start", "retries", "access", "timeout"}

PROMPT = """You organize a team of agents. Propose a plan: the steps, which member does each, what each step needs
from earlier steps, and which judge program decides whether each step passed.

Goal:
{goal}

Members you may use (each does one step at a time; a member can do several steps):
{members}

Judges you may use (programs; a step passes only when its judge says valid; you cannot add judges):
{judges}

Limits: at most {max_steps} steps; at most {max_turns} member turns in all, where a step costs 1 + its retries;
retries at most {max_retries} per step.
{place}
How the plan runs: a step starts only after every step it needs has passed its judge, and its member sees only the
outputs of the steps it needs. Members start with a fresh session for every step, so each task must say everything
that step's member needs to know: what to make, where (file names), and what its judge checks. Steps that do not
need each other run at the same time.

Reply with one or two sentences on why this organisation fits the goal, then one fenced JSON block:
```json
{{"name": "a short name", "why": "why this organisation", "steps": [
  {{"id": "draft", "member": "<a member above>", "judge": "<a judge above>", "task": "the full task for this step",
   "needs": [], "retries": 1}}]}}
```
Step fields: id (letters, digits, - and _), member, judge, task, needs (ids of earlier steps), retries, and{start_field}
access ("write" by default, "read" for a step that only looks).{previous}"""
PLACE_REPO = ("Every step works in its own git clone of the project. A step that needs one step continues from that "
              "step's output; a step that needs several starts at the base commit, unless its \"start\" is \"merge\" "
              "(the program merges them first) or one of them.\n")
START_FIELD = ' start ("base", "merge" or one of its needs),'


class TeamError(ValueError):
    pass


def load_team(path):
    """Read team.json; every problem at once (TeamError)."""
    folder = os.path.dirname(os.path.abspath(path))
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError) as exc:
        raise TeamError(f"{path}: {exc}")
    problems = []
    goal = raw.get("goal") if isinstance(raw, dict) else None
    if not isinstance(goal, str) or not goal.strip():
        raise TeamError(f"{path}: \"goal\" is a file name or the goal as text")
    goal_path = os.path.join(folder, goal)
    if os.path.isfile(goal_path):
        with open(goal_path, encoding="utf-8") as handle:
            goal = handle.read()
    members, judges = collections.OrderedDict(), collections.OrderedDict()
    for m in raw.get("members") or []:
        try:
            spec = parse_member(m.get("member", "")) if isinstance(m, dict) else None
        except MemberError as exc:
            problems.append(f"members: {exc}")
            continue
        if not spec or m.get("name") != spec["name"]:
            problems.append(f"members: {m!r}: give name and member, with the same name (\"designer\" and \"designer=claude\")")
            continue
        members[spec["name"]] = {"member": m["member"], "about": str(m.get("about") or ""), "spec": spec}
    for j in raw.get("judges") or []:
        if not isinstance(j, dict) or not isinstance(j.get("name"), str) or not isinstance(j.get("command"), str):
            problems.append(f"judges: {j!r}: give name and command")
            continue
        if j.get("mode", "json") not in dag.JUDGE_MODES:
            problems.append(f"judges: {j['name']}: mode is json or exit")
        judges[j["name"]] = {"command": j["command"], "about": str(j.get("about") or ""), "mode": j.get("mode", "json")}
    if not members:
        problems.append("members: list at least one")
    if not judges:
        problems.append("judges: list at least one (the organizer never chooses its own)")
    limits = dict({"max_steps": 6, "max_turns": 10, "max_retries": 2}, **(raw.get("limits") or {}))
    for key, value in limits.items():
        if key not in ("max_steps", "max_turns", "max_retries") or not isinstance(value, int) or isinstance(value, bool) or value < 0:
            problems.append(f"limits: {key} is max_steps, max_turns or max_retries, a whole number")
    repo = raw.get("repo")
    if repo is not None and (not isinstance(repo, str) or not repo.strip()):
        problems.append("repo: a path")
    if problems:
        raise TeamError("; ".join(problems))
    return {"folder": folder, "goal": goal.strip(), "members": members, "judges": judges, "limits": limits,
            "repo": os.path.normpath(os.path.join(folder, repo)) if repo else None, "base": raw.get("base", "HEAD")}


def prompt(team, problems=None):
    members = "\n".join(f"- {n} ({m['spec']['backend']}{':' + m['spec']['model'] if m['spec']['model'] else ''})"
                        + (f": {m['about']}" if m["about"] else "") for n, m in team["members"].items())
    judges = "\n".join(f"- {n}" + (f": {j['about']}" if j["about"] else "") for n, j in team["judges"].items())
    previous = ""
    if problems:
        previous = "\n\nYour previous plan was refused:\n" + "\n".join(f"- {p}" for p in problems) + \
                   "\nSend the whole plan again, fixed."
    return PROMPT.format(goal=team["goal"], members=members, judges=judges, place=PLACE_REPO if team["repo"] else "",
                         start_field=START_FIELD if team["repo"] else "", previous=previous, **team["limits"])


def parse(reply):
    """The plan in the reply's last fenced JSON block -> (dict, None) or (None, problem)."""
    blocks = FENCE.findall(reply or "")
    if not blocks:
        return None, "no fenced JSON block in the reply"
    try:
        plan = json.loads(blocks[-1])
    except ValueError as exc:
        return None, f"the JSON block does not parse: {exc}"
    if not isinstance(plan, dict) or not isinstance(plan.get("steps"), list) or not plan["steps"]:
        return None, "the JSON block is an object with a non-empty \"steps\" list"
    return plan, None


def resolved(command, folder):
    """A command whose file arguments are made absolute against the team's folder: plan.json lives elsewhere."""
    return " ".join(shlex.quote(a) for a in dag.resolve_args(command, folder))


def resolved_member(member, folder):
    spec = member["spec"]
    return f"{spec['name']}=command:{resolved(spec['command'], folder)}" if spec["backend"] == "command" else member["member"]


def build(team, plan, folder):
    """Write plan.json and tasks/ for a proposal into folder; the problems with it (empty when it may run)."""
    problems, limits = [], team["limits"]
    steps = [s for s in plan["steps"] if isinstance(s, dict)]
    if len(steps) != len(plan["steps"]):
        problems.append("every step is an object")
    if len(steps) > limits["max_steps"]:
        problems.append(f"{len(steps)} steps: at most {limits['max_steps']}")
    turns = 0
    nodes = []
    os.makedirs(os.path.join(folder, "tasks"), exist_ok=True)
    for i, s in enumerate(steps, 1):
        sid = s.get("id") if isinstance(s.get("id"), str) else f"step{i}"
        unknown = sorted(set(s) - STEP_FIELDS)
        if unknown:
            problems.append(f"step {sid}: unknown fields {', '.join(unknown)}")
        member, judge = team["members"].get(s.get("member")), team["judges"].get(s.get("judge"))
        if member is None:
            problems.append(f"step {sid}: member {s.get('member')!r} is not one of {', '.join(team['members'])}")
        if judge is None:
            problems.append(f"step {sid}: judge {s.get('judge')!r} is not one of {', '.join(team['judges'])}")
        retries = s.get("retries", 0)
        if isinstance(retries, int) and not isinstance(retries, bool):
            if retries > limits["max_retries"]:
                problems.append(f"step {sid}: {retries} retries, at most {limits['max_retries']}")
            turns += 1 + max(0, retries)
        task = s.get("task")
        if not isinstance(task, str) or len(task.strip()) < 20:
            problems.append(f"step {sid}: the task is the full text the member needs (20 characters or more)")
        if not dag.NODE_ID.match(sid):
            continue  # dag.load_plan reports it; no file is written under that name
        with open(os.path.join(folder, "tasks", sid + ".md"), "w", encoding="utf-8") as handle:
            handle.write((task or "").strip() + "\n")
        node = {"id": sid, "member": resolved_member(member, team["folder"]) if member else str(s.get("member")),
                "task": f"tasks/{sid}.md", "judge": resolved(judge["command"], team["folder"]) if judge else "-",
                "judge_mode": judge["mode"] if judge else "json", "needs": s.get("needs", []), "retries": retries}
        for key in ("start", "access", "timeout"):
            if key in s:
                node[key] = s[key]
        nodes.append(node)
    if turns > limits["max_turns"]:
        problems.append(f"{turns} member turns counting retries: at most {limits['max_turns']}")
    body = {"name": str(plan.get("name") or "plan")[:80], "nodes": nodes}
    if team["repo"]:
        body.update(repo=team["repo"], base=team["base"])
    with open(os.path.join(folder, "plan.json"), "w", encoding="utf-8") as handle:
        json.dump(body, handle, ensure_ascii=False, indent=1)
    try:
        dag.load_plan(os.path.join(folder, "plan.json"))
    except dag.PlanError as exc:
        problems += [p.strip() for p in str(exc).split(";") if p.strip()]
    return problems


class Organizer:
    def __init__(self, team, members, name, out, tries=3, timeout=900, backend=None):
        self.team, self.members, self.name, self.out, self.tries, self.timeout = team, members, name, out, tries, timeout
        self.backend = backend  # recorded in organize.jsonl, so a reader knows what kind of agent organized
        os.makedirs(os.path.join(out, "replies"), exist_ok=True)
        self.log_path = os.path.join(out, "organize.jsonl")
        if os.path.exists(self.log_path):
            raise TeamError(f"{out} already holds an organizer's work: give a new folder")

    def emit(self, kind, **fields):
        with open(self.log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(fields, t=round(time.time(), 3), kind=kind), ensure_ascii=False, sort_keys=True) + "\n")

    def run(self):
        """Ask until a proposal passes or the tries run out; returns (accepted, problems of the last proposal)."""
        problems = None
        self.emit("organize.start", organizer=self.name, backend=self.backend, members=list(self.team["members"]),
                  judges=list(self.team["judges"]), limits=self.team["limits"])
        for attempt in range(1, self.tries + 1):
            text = prompt(self.team, problems)
            began = time.time()
            try:
                reply, state = self.members.run_turn(self.name, text, timeout=self.timeout)
            except Exception as exc:  # the organizer's backend broke
                reply, state = f"({type(exc).__name__}: {exc})", "error"
            digest = hashlib.sha256((reply or "").encode("utf-8")).hexdigest()
            with open(os.path.join(self.out, "replies", digest + ".txt"), "w", encoding="utf-8") as handle:
                handle.write(reply or "")
            self.emit("organize.reply", attempt=attempt, state=state, seconds=round(time.time() - began, 2), reply=digest)
            if state != "idle":
                problems = [f"the organizer's turn ended {state}: {one_line(reply, 200)}"]
                self.emit("organize.refused", attempt=attempt, problems=problems)
                if state in BROKEN:
                    return False, problems  # the setup broke: asking again would not help
                continue
            plan, problem = parse(reply)
            if problem:
                problems = [problem]
            else:
                draft = os.path.join(self.out, f"proposal-{attempt}")
                if os.path.exists(draft):
                    shutil.rmtree(draft)
                problems = build(self.team, plan, draft)
                if not problems:
                    for name in ("plan.json", "tasks"):
                        target = os.path.join(self.out, name)
                        if os.path.isdir(target):
                            shutil.rmtree(target)
                        elif os.path.exists(target):
                            os.remove(target)
                        shutil.move(os.path.join(draft, name), target)
                    shutil.rmtree(draft)
                    with open(os.path.join(self.out, "plan.json"), encoding="utf-8") as handle:
                        body = handle.read()
                    self.emit("organize.accept", attempt=attempt, plan=hashlib.sha256(body.encode("utf-8")).hexdigest(),
                              why=one_line(str(plan.get("why") or ""), 1000), steps=[s.get("id") for s in plan["steps"]])
                    return True, []
            self.emit("organize.refused", attempt=attempt, problems=problems)
        return False, problems


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m herdr_py.organize", description=__doc__.split("\n\n")[0])
    ap.add_argument("team", help="team.json: goal, members, judges, limits")
    ap.add_argument("--organizer", required=True, metavar="NAME=BACKEND[:MODEL]", help="who proposes the plan")
    ap.add_argument("--out", required=True, metavar="DIR", help="a new folder for the plan (plan.json, tasks/, organize.jsonl)")
    ap.add_argument("--tries", type=int, default=3, help="proposals at most (default 3)")
    ap.add_argument("--timeout", type=int, default=900, metavar="S", help="the organizer's time per proposal")
    ap.add_argument("--socket", help="the herdr-py daemon's socket (an opencode organizer)")
    a = ap.parse_args(argv)
    try:
        team = load_team(a.team)
        spec = parse_member(a.organizer)
        members = Members([spec], os.path.join(os.path.abspath(a.out), "members"), socket=a.socket, sessions="fresh",
                          cwd=team["folder"])
        organizer = Organizer(team, members, spec["name"], os.path.abspath(a.out), tries=a.tries, timeout=a.timeout,
                              backend=spec["backend"])
    except (TeamError, MemberError) as exc:
        print(f"herdr-py organize: {exc}", file=sys.stderr)
        return 2
    try:
        accepted, problems = organizer.run()
    finally:
        members.close()
    if accepted:
        plan = dag.load_plan(os.path.join(os.path.abspath(a.out), "plan.json"))
        dag.print_check(plan)
        print(f"plan: {os.path.join(os.path.abspath(a.out), 'plan.json')}")
        return 0
    print("herdr-py organize: no plan was accepted: " + "; ".join(problems or []), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
