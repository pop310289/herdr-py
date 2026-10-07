#!/usr/bin/env python3
"""One command for repeatable, comparable layout-team runs.

    teamrun.py SPEC.json                  one run into the spec's output folder; report.md next to chat.jsonl and summary.json
    teamrun.py SPEC.json --repeat N       N runs into OUTPUT/rep-1..N, then OUTPUT/aggregate.md (mean, min and max)
    teamrun.py --compare RUN_A RUN_B      two aggregates side by side

The spec is JSON; relative paths are relative to the spec file:
    {"title": "Claude drawers, Codex art director",                                             (optional)
     "task": {"original": "original.png", "canvas": [1206, 1441], "checklist": "my_checklist.md"},  (checklist optional)
     "members": [{"name": "drawA", "role": "drawer", "backend": "claude", "model": "haiku", "sessions": "fresh"},
                 {"name": "drawB", "role": "drawer", "backend": "opencode", "model": "ollama/qwen3-8b-32k:latest"},
                 {"name": "art", "role": "art", "backend": "codex", "sessions": "keep"}],
     "rounds": {"revisions": 2, "fixes": 2, "score": "strict", "notes": "match"},                  (optional; score, notes: strict|match)
     "timeouts": {"turn": 600, "art": 300},                                                        (optional, seconds)
     "opencode": {"url": "http://127.0.0.1:4096", "password_file": "~/.config/opencode-password"},  (when a member uses it)
     "chrome": "/path/to/chrome", "open_slide": "/path/to/open-slide-py",                          (optional)
     "output": "runs/claude-codex"}
backend: opencode (through a herdr-py daemon this program starts for each run against the given server; it never
starts OpenCode or a container), codex (Codex CLI), claude (Claude Code CLI) or fake (scripted members, for dry runs).
sessions: fresh (the default: every turn in a new conversation, the prompt carries everything) or keep.
The numbers in report.md and aggregate.md come from the run's files (runreport.py), never from this program's memory.
"""
import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, REPO)
import imgcmp  # noqa: E402
import layout_team as L  # noqa: E402
import runreport  # noqa: E402
import slide_team  # noqa: E402
from claude_agents import ClaudeAgents  # noqa: E402
from codex_agents import CodexAgents  # noqa: E402
from fake_agents import FakeAgents, Script  # noqa: E402
from herdr_py.client import Client, ClientError  # noqa: E402

BACKENDS = ("opencode", "codex", "claude", "fake")
NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}$")
RESERVED = {"supervisor", "manager", "check", "lessons", "picture", "render", "team", "build", "lint", "content", "operator"}
KEYS = {"title", "task", "members", "rounds", "timeouts", "opencode", "chrome", "open_slide", "output"}


class SpecError(ValueError):
    pass


def section(raw, key, allowed, problems, required=False):
    value = raw.get(key)
    if value is None:
        if required:
            problems.append(f"{key}: missing")
        return {}
    if not isinstance(value, dict):
        problems.append(f"{key}: must be an object")
        return {}
    unknown = sorted(set(value) - allowed)
    if unknown:
        problems.append(f"{key}: unknown keys {', '.join(unknown)} (allowed: {', '.join(sorted(allowed))})")
    return value


def whole(value, least):
    return isinstance(value, int) and not isinstance(value, bool) and value >= least


