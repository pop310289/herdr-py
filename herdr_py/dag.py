"""DAG dispatch: the steps of one plan, each done by one member in its own workspace and passed or failed by a judge
program. A step starts only when every step it needs has passed, and sees only what those steps produced.

    python3 -m herdr_py.dag plan.json --check                  # check the plan, print its steps level by level
    python3 -m herdr_py.dag plan.json --out runs/p1 [--parallel 4] [--socket SOCK]
    python3 -m herdr_py.dag plan.json --out runs/p1 --resume   # go on after a crash: passed steps are not run again
    python3 -m herdr_py.dag --recheck runs/p1                  # judge every passed step again in a fresh clone

plan.json (paths are relative to the plan's folder):
    {"name": "two-ways-then-merge",
     "repo": "../myproject",        optional: every step works in its own clone of this git repository...
     "base": "HEAD",                ...starting from this commit (resolved once, when the run starts)
     "nodes": [
       {"id": "W1", "member": "w1=codex", "task": "tasks/w1.md", "judge": "python3 judges/tests.py"},
       {"id": "W2", "member": "w2=claude", "task": "tasks/w2.md", "judge": "python3 judges/tests.py"},
       {"id": "W3", "member": "w3=claude", "task": "tasks/merge.md", "needs": ["W1", "W2"],
        "judge": "python3 judges/tests.py", "retries": 1, "timeout": 900, "access": "write", "judge_mode": "json",
        "start": "base"}]}

A member is NAME=BACKEND[:MODEL] or NAME=command:COMMAND (members.py); one name is one member, and a member does one
step at a time. With a repo, each attempt of each step gets its own clone (git clone --shared): clones share
objects, not branches, so a step's clone holds no other step's work. A step's clone also gets the output commit of
each step it needs, as refs/dag/<id>, and its prompt shows those steps' summaries and diffs: nothing from any other
step. Where the clone's files start ("start"): a step that needs nothing starts at the base commit; a step that needs
one step continues from that step's output; a step that needs several starts at the base, and the member compares and
combines them (two ways of doing one thing usually touch the same files). "start" can name one of the needs, "base",
or "merge": the program merges the needs' outputs first, and a conflict fails the step, naming the files. Codex members work there in the workspace-write sandbox, Claude members with file tools only (their
modules say what the real CLIs refuse); opencode members cannot work in a given folder yet. When the member's turn
ends, the program commits everything in the clone; that commit is the step's output and what the judge checks (a
step with "access": "read" keeps no changes: its output is its start commit and its reply). Without a repo the reply
is the output and members work where they always do.

The judge follows coop.py's contract: the reply file is its last argument, it runs in the step's clone (in replies/
without a repo) with HERDR_DAG_* telling it the step, the commit and what the step built on; judge_mode json: it prints
{"status": "valid" | "invalid", "score": ..., "detail": ...} (a crash is a judge error, not a fail); exit: exit code 0
passes. Files named in a judge command resolve against the plan's folder, outside every clone, so a member cannot edit
the judge it is checked by; they are hashed when the run starts and checked before every verdict.

An attempt that fails (invalid, or the member timed out) is tried again while retries last, from the failed attempt's
commit and with the judge's words in the prompt. A step whose need failed is blocked. When the setup breaks (a member
backend ends error or aborted, the judge fails, a clone cannot be made) no new step starts (--keep-going: only that
step fails); --resume runs such a step again. Every dispatch, return, commit and verdict is written to events.jsonl as
it happens; --resume rebuilds the state from it, and refuses when the plan, a task or a judge file changed.
summary.json counts what the invariants need (agent-cluster definitions §21): dispatches made before every need had
passed and passed steps dispatched again (both must be 0, worked out from events.jsonl alone), member tool events that
name another step's workspace (must be 0 where a backend logs them: codex, claude), and how parallel the run was.
Exit codes: 0 every step passed, 1 some did not, 2 a bad plan or arguments, 3 stopped because the setup broke.
"""
import argparse
import collections
import concurrent.futures
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time

from . import dagview
from .coop import command_judge, exit_judge
from .members import MemberError, Members, parse_member
from .teamkb import one_line

NODE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
FIELDS = {"id", "member", "task", "judge", "needs", "retries", "timeout", "judge_timeout", "access", "judge_mode", "start"}
ACCESS = ("write", "read")
JUDGE_MODES = ("json", "exit")
GIT_ID = {"GIT_AUTHOR_NAME": "herdr-py dag", "GIT_AUTHOR_EMAIL": "dag@herdr-py.invalid",
          "GIT_COMMITTER_NAME": "herdr-py dag", "GIT_COMMITTER_EMAIL": "dag@herdr-py.invalid"}
OUT_BRANCH = "dag-out"  # the branch a step's output commit is on, in the step's clone

PROMPT = """You are {member}, doing step {node} of the plan "{plan}".{position}

Task:
{task}
{upstream}{retry}
{place}
Reply with a short summary of what you did and what the next steps should know."""
PLACE_WRITE = ("Your folder is a git clone made for this step only, at commit {start} ({origin}). Change the files there. You do "
               "not need to commit: when your turn ends, everything in the folder is committed for you, and a judge "
               "program checks that commit. Only the judge's verdict counts.")
PLACE_READ = ("Your folder is a git clone made for this step only, at commit {start} ({origin}). Read what you need there; changes "
              "are not kept. A judge program checks your reply. Only the judge's verdict counts.")
PLACE_TEXT = "Your reply is your result: a judge program checks it. Only the judge's verdict counts."


class PlanError(ValueError):
    pass