def load_spec(path):
    """The spec with defaults filled in and paths made absolute. SpecError lists every problem at once, before any
    model is called (a typo must not cost a run)."""
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError) as exc:
        raise SpecError(f"{path}: {exc}")
    if not isinstance(raw, dict):
        raise SpecError(f"{path}: the spec must be a JSON object")
    base = os.path.dirname(os.path.abspath(path))

    def where(p):
        return os.path.normpath(os.path.join(base, os.path.expanduser(p)))

    problems = []
    unknown = sorted(set(raw) - KEYS)
    if unknown:
        problems.append(f"unknown keys {', '.join(unknown)} (allowed: {', '.join(sorted(KEYS))})")
    defaults = L.arguments().parse_args(["--workdir", ".", "--reference", ".", "--open-slide", ""])
    spec = {"source": os.path.abspath(path), "title": raw.get("title") or ""}
    if not isinstance(spec["title"], str):
        problems.append("title: must be text")

    task = section(raw, "task", {"original", "canvas", "checklist"}, problems, required=True)
    original, size = task.get("original"), None
    if not isinstance(original, str):
        problems.append("task.original: give the path of the original picture (PNG)")
    else:
        original = where(original)
        try:
            size = imgcmp.read_png(original)[:2]
        except (OSError, ValueError) as exc:
            problems.append(f"task.original: {exc}")
    canvas = task.get("canvas")
    if not (isinstance(canvas, list) and len(canvas) == 2 and all(whole(v, 1) for v in canvas)):
        problems.append("task.canvas: give [width, height] in pixels")
    elif tuple(canvas) != L.SIZE:
        problems.append(f"task.canvas: the layout team knows one canvas, {L.SIZE[0]} x {L.SIZE[1]} (its rows, zones and labels "
                        f"belong to that picture), not {canvas[0]} x {canvas[1]}")
    elif size and tuple(size) != tuple(canvas):
        problems.append(f"task.original: the picture is {size[0]} x {size[1]} pixels, the canvas {canvas[0]} x {canvas[1]}")
    checklist = task.get("checklist")
    if checklist is not None:
        if not isinstance(checklist, str):
            problems.append("task.checklist: give a path")
        else:
            checklist = where(checklist)
            try:
                with open(checklist, encoding="utf-8") as handle:
                    text = handle.read()
                absent = [f'"## Row {n}"' for n in L.ROWS if f"## Row {n}" not in text]
                if absent:
                    problems.append(f"task.checklist: {checklist} has no {', '.join(absent)} section (one per row)")
            except OSError as exc:
                problems.append(f"task.checklist: {exc}")
    spec["task"] = {"original": original, "canvas": canvas, "checklist": checklist}

    members, seen = [], set()
    if not isinstance(raw.get("members"), list) or not raw["members"]:
        problems.append("members: give a list of members")
    for i, m in enumerate(raw.get("members") or []):
        label = f"members[{i}]"
        if not isinstance(m, dict):
            problems.append(f"{label}: must be an object")
            continue
        unknown = sorted(set(m) - {"name", "role", "backend", "model", "sessions"})
        if unknown:
            problems.append(f"{label}: unknown keys {', '.join(unknown)} (allowed: backend, model, name, role, sessions)")
        name = m.get("name")
        if not isinstance(name, str) or not NAME.match(name):
            problems.append(f"{label}.name: a letter, then letters, digits, - or _ (at most 32 characters)")
        elif name in RESERVED:
            problems.append(f"{label}.name: {name} is a role in the conversation log; choose another name")
        elif name in seen:
            problems.append(f"{label}.name: {name} is used twice")
        seen.add(name)
        if m.get("role") not in ("drawer", "art"):
            problems.append(f"{label}.role: drawer or art")
        if m.get("backend") not in BACKENDS:
            problems.append(f"{label}.backend: one of {', '.join(BACKENDS)}")
        if m.get("sessions", "fresh") not in ("fresh", "keep"):
            problems.append(f"{label}.sessions: fresh or keep")
        if m.get("model") is not None and not (isinstance(m["model"], str) and m["model"]):
            problems.append(f"{label}.model: give the model's name, or leave it out for the backend's default")
        elif m.get("backend") == "opencode" and m.get("model") and "/" not in m["model"]:
            problems.append(f"{label}.model: OpenCode names a model provider/model, e.g. ollama/qwen3-8b-32k:latest")
        members.append({"name": name, "role": m.get("role"), "backend": m.get("backend"), "model": m.get("model"),
                        "sessions": m.get("sessions", "fresh")})
    if members:
        arts = [m["name"] for m in members if m["role"] == "art"]
        if len(arts) != 1:
            problems.append(f"members: exactly one art director (role art), not {len(arts)}")
        if not any(m["role"] == "drawer" for m in members):
            problems.append("members: at least one drawer (role drawer)")
    spec["members"] = members

    rounds = section(raw, "rounds", {"revisions", "fixes", "score", "notes"}, problems)
    spec["rounds"] = {"revisions": rounds.get("revisions", defaults.revisions), "fixes": rounds.get("fixes", defaults.fixes)}
    for key, value in spec["rounds"].items():
        if not whole(value, 0):
            problems.append(f"rounds.{key}: a whole number, 0 or more")
    spec["rounds"]["score"] = rounds.get("score", defaults.score)  # which score keeps a revision (layout_team.py --score)
    if spec["rounds"]["score"] not in L.scoring.SCORE_MODES:
        problems.append(f"rounds.score: one of {', '.join(L.scoring.SCORE_MODES)}")
    spec["rounds"]["notes"] = rounds.get("notes", defaults.notes)  # which program notes revisers get (layout_team.py --notes)
    if spec["rounds"]["notes"] not in ("match", "strict"):
        problems.append("rounds.notes: one of match, strict")
    timeouts = section(raw, "timeouts", {"turn", "art"}, problems)
    spec["timeouts"] = {"turn": timeouts.get("turn", defaults.turn_timeout), "art": timeouts.get("art", defaults.art_timeout)}
    for key, value in spec["timeouts"].items():
        if not whole(value, 1):
            problems.append(f"timeouts.{key}: whole seconds, 1 or more")

    spec["opencode"] = None
    if any(m["backend"] == "opencode" for m in members):
        oc = section(raw, "opencode", {"url", "password_file", "username", "policy"}, problems, required=True)
        url = oc.get("url")
        if not (isinstance(url, str) and re.match(r"https?://", url)):
            problems.append("opencode.url: the OpenCode server, e.g. http://127.0.0.1:4096")
        spec["opencode"] = {"url": url, "username": oc.get("username"), "password_file": None,
                            "policy": os.path.join(HERE, "layout", "policy.json")}
        for key in ("password_file", "policy"):
            if oc.get(key) is not None:
                spec["opencode"][key] = where(oc[key]) if isinstance(oc[key], str) else None
                if not (spec["opencode"][key] and os.path.isfile(spec["opencode"][key])):
                    problems.append(f"opencode.{key}: {oc[key]} is not a file")
    for key in ("chrome", "open_slide"):
        value = raw.get(key)
        if value is not None and not isinstance(value, str):
            problems.append(f"{key}: give a path")
            value = None
        spec[key] = where(value) if value else None
        if key == "open_slide" and spec[key] and not os.path.isdir(spec[key]):
            problems.append(f"open_slide: {spec[key]} is not a folder")
    spec["chrome"] = spec["chrome"] or slide_team.CHROME
    if not isinstance(raw.get("output"), str) or not raw["output"]:
        problems.append("output: the folder for the run (it must not hold files yet)")
    else:
        spec["output"] = where(raw["output"])
    if problems:
        raise SpecError(f"{path}:\n" + "\n".join("- " + p for p in problems))
    return spec


class OpenCodeMembers:
    """OpenCode members through the herdr-py daemon, with slide_team.Team.run_turn's turn: start or prompt, wait until
    the agent is idle, read its last reply."""

    def __init__(self, socket, owner):
        self.client = Client(socket, timeout=None)
        self.owner = owner  # its chat() hears about turns stopped at the time limit
        self.fresh = set()

    def chat(self, *args):
        self.owner.chat(*args)

    def forget(self, name):
        self.fresh.add(name)

    def run_turn(self, name, prompt, model=None, files=(), timeout=900):
        fresh = name in self.fresh
        self.fresh.discard(name)
        return slide_team.Team.run_turn(self, name, prompt, model, files=files, timeout=timeout, fresh=fresh)

    def tokens(self, name):
        try:
            return self.client.call("agent.get", name=name)["tokens"]
        except (ClientError, OSError, KeyError):
            return None

    def close(self):
        pass


class Members:
    """Sends each member's turn to its backend (members of one backend share an instance) and times every turn."""

    def __init__(self, members, work, seed=1, socket=None):
        self.spec = {m["name"]: m for m in members}
        kinds = {m["backend"] for m in members}
        self.backends = {}
        if "codex" in kinds:
            self.backends["codex"] = CodexAgents(os.path.join(work, "codex"))
        if "claude" in kinds:
            self.backends["claude"] = ClaudeAgents(os.path.join(work, "claude"))
        if "fake" in kinds:
            self.backends["fake"] = FakeAgents(os.path.join(work, "fake"), Script(seed))
        if "opencode" in kinds:
            self.backends["opencode"] = OpenCodeMembers(socket, self)
        self.stats = {name: {"turns": 0, "seconds": 0.0, "states": {}} for name in self.spec}
        self.chat = lambda *args: None  # the team's chat, once the team exists

    def run_turn(self, name, prompt, model=None, files=(), timeout=600):
        """The member's own model and session rule apply; the model the team passes is not used."""
        member = self.spec[name]
        backend = self.backends[member["backend"]]
        if member["sessions"] == "fresh":
            backend.forget(name)
        start, state = time.time(), "error"
        try:
            text, state = backend.run_turn(name, prompt, member["model"], files=files, timeout=timeout)
        finally:
            stats = self.stats[name]
            stats["turns"] += 1
            stats["seconds"] += time.time() - start
            stats["states"][state] = stats["states"].get(state, 0) + 1
        return text, state

    def tokens(self, name):
        backend = self.backends[self.spec[name]["backend"]]
        if isinstance(backend, OpenCodeMembers):
            return backend.tokens(name)
        return backend.agents.get(name, {}).get("tokens", 0)

    def summary(self):
        return [dict(self.spec[name], turns=s["turns"], seconds=round(s["seconds"], 2), states=s["states"], tokens=self.tokens(name))
                for name, s in self.stats.items()]

    def close(self):
        for backend in self.backends.values():
            backend.close()