class WorkspaceError(RuntimeError):
    pass


class MergeConflict(Exception):
    """The outputs a step should start from do not merge: the plan's problem, not a broken setup."""


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def git(*args, env=None):
    try:
        p = subprocess.run(["git"] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                           env=env)
    except FileNotFoundError:
        raise WorkspaceError("git is not installed")
    if p.returncode != 0:
        words = [a for a in args if not a.startswith("-") and "/" not in a][:2]
        raise WorkspaceError(f"git {' '.join(words)}: {one_line(p.stderr or p.stdout, 300)}")
    return p.stdout.strip()


def has_commit(folder, sha):
    return bool(sha) and os.path.isdir(folder) and subprocess.run(
        ["git", "-C", folder, "cat-file", "-e", sha + "^{commit}"], stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL).returncode == 0


def find_cycle(nodes, order):
    """One cycle in the needs as a list of ids (first id repeated at the end), or None."""
    color, stack = {n: 0 for n in order}, []

    def visit(n):
        color[n] = 1
        stack.append(n)
        for m in nodes[n]["needs"]:
            if color.get(m) == 1:
                return stack[stack.index(m):] + [m]
            if color.get(m) == 0:
                found = visit(m)
                if found:
                    return found
        stack.pop()
        color[n] = 2
        return None

    for n in order:
        if color[n] == 0:
            found = visit(n)
            if found:
                return found
    return None


def levels(plan):
    """{id: level}: 0 for a step that needs nothing, else one more than the deepest step it needs."""
    out = {}

    def level(n):
        if n not in out:
            out[n] = 1 + max([level(m) for m in plan["nodes"][n]["needs"]] or [-1])
        return out[n]

    for n in plan["order"]:
        level(n)
    return out


def resolve_args(command, folder):
    """A command as arguments; an argument naming a file in the plan's folder becomes its absolute path (judges and
    command members run inside a step's clone, where a relative path would mean something else)."""
    return [os.path.join(folder, a) if not os.path.isabs(a) and os.path.isfile(os.path.join(folder, a)) else a
            for a in shlex.split(command)]


def load_plan(path):
    """Read and check a plan; every problem is reported at once (PlanError)."""
    folder = os.path.dirname(os.path.abspath(path))
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError) as exc:
        raise PlanError(f"{path}: {exc}")
    if not isinstance(raw, dict) or not isinstance(raw.get("nodes"), list) or not raw["nodes"]:
        raise PlanError(f"{path}: a JSON object with a non-empty \"nodes\" list")
    problems, nodes, order, given_start = [], {}, [], {}
    for i, n in enumerate(raw["nodes"], 1):
        if not isinstance(n, dict):
            problems.append(f"node {i}: an object")
            continue
        nid = n.get("id")
        if not isinstance(nid, str) or not NODE_ID.match(nid):
            problems.append(f"node {i}: the id is letters, digits, - and _, starting with a letter")
            continue
        if nid in nodes:
            problems.append(f"node {nid}: two nodes have this id")
            continue
        where = f"node {nid}"
        unknown = sorted(set(n) - FIELDS)
        if unknown:
            problems.append(f"{where}: unknown fields {', '.join(unknown)}")
        for key in ("member", "task", "judge"):
            if not isinstance(n.get(key), str) or not n[key].strip():
                problems.append(f"{where}: {key} is required")
        member = None
        if isinstance(n.get("member"), str):
            try:
                member = parse_member(n["member"])
            except MemberError as exc:
                problems.append(f"{where}: {exc}")
        task = os.path.join(folder, n["task"]) if isinstance(n.get("task"), str) and n["task"].strip() else None
        if task and not os.path.isfile(task):
            problems.append(f"{where}: task file {n['task']} not found")
        needs = n.get("needs", [])
        if not isinstance(needs, list) or not all(isinstance(x, str) for x in needs):
            problems.append(f"{where}: needs is a list of node ids")
            needs = []
        numbers = {}
        for key, default, least in (("retries", 0, 0), ("timeout", 900, 1), ("judge_timeout", 600, 1)):
            value = n.get(key, default)
            if not isinstance(value, int) or isinstance(value, bool) or value < least:
                problems.append(f"{where}: {key} is a whole number, {least} or more")
                value = default
            numbers[key] = value
        access, mode = n.get("access", "write"), n.get("judge_mode", "json")
        if access not in ACCESS:
            problems.append(f"{where}: access is write or read")
        if mode not in JUDGE_MODES:
            problems.append(f"{where}: judge_mode is json or exit")
        nodes[nid] = dict(numbers, id=nid, member=member, task=task, judge=n.get("judge"), access=access, judge_mode=mode,
                          needs=list(collections.OrderedDict.fromkeys(needs)))
        given_start[nid] = n.get("start")
        order.append(nid)
    for nid in order:
        for need in nodes[nid]["needs"]:
            if need == nid:
                problems.append(f"node {nid}: needs itself")
            elif need not in nodes:
                problems.append(f"node {nid}: needs {need}, which is not a node")
        given, needs = given_start[nid], nodes[nid]["needs"]
        if given is not None and given not in ["base", "merge"] + needs:
            problems.append(f"node {nid}: start is base, merge or one of its needs")
        if given == "merge" and len(needs) < 2:
            problems.append(f"node {nid}: start merge needs two or more needs")
        nodes[nid]["start"] = given or ("base" if len(needs) != 1 else needs[0])
    members = {}
    for nid in order:
        m = nodes[nid]["member"]
        if m and members.setdefault(m["name"], (nid, m))[1] != m:
            problems.append(f"node {nid}: member {m['name']} is not the same as in node {members[m['name']][0]}")
    repo = raw.get("repo")
    if repo is None:
        problems += [f"node {nid}: start only means something with a repo" for nid in order if given_start[nid] is not None]
    if repo is not None:
        repo = os.path.normpath(os.path.join(folder, repo)) if isinstance(repo, str) and repo.strip() else None
        if shutil.which("git") is None:
            problems.append("repo: git is not installed, and a plan with a repository needs it")
        elif not repo or subprocess.run(["git", "-C", repo, "rev-parse", "--git-dir"], stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL).returncode != 0:
            problems.append(f"repo: {raw.get('repo')!r} is not a git repository")
        for nid in order:
            if (nodes[nid]["member"] or {}).get("backend") == "opencode":
                problems.append(f"node {nid}: opencode members cannot work in a step's clone yet")
    base = raw.get("base", "HEAD")
    if not isinstance(base, str) or not base.strip():
        problems.append("base: a commit, branch or tag")
    if not problems:
        cycle = find_cycle(nodes, order)
        if cycle:
            problems.append("the needs make a cycle: " + " -> ".join(cycle))
    if problems:
        raise PlanError("; ".join(problems))
    for nid in order:
        nodes[nid]["judge_argv"] = resolve_args(nodes[nid]["judge"], folder)
        m = nodes[nid]["member"]
        if m["backend"] == "command":
            nodes[nid]["member"] = dict(m, command=" ".join(shlex.quote(a) for a in resolve_args(m["command"], folder)))
    name = raw["name"] if isinstance(raw.get("name"), str) and raw["name"].strip() else os.path.splitext(os.path.basename(path))[0]
    return {"name": name, "path": os.path.abspath(path), "folder": folder, "repo": repo, "base": base, "nodes": nodes,
            "order": order}