def prompt_limit(spec):
    """Prompts one OpenCode member may need in a run (the daemon's --max-prompts counts across fresh sessions)."""
    drawers = sum(1 for m in spec["members"] if m["role"] == "drawer")
    rounds = spec["rounds"]
    turns = len(L.ROWS) * (1 + rounds["revisions"])
    return max(math.ceil(turns / drawers) * (1 + rounds["fixes"]), len(L.ROWS) * rounds["revisions"], 1)


def start_daemon(spec, state_dir, timeout=90):
    """A herdr-py daemon of this run's own (agent names, turn counts and sessions never carry into the next run),
    connected to the OpenCode server named in the spec. Returns (process, socket path)."""
    os.makedirs(state_dir, exist_ok=True)
    sock = os.path.join(tempfile.mkdtemp(prefix="teamrun-", dir="/tmp"), "d.sock")  # AF_UNIX paths are short (~104 bytes)
    oc = spec["opencode"]
    argv = [sys.executable, "-m", "herdr_py", "--socket", sock, "serve", "--opencode", oc["url"], "--state-dir", state_dir,
            "--policy", oc["policy"], "--questions", "reject", "--wait", "60",
            "--max-agents", str(sum(1 for m in spec["members"] if m["backend"] == "opencode")),
            "--max-prompts", str(prompt_limit(spec))]
    if oc["password_file"]:
        argv += ["--password-file", oc["password_file"]]
    if oc["username"]:
        argv += ["--username", oc["username"]]
    env = dict(os.environ, PYTHONPATH=REPO, PYTHONDONTWRITEBYTECODE="1")
    with open(os.path.join(state_dir, "daemon.out"), "w") as out:
        proc = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        try:
            Client(sock, timeout=2).call("ping")
            return proc, sock
        except (ClientError, OSError):
            time.sleep(0.2)
    stop_daemon(proc, sock)
    with open(os.path.join(state_dir, "daemon.out")) as handle:
        raise RuntimeError("the herdr-py daemon did not start: " + handle.read().strip()[-400:])