def judge_files(plan):
    """{absolute path: sha256} of every file a judge command names."""
    found = {}
    for nid in plan["order"]:
        for arg in plan["nodes"][nid]["judge_argv"]:
            if os.path.isabs(arg) and os.path.isfile(arg):
                found[arg] = sha256_file(arg)
    return found


def recorded_nodes(plan):
    """What the run records about each step, to tell later whether the plan changed."""
    return {n: {"member": dict(plan["nodes"][n]["member"]), "needs": plan["nodes"][n]["needs"],
                "task": sha256_file(plan["nodes"][n]["task"]), "judge": plan["nodes"][n]["judge"],
                "judge_argv": plan["nodes"][n]["judge_argv"], "judge_mode": plan["nodes"][n]["judge_mode"],
                "judge_timeout": plan["nodes"][n]["judge_timeout"], "access": plan["nodes"][n]["access"],
                "retries": plan["nodes"][n]["retries"], "timeout": plan["nodes"][n]["timeout"],
                "start": plan["nodes"][n]["start"]} for n in plan["order"]}


def make_workspace(repo, path, start, fetch=()):
    """A clone of repo at commit start with no remote left, and each (ref, folder, sha) of fetch brought in from that
    folder's output branch as refs/dag/<ref> (it must be the recorded commit)."""
    git("clone", "--quiet", "--shared", "--no-checkout", repo, path)
    git("-C", path, "remote", "remove", "origin")
    for ref, folder, sha in fetch:
        git("-C", path, "fetch", "--quiet", "--no-tags", folder, f"refs/heads/{OUT_BRANCH}:refs/dag/{ref}")
        got = git("-C", path, "rev-parse", f"refs/dag/{ref}")
        if got != sha:
            raise WorkspaceError(f"{ref}: its clone is at {got[:12]}, the run recorded {sha[:12]}")
    git("-C", path, "checkout", "--quiet", "-B", "dag-work", start)


def merge_needs(path, refs):
    """Merge refs/dag/<ref> for each ref into the clone's branch; a conflict is undone and raised as MergeConflict."""
    p = subprocess.run(["git", "-C", path, "-c", "commit.gpgsign=false", "merge", "--quiet", "--no-edit", "--no-verify",
                        "-m", "herdr-py dag: start from " + ", ".join(refs)] + [f"refs/dag/{r}" for r in refs],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, env=dict(os.environ, **GIT_ID))
    if p.returncode != 0:
        files = git("-C", path, "diff", "--name-only", "--diff-filter=U").split()
        subprocess.run(["git", "-C", path, "merge", "--abort"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        raise MergeConflict(f"the outputs of {', '.join(refs)} do not merge cleanly"
                            + (f" (conflicts in {', '.join(files)})" if files else f": {one_line(p.stdout, 200)}"))
    return git("-C", path, "rev-parse", "HEAD")


def commit_all(path, start, message):
    """Commit everything in the clone (whatever the member did to its branches) and point the output branch at it.
    Returns (sha, files changed since start)."""
    git("-C", path, "add", "-A")
    git("-C", path, "-c", "commit.gpgsign=false", "commit", "--quiet", "--allow-empty", "--no-verify", "-m", message,
        env=dict(os.environ, **GIT_ID))
    sha = git("-C", path, "rev-parse", "HEAD")
    git("-C", path, "branch", "-f", OUT_BRANCH, sha)
    changed = [line for line in git("-C", path, "diff", "--name-only", start, sha).splitlines() if line]
    return sha, changed


def judge_once(node, reply_path, cwd, env):
    """One verdict under coop.py's contract: (status, score, detail); a judge that breaks is infra_error."""
    check = (command_judge if node["judge_mode"] == "json" else exit_judge)(node["judge_argv"], timeout=node["judge_timeout"])
    try:
        status, score, detail = check(reply_path, cwd=cwd, env=env)
    except Exception as exc:  # the judge itself broke: not a fail of the step
        return "infra_error", None, str(exc)
    if status not in ("valid", "invalid"):
        return "infra_error", None, f"the judge said status {status!r}"
    return status, score, detail


def judge_env(plan_name, nid, attempt, base, sha, workspace, upstream):
    return dict(os.environ, HERDR_DAG_PLAN=plan_name, HERDR_DAG_NODE=nid, HERDR_DAG_ATTEMPT=str(attempt),
                HERDR_DAG_BASE=base or "", HERDR_DAG_COMMIT=sha or "", HERDR_DAG_WORKSPACE=workspace or "",
                HERDR_DAG_UPSTREAM=json.dumps(upstream, sort_keys=True))


class DagRun:
    """One run of one plan. members: an object with run_turn(name, prompt, timeout=..., workdir=..., access=...) ->
    (reply, state) and, optionally, tokens(name) (members.Members, or anything shaped like it)."""

    def __init__(self, plan, members, out, parallel=4, resume=False, keep_going=False, diff_bytes=12000,
                 reply_bytes=3000):
        if not (isinstance(parallel, int) and parallel >= 1):
            raise ValueError("parallel: 1 or more")
        out = os.path.abspath(out)  # members are given absolute folders
        self.plan, self.members, self.out, self.parallel = plan, members, out, parallel
        self.keep_going, self.diff_bytes, self.reply_bytes = keep_going, diff_bytes, reply_bytes
        self.events_path = os.path.join(out, "events.jsonl")
        if os.path.exists(self.events_path) and not resume:
            raise ValueError(f"{out} already holds a run: give a new folder, or --resume")
        if resume and not os.path.exists(self.events_path):
            raise ValueError(f"{out} holds no run to resume")
        for sub in ("replies", "workspaces"):
            os.makedirs(os.path.join(out, sub), exist_ok=True)
        self.lock = threading.Lock()
        self.member_locks = collections.defaultdict(threading.Lock)
        self.events, self.stopped, self.resumed_passed = [], None, set()
        self.state = {n: {"state": "waiting", "attempt": 0, "tries": 0, "sha": None, "score": None, "reply": None,
                          "workspace": None, "detail": None, "prev": None, "seconds": 0.0, "busy": 0.0, "tokens": None}
                      for n in plan["order"]}
        if resume:
            self.restore()
        self.log = open(self.events_path, "a", encoding="utf-8", buffering=1)
        if resume:
            self.emit("run.resume", passed=sorted(self.resumed_passed))
        else:
            self.start()

    # -- records -------------------------------------------------------------------------------------------------

    def emit(self, kind, **fields):
        with self.lock:
            event = dict(fields, t=round(time.time(), 3), seq=len(self.events) + 1, kind=kind)
            self.events.append(event)
            self.log.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            return event

    def start(self):
        plan = self.plan
        self.base = git("-C", plan["repo"], "rev-parse", "--verify", plan["base"] + "^{commit}") if plan["repo"] else None
        self.judges = judge_files(plan)
        nodes = recorded_nodes(plan)
        with open(os.path.join(self.out, "plan.json"), "w", encoding="utf-8") as handle:
            json.dump({"name": plan["name"], "repo": plan["repo"], "base": self.base, "order": plan["order"], "nodes": nodes},
                      handle, ensure_ascii=False, indent=1)
        self.first = self.emit("run.start", plan=plan["name"], repo=plan["repo"], base=self.base, nodes=nodes,
                               order=plan["order"], judge_files=self.judges, parallel=self.parallel)

    def restore(self):
        """Rebuild the state from events.jsonl; the plan, its tasks and its judges must be what the run started with."""
        with open(self.events_path, encoding="utf-8") as handle:
            self.events = [json.loads(line) for line in handle if line.strip()]
        first = next((e for e in self.events if e["kind"] == "run.start"), None)
        if first is None:
            raise ValueError(f"{self.events_path}: no run.start")
        if recorded_nodes(self.plan) != first["nodes"]:
            raise ValueError("the plan or a task file changed since the run started: start a new run")
        if judge_files(self.plan) != first["judge_files"]:
            raise ValueError("a judge file changed since the run started: start a new run")
        self.first, self.base, self.judges = first, first["base"], first["judge_files"]
        for e in self.events:
            st = self.state.get(e.get("node"))
            if st is None:
                continue
            kind = e["kind"]
            if kind == "node.dispatch":
                st.update(attempt=max(st["attempt"], e["attempt"]), state="waiting")
            elif kind == "node.verdict":
                st["tries"] += 1
            elif kind == "node.retry":
                st.update(prev=e.get("prev"), detail=e.get("detail"))
            elif kind == "node.pass":
                st.update(state="passed", sha=e.get("sha"), score=e.get("score"), reply=e.get("reply"),
                          workspace=e.get("workspace"))
            elif kind == "node.fail":
                # a step that failed because the setup broke runs again; a step the judge failed stays failed
                st.update(state="waiting" if e.get("infra") else "failed", detail=e.get("why"))
            elif kind == "node.return":
                st["seconds"] += e.get("seconds") or 0
        for n, st in self.state.items():
            if st["state"] == "passed":
                kept = (has_commit(os.path.join(self.out, st["workspace"]), st["sha"]) if st["workspace"]
                        else bool(st["reply"]) and os.path.isfile(os.path.join(self.out, "replies", st["reply"] + ".txt")))
                if kept:
                    self.resumed_passed.add(n)
                else:
                    st.update(state="waiting")  # its output is gone: it has to run again (summary: dispatched_after_pass)

    # -- one step ------------------------------------------------------------------------------------------------

    def store(self, text):
        digest = sha256_text(text)
        path = os.path.join(self.out, "replies", digest + ".txt")
        if not os.path.exists(path):
            with open(path + ".tmp", "w", encoding="utf-8") as handle:
                handle.write(text)
            os.replace(path + ".tmp", path)
        return digest

    def reply_text(self, digest):
        with open(os.path.join(self.out, "replies", digest + ".txt"), encoding="utf-8") as handle:
            return handle.read()

    def upstream_text(self, node, workspace):
        parts = []
        for uid in node["needs"]:
            up, st = self.plan["nodes"][uid], self.state[uid]
            score = f" with score {st['score']}" if st["score"] is not None else ""
            summary = self.reply_text(st["reply"]).strip() if st["reply"] else ""
            if len(summary) > self.reply_bytes:
                summary = summary[:self.reply_bytes] + " [...]"
            lines = [f"Step {uid} (done by {up['member']['name']}) passed the judge{score}. Its summary:", summary or "(none)"]
            if workspace and st["sha"]:
                stat = git("-C", workspace, "diff", "--stat", self.base, st["sha"])
                diff = git("-C", workspace, "diff", self.base, st["sha"])
                if len(diff) > self.diff_bytes:
                    diff = diff[:self.diff_bytes] + f"\n[... {len(diff) - self.diff_bytes} more bytes: git diff {self.base[:12]} refs/dag/{uid}]"
                lines += [f"Its result is commit {st['sha']}, in your clone as refs/dag/{uid}. What it changed from the base:",
                          stat or "(no changes)", "```diff", diff, "```"]
            parts.append("\n".join(lines))
        return ("\nWhat the steps before yours produced:\n\n" + "\n\n".join(parts) + "\n") if parts else ""

    def prompt(self, nid, start, workspace, origin=None):
        node, st = self.plan["nodes"][nid], self.state[nid]
        with open(node["task"], encoding="utf-8") as handle:
            task = handle.read().strip()
        position = f" It builds on {', '.join(node['needs'])}." if node["needs"] else ""
        retry = ""
        if st["prev"]:
            retry = (f"\nYour previous attempt did not pass the judge: {one_line(st['detail'] or 'no reason given', 1500)}\n"
                     + ("Your folder starts from that attempt's commit, so you can fix it.\n" if workspace else ""))
        if not workspace:
            place = PLACE_TEXT
        else:
            place = (PLACE_WRITE if node["access"] == "write" else PLACE_READ).format(start=start[:12], origin=origin)
        return PROMPT.format(member=node["member"]["name"], node=nid, plan=self.plan["name"], position=position, task=task,
                             upstream=self.upstream_text(node, workspace), retry=retry, place=place)

    def tokens(self, name):
        try:
            return self.members.tokens(name) if hasattr(self.members, "tokens") else None
        except Exception:  # counting tokens must never fail a step
            return None

    def judge(self, nid, attempt, reply_path, cwd, sha, workspace, upstream):
        for path, digest in self.judges.items():
            if not os.path.isfile(path) or sha256_file(path) != digest:
                return "infra_error", None, f"the judge file {path} changed during the run"
        env = judge_env(self.plan["name"], nid, attempt, self.base, sha, workspace, upstream)
        return judge_once(self.plan["nodes"][nid], reply_path, cwd, env)

    def fail(self, nid, attempt, why, dispatch=None, infra=False):
        self.state[nid].update(state="failed", detail=why)
        self.emit("node.fail", node=nid, attempt=attempt, dispatch=dispatch, why=why, infra=infra)
        if infra:
            self.halt(f"{nid}: {why}")

    def step(self, nid):
        """Run one step until it passes, fails or the run stops; its whole time (clones, member, commit, judge) is busy."""
        began = time.time()
        try:
            return self.attempts(nid)
        finally:
            self.state[nid]["busy"] += time.time() - began

    def attempts(self, nid):
        """The attempts of one step; every outcome is in events.jsonl."""
        node, st = self.plan["nodes"][nid], self.state[nid]
        name = node["member"]["name"]
        while True:
            st["attempt"] += 1
            attempt = st["attempt"]
            dispatch = sha256_text(f"{self.first['plan']}/{self.first['t']}/{nid}/{attempt}")[:16]
            rel = os.path.join("workspaces", f"{nid}-{attempt}")
            workspace = os.path.join(self.out, rel) if self.plan["repo"] else None
            upstream = {u: self.state[u]["sha"] or self.state[u]["reply"] for u in node["needs"]}
            start, origin = None, None
            try:
                if workspace:
                    if os.path.exists(workspace):  # left by an attempt that a crash cut short
                        shutil.rmtree(workspace)
                    if st["prev"]:
                        start, origin = st["prev"]["sha"], "your previous attempt"
                    elif node["start"] in ("base", "merge"):
                        start, origin = self.base, "the base commit"
                    else:
                        start, origin = self.state[node["start"]]["sha"], f"the output of step {node['start']}"
                    fetch = [(u, os.path.join(self.out, self.state[u]["workspace"]), self.state[u]["sha"]) for u in node["needs"]]
                    if st["prev"]:
                        fetch.append(("previous", os.path.join(self.out, st["prev"]["workspace"]), st["prev"]["sha"]))
                    make_workspace(self.plan["repo"], workspace, start, fetch)
                    if node["start"] == "merge" and not st["prev"]:
                        start, origin = merge_needs(workspace, node["needs"]), "the outputs of " + " and ".join(node["needs"]) + " merged"
                prompt = self.prompt(nid, start, workspace, origin)
            except MergeConflict as exc:
                return self.fail(nid, attempt, str(exc))
            except (WorkspaceError, OSError) as exc:
                return self.fail(nid, attempt, f"the step's clone could not be made: {exc}", infra=True)
            self.emit("node.dispatch", node=nid, attempt=attempt, dispatch=dispatch, member=name, start=start,
                      workspace=rel if workspace else None, upstream=upstream,
                      input=sha256_text(json.dumps([prompt, start, upstream], sort_keys=True)))
            st["state"] = "running"
            place = {"workdir": workspace, "access": node["access"]} if workspace else {}
            with self.member_locks[name]:
                before, began = self.tokens(name), time.time()
                try:
                    reply, state = self.members.run_turn(name, prompt, timeout=node["timeout"], **place)
                except Exception as exc:  # a member backend that breaks fails this attempt
                    reply, state = f"({type(exc).__name__}: {exc})", "error"
                seconds, after = round(time.time() - began, 2), self.tokens(name)
            used = after - before if isinstance(before, int) and isinstance(after, int) else None
            st["seconds"] += seconds
            if used is not None:
                st["tokens"] = (st["tokens"] or 0) + used
            digest = self.store(reply or "")
            self.emit("node.return", node=nid, attempt=attempt, dispatch=dispatch, state=state, seconds=seconds,
                      tokens=used, reply=digest)
            sha, broken = start, None
            if workspace:
                try:
                    if node["access"] == "write":
                        sha, changed = commit_all(workspace, start, f"herdr-py dag: {self.plan['name']} {nid} attempt {attempt}")
                    else:  # a read step keeps no changes: its output commit is where it started
                        git("-C", workspace, "branch", "-f", OUT_BRANCH, start)
                        changed = []
                    self.emit("node.commit", node=nid, attempt=attempt, dispatch=dispatch, sha=sha, changed=changed)
                except WorkspaceError as exc:
                    broken = f"the step's clone could not be committed: {exc}"
            if state in ("error", "aborted"):
                return self.fail(nid, attempt, f"the member's backend ended {state}: {one_line(reply, 300)}", dispatch, infra=True)
            if broken:
                status, score, detail = "invalid", None, broken
            elif state == "idle":
                st["state"] = "judging"
                status, score, detail = self.judge(nid, attempt, os.path.join(self.out, "replies", digest + ".txt"),
                                                   workspace or os.path.join(self.out, "replies"), sha, workspace, upstream)
            else:
                status, score, detail = "invalid", None, f"the member's turn ended {state} (limit {node['timeout']} s)"
            if status == "infra_error":
                return self.fail(nid, attempt, f"the judge failed: {one_line(detail, 300)}", dispatch, infra=True)
            st["tries"] += 1
            self.emit("node.verdict", node=nid, attempt=attempt, dispatch=dispatch, status=status, score=score,
                      detail=one_line(detail, 600))
            if status == "valid":
                st.update(state="passed", sha=sha, score=score, reply=digest, workspace=rel if workspace else None)
                self.emit("node.pass", node=nid, attempt=attempt, dispatch=dispatch, sha=sha, score=score, reply=digest,
                          workspace=rel if workspace else None)
                return None
            if st["tries"] <= node["retries"] and not self.stopped:
                st.update(prev={"sha": sha, "workspace": rel} if workspace else {"reply": digest}, detail=detail)
                self.emit("node.retry", node=nid, attempt=attempt, dispatch=dispatch, detail=one_line(detail, 600),
                          prev=st["prev"])
                continue
            return self.fail(nid, attempt, f"did not pass after {st['tries']} attempt(s): {one_line(detail, 300)}", dispatch)

    def halt(self, why):
        """The setup broke: no new step starts (unless keep_going); running steps finish."""
        if not self.keep_going and not self.stopped:
            self.stopped = why
            self.emit("run.stop", why=why)

    # -- the run -------------------------------------------------------------------------------------------------

    def ready(self):
        return [n for n in self.plan["order"] if self.state[n]["state"] == "waiting"
                and all(self.state[m]["state"] == "passed" for m in self.plan["nodes"][n]["needs"])]

    def block(self):
        """Mark waiting steps whose need failed or was blocked; repeat until nothing changes."""
        changed = True
        while changed:
            changed = False
            for n in self.plan["order"]:
                bad = [m for m in self.plan["nodes"][n]["needs"] if self.state[m]["state"] in ("failed", "blocked")]
                if self.state[n]["state"] == "waiting" and bad:
                    self.state[n].update(state="blocked", detail="needs " + ", ".join(bad))
                    self.emit("node.blocked", node=n, why="needs " + ", ".join(bad) + ", which did not pass")
                    changed = True

    def run(self):
        began = time.time()
        with concurrent.futures.ThreadPoolExecutor(self.parallel) as pool:
            running = {}
            while True:
                self.block()
                if not self.stopped:
                    for n in self.ready():
                        if len(running) >= self.parallel:
                            break
                        self.state[n]["state"] = "running"
                        running[pool.submit(self.step, n)] = n
                if not running:
                    break
                done, _ = concurrent.futures.wait(running, return_when=concurrent.futures.FIRST_COMPLETED)
                for future in done:
                    n = running.pop(future)
                    exc = future.exception()
                    if exc is not None:  # a bug in this runner, not in the member: say so and stop
                        self.fail(n, self.state[n]["attempt"], f"the runner failed: {type(exc).__name__}: {exc}", infra=True)
                self.view()
        summary = self.summary(time.time() - began)
        self.emit("run.end", **{k: summary[k] for k in ("passed", "failed", "blocked", "waiting", "stopped")})
        self.log.close()
        tmp = os.path.join(self.out, "summary.json.tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, ensure_ascii=False, indent=1)
        os.replace(tmp, os.path.join(self.out, "summary.json"))
        self.view()
        return summary

    def view(self):
        """Write view.html; a page that cannot be written is reported, it never ends the run."""
        try:
            dagview.save(self.out)
        except Exception as exc:  # noqa: BLE001 - the page is for people; events.jsonl is what counts
            print(f"herdr-py dag: warning: view.html not written: {type(exc).__name__}: {exc}", file=sys.stderr)

    def summary(self, seconds):
        counts = collections.Counter(st["state"] for st in self.state.values())
        busy = sum(st["busy"] for st in self.state.values())
        known = [st["tokens"] for st in self.state.values() if st["tokens"] is not None]
        summary = {"plan": self.plan["name"], "base": self.base, "passed": counts["passed"], "failed": counts["failed"],
                   "blocked": counts["blocked"], "waiting": counts["waiting"] + counts["running"] + counts["judging"],
                   "stopped": self.stopped, "seconds": round(seconds, 1), "busy_seconds": round(busy, 1),
                   "member_seconds": round(sum(st["seconds"] for st in self.state.values()), 1),
                   "parallelism": round(busy / seconds, 2) if seconds > 0 else None,
                   "tokens": sum(known) if known else None,
                   "nodes": {n: {"state": st["state"], "attempts": st["attempt"], "sha": st["sha"], "score": st["score"],
                                 "member": self.plan["nodes"][n]["member"]["name"], "seconds": round(st["seconds"], 1),
                                 "tokens": st["tokens"], "detail": st["detail"]} for n, st in self.state.items()},
                   "out_of_bounds": out_of_bounds(self.out, self.events)}
        summary.update(invariants(self.events, self.first["nodes"]))
        return summary


def invariants(events, nodes):
    """What the invariants count, from the events alone: dispatches made before every need had passed, and steps
    dispatched again after they had passed (both must be 0). nodes: {id: {"needs": [...]}}."""
    passed, early, again = set(), 0, 0
    for e in events:
        if e["kind"] == "node.pass":
            passed.add(e["node"])
        elif e["kind"] == "node.dispatch":
            if any(m not in passed for m in nodes[e["node"]]["needs"]):
                early += 1
            if e["node"] in passed:
                again += 1
    return {"released_early": early, "dispatched_after_pass": again}


def out_of_bounds(out, events):
    """Member tool events (codex and claude logs under members/) that name another step's workspace while a step ran:
    {"count": n, "examples": [...], "measured": [backends with logs]}; command members keep no such log."""
    homes = {}
    for e in events:
        if e["kind"] == "node.dispatch" and e.get("workspace"):
            path = os.path.join(out, e["workspace"])
            homes[(e["node"], e["attempt"])] = {path, os.path.realpath(path)}
    ends = {(e["node"], e["attempt"]): e["t"] for e in events if e["kind"] == "node.return"}
    windows = [(e["member"], e["t"], ends.get((e["node"], e["attempt"]), float("inf")), homes[(e["node"], e["attempt"])])
               for e in events if e["kind"] == "node.dispatch" and e.get("workspace")]
    count, examples, measured = 0, [], []
    for backend in ("codex", "claude"):
        path = os.path.join(out, "members", backend, "events.jsonl")
        if not os.path.isfile(path):
            continue
        measured.append(backend)
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict) or "event" not in row:
                    continue
                text = json.dumps(row["event"])
                for member, t0, t1, home in windows:
                    if row.get("agent") != member or not (t0 <= row.get("t", 0) <= t1 + 1):
                        continue
                    for key, others in homes.items():
                        if others is home:
                            continue
                        if any(other + os.sep in text or text.endswith(other) or (other + '"') in text for other in others):
                            count += 1
                            if len(examples) < 5:
                                examples.append(f"{member} touched {key[0]}-{key[1]}")
    return {"count": count, "examples": examples, "measured": measured}


def recheck(out):
    """Judge every passed step again in a fresh clone of its output commit, with the recorded judge.
    Returns [{"node", "recorded", "now", "score_recorded", "score_now", "detail"}], one per passed step."""
    out = os.path.abspath(out)
    with open(os.path.join(out, "events.jsonl"), encoding="utf-8") as handle:
        events = [json.loads(line) for line in handle if line.strip()]
    first = next(e for e in events if e["kind"] == "run.start")
    dispatched = {(e["node"], e["attempt"]): e for e in events if e["kind"] == "node.dispatch"}
    latest = {}
    for e in events:
        if e["kind"] == "node.pass":
            latest[e["node"]] = e
    changed = [p for p, digest in first["judge_files"].items() if not os.path.isfile(p) or sha256_file(p) != digest]
    rows = []
    for nid, e in latest.items():
        node = first["nodes"][nid]
        reply = os.path.join(out, "replies", e["reply"] + ".txt")
        if changed:
            rows.append({"node": nid, "recorded": "valid", "now": "infra_error", "score_recorded": e.get("score"),
                         "score_now": None, "detail": "judge files changed since the run: " + ", ".join(changed)})
            continue
        cwd, workspace = os.path.join(out, "replies"), None
        if e.get("workspace"):
            workspace = os.path.join(out, "recheck", f"{nid}-{e['attempt']}")
            if os.path.exists(workspace):
                shutil.rmtree(workspace)
            make_workspace(first["repo"], workspace, e["sha"], [("output", os.path.join(out, e["workspace"]), e["sha"])])
            cwd = workspace
        upstream = dispatched[(nid, e["attempt"])].get("upstream") or {}
        env = judge_env(first["plan"], nid, e["attempt"], first["base"], e.get("sha"), workspace, upstream)
        status, score, detail = judge_once(node, reply, cwd, env)
        rows.append({"node": nid, "recorded": "valid", "now": status, "score_recorded": e.get("score"), "score_now": score,
                     "detail": one_line(detail, 300)})
    return rows


def report(summary):
    s = summary
    lines = [f"plan {s['plan']}: {s['passed']} passed, {s['failed']} failed, {s['blocked']} blocked, {s['waiting']} not run"
             + (f"; STOPPED: {s['stopped']}" if s["stopped"] else ""),
             f"{s['seconds']} s wall, {s['busy_seconds']} s of step work, {s['member_seconds']} s of it in member turns "
             f"(parallelism {s['parallelism']}); "
             f"tokens {s['tokens'] if s['tokens'] is not None else 'not reported'}",
             f"invariants: started before their needs passed {s['released_early']}, passed steps dispatched again "
             f"{s['dispatched_after_pass']}, workspace crossings {s['out_of_bounds']['count']} "
             f"(measured for: {', '.join(s['out_of_bounds']['measured']) or 'no member logs'})"]
    for n, v in s["nodes"].items():
        lines.append(f"  {n:<12} {v['state']:<8} attempts {v['attempts']}  {v['member']}"
                     + (f"  commit {v['sha'][:12]}" if v["sha"] else "") + (f"  score {v['score']}" if v["score"] is not None else "")
                     + (f"  ({one_line(v['detail'], 120)})" if v["detail"] and v["state"] != "passed" else ""))
    return "\n".join(lines)


def print_check(plan):
    lv = levels(plan)
    print(f"plan {plan['name']}: {len(plan['order'])} steps"
          + (f", each in a clone of {plan['repo']} at {plan['base']}" if plan["repo"] else ", no repository"))
    for level in range(max(lv.values()) + 1):
        row = [n for n in plan["order"] if lv[n] == level]
        print(f"  level {level}: " + ", ".join(
            f"{n} ({plan['nodes'][n]['member']['name']}"
            + (f", after {', '.join(plan['nodes'][n]['needs'])}" if plan["nodes"][n]["needs"] else "") + ")" for n in row))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m herdr_py.dag", description=__doc__.split("\n\n")[0])
    ap.add_argument("plan", nargs="?", help="the plan (JSON)")
    ap.add_argument("--out", metavar="DIR", help="the run's folder (new, or the one to --resume)")
    ap.add_argument("--check", action="store_true", help="check the plan and print its steps; run nothing")
    ap.add_argument("--resume", action="store_true", help="go on with the run in --out")
    ap.add_argument("--parallel", type=int, default=4, metavar="N", help="steps at the same time (default 4)")
    ap.add_argument("--keep-going", action="store_true", help="a broken backend or judge fails only its step")
    ap.add_argument("--socket", help="the herdr-py daemon's socket (opencode members)")
    ap.add_argument("--recheck", metavar="DIR", help="judge every passed step of that run again in a fresh clone")
    a = ap.parse_args(argv)
    if a.recheck:
        try:
            rows = recheck(a.recheck)
        except (OSError, ValueError, KeyError, StopIteration, WorkspaceError) as exc:
            print(f"herdr-py dag: recheck: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        for r in rows:
            print(f"  {r['node']:<12} recorded {r['recorded']}, now {r['now']}"
                  + (f" (score {r['score_recorded']} -> {r['score_now']})" if r["score_recorded"] != r["score_now"] else "")
                  + (f": {r['detail']}" if r["now"] != "valid" else ""))
        wrong = [r for r in rows if r["now"] != "valid"]
        print(f"{len(rows)} passed steps judged again, {len(wrong)} did not hold")
        return 2 if any(r["now"] == "infra_error" for r in wrong) else (1 if wrong else 0)
    if not a.plan:
        ap.error("give the plan")
    try:
        plan = load_plan(a.plan)
    except PlanError as exc:
        print(f"herdr-py dag: {exc}", file=sys.stderr)
        return 2
    if a.check:
        print_check(plan)
        return 0
    if not a.out:
        ap.error("--out: the run's folder")
    specs = list(collections.OrderedDict((plan["nodes"][n]["member"]["name"], plan["nodes"][n]["member"])
                                         for n in plan["order"]).values())
    try:
        members = Members(specs, os.path.join(os.path.abspath(a.out), "members"), socket=a.socket, sessions="fresh",
                          cwd=plan["folder"])
    except MemberError as exc:
        print(f"herdr-py dag: {exc}", file=sys.stderr)
        return 2
    try:
        run = DagRun(plan, members, a.out, parallel=a.parallel, resume=a.resume, keep_going=a.keep_going)
    except (ValueError, WorkspaceError) as exc:
        members.close()
        print(f"herdr-py dag: {exc}", file=sys.stderr)
        return 2
    try:
        summary = run.run()
    finally:
        members.close()
    print(report(summary))
    print(f"every step on one page: {os.path.join(os.path.abspath(a.out), 'view.html')}")
    if summary["stopped"]:
        return 3
    return 0 if summary["passed"] == len(plan["order"]) else 1


if __name__ == "__main__":
    sys.exit(main())