def stop_daemon(proc, sock):
    try:
        Client(sock, timeout=5).call("server.stop")
    except (ClientError, OSError):
        pass
    try:
        proc.wait(30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    shutil.rmtree(os.path.dirname(sock), ignore_errors=True)


def team_args(spec, folder):
    argv = ["--workdir", folder, "--reference", spec["task"]["original"], "--open-slide", spec["open_slide"] or "",
            "--revisions", str(spec["rounds"]["revisions"]), "--fixes", str(spec["rounds"]["fixes"]), "--score", spec["rounds"]["score"], "--notes", spec["rounds"]["notes"],
            "--turn-timeout", str(spec["timeouts"]["turn"]), "--art-timeout", str(spec["timeouts"]["art"]),
            "--chrome", spec["chrome"]]
    if spec["task"]["checklist"]:
        argv += ["--checklist", spec["task"]["checklist"]]
    return L.arguments().parse_args(argv)  # the models in it are not used: every member brings its own


def run_once(spec, folder, rep, renderer=None):
    """One run of the team into folder: the team's files, run.json (members' tokens and time, errors) and report.md."""
    os.makedirs(folder, exist_ok=True)
    record = {"title": spec["title"], "spec": spec["source"], "rep": rep, "renderer": type(renderer).__name__ if renderer
              else "ChromeRenderer", "started": round(time.time(), 2), "error": None, "members": []}
    daemon = sock = members = team = None
    try:
        if spec["opencode"]:
            daemon, sock = start_daemon(spec, os.path.join(folder, "daemon"))
        members = Members(spec["members"], folder, seed=rep, socket=sock)
        team = L.LayoutTeam(team_args(spec, folder), members=members, renderer=renderer,
                            drawers=[m["name"] for m in spec["members"] if m["role"] == "drawer"],
                            art=next(m["name"] for m in spec["members"] if m["role"] == "art"))
        members.chat = team.chat
        team.run()
    except KeyboardInterrupt:
        record["error"] = "interrupted"
        raise
    except (Exception, SystemExit) as exc:  # the run failed: say why in run.json and report.md, then go on with the next run
        record["error"] = f"{type(exc).__name__}: {exc}"
        record["trace"] = traceback.format_exc()[-3000:]
    finally:
        if members:
            record["members"] = members.summary()  # before the daemon stops: OpenCode members' tokens come from it
            members.close()
        if team:
            team.chat_file.close()
            team.manifest.close()
        if daemon:
            stop_daemon(daemon, sock)
            record["daemon"] = {"socket": sock, "exit": daemon.poll(), "log": os.path.join(folder, "daemon", "daemon.out")}
        record["finished"] = round(time.time(), 2)
        record["seconds"] = round(record["finished"] - record["started"], 2)
        with open(os.path.join(folder, "run.json"), "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=1)
        runreport.write_report(folder)
    return record["error"] is None


def main(argv=None, renderer=None):
    """renderer: tests pass a stand-in for headless Chrome."""
    ap = argparse.ArgumentParser(description="Repeatable, comparable layout-team runs from a spec file.")
    ap.add_argument("spec", nargs="?", help="the run's spec (JSON)")
    ap.add_argument("--repeat", type=int, help="run the spec N times into OUTPUT/rep-1..N and write OUTPUT/aggregate.md")
    ap.add_argument("--compare", nargs=2, metavar=("RUN_A", "RUN_B"), help="print two aggregates side by side")
    a = ap.parse_args(argv)
    if a.compare:
        missing = [folder for folder in a.compare if not os.path.isdir(folder)]
        if missing:
            print(f"teamrun: no run folder at {', '.join(missing)}", file=sys.stderr)
            return 2
        print(runreport.compare_text(*a.compare))
        return 0
    if not a.spec:
        ap.error("give a spec file, or --compare RUN_A RUN_B")
    if a.repeat is not None and a.repeat < 1:
        ap.error("--repeat needs 1 or more")
    try:
        spec = load_spec(a.spec)
    except SpecError as exc:
        print(f"teamrun: {exc}", file=sys.stderr)
        return 2
    if renderer is None and not os.path.isfile(spec["chrome"]):
        print(f"teamrun: Chrome is not at {spec['chrome']}; give its path as \"chrome\" in the spec", file=sys.stderr)
        return 2
    out = spec["output"]
    if os.path.isdir(out) and os.listdir(out):
        print(f"teamrun: {out} already holds files; give the spec another output folder (nothing was overwritten)",
              file=sys.stderr)
        return 2
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "spec.json"), "w", encoding="utf-8") as handle:
        json.dump(spec, handle, indent=1)  # what was run: defaults filled in, paths resolved
    if a.repeat is None:
        try:
            ok = run_once(spec, out, 1, renderer)
        except KeyboardInterrupt:
            print(f"teamrun: interrupted: {os.path.join(out, 'report.md')}", file=sys.stderr)
            return 130
        print(f"teamrun: {'finished' if ok else 'FAILED'}: {os.path.join(out, 'report.md')}", flush=True)
        return 0 if ok else 1
    failed = []
    try:
        for k in range(1, a.repeat + 1):
            folder = os.path.join(out, f"rep-{k}")
            ok = run_once(spec, folder, k, renderer)
            failed += [] if ok else [k]
            print(f"teamrun: run {k}/{a.repeat} {'finished' if ok else 'FAILED'}: {os.path.join(folder, 'report.md')}", flush=True)
    except KeyboardInterrupt:  # stopped by hand: the runs that finished still get their aggregate
        print(f"teamrun: interrupted in run {k}: {runreport.write_aggregate(out)}", file=sys.stderr)
        return 130
    path = runreport.write_aggregate(out)
    print(f"teamrun: {a.repeat - len(failed)} of {a.repeat} runs finished: {path}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
