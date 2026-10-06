#!/usr/bin/env python3
"""Run the P23 plan (see the pre-registration in agent-cluster docs/experiments/p23_supervisor/).

For each task (sorted) the conditions run in the registered order (stage 1: S N T, stage 2: T N S). Every run gets a fresh
RHEL 8 container (OpenCode serve + the herdr-py daemon + `herdr-py team`) and a fresh copy of the task folder. Afterwards
the hidden grader runs in another container with no network and the work folder mounted read-only. Each run appends one
line to results.jsonl. An infrastructure failure (no summary written) is retried once and recorded either way.

usage: run.py --stage 1|2 [--tasks count,median] [--conditions S,N,T] [--out DIR] [--wall 720] [--dry-run]
"""
import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
IMAGE = "herdr-py/p23:rhel8"
MODEL = "qwen3-8b-32k:latest"
ORDER = {1: ["S", "N", "T"], 2: ["T", "N", "S"]}
OPENCODE_CONFIG = {
    "autoupdate": False, "share": "disabled", "model": "ollama/" + MODEL,
    "provider": {"ollama": {"npm": "@ai-sdk/openai-compatible", "name": "Ollama (host)",
                            "options": {"baseURL": "http://host.lima.internal:11434/v1"},
                            "models": {MODEL: {"name": "qwen3-8b-32k", "limit": {"context": 32768, "output": 8192}}}}},
    # every edit and shell command goes to herdr-py's policy (per role); web fetches are off for everyone
    "permission": {"edit": "ask", "bash": "ask", "webfetch": "deny", "external_directory": "ask", "doom_loop": "ask"},
}


def git_commit():
    p = subprocess.run(["git", "-C", REPO, "rev-parse", "--short", "HEAD"], stdout=subprocess.PIPE, universal_newlines=True)
    dirty = subprocess.run(["git", "-C", REPO, "status", "--porcelain"], stdout=subprocess.PIPE, universal_newlines=True).stdout.strip()
    return (p.stdout.strip() or "none") + ("+dirty" if dirty else "")


def docker(args, timeout):
    return subprocess.run(["docker"] + args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=timeout)


def one_run(out, stage, task, cond, wall, attempt):
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_id = "s%d-%s-%s-%s-a%d" % (stage, task, cond, stamp, attempt)
    base = os.path.join(out, "runs", run_id)
    work, state = os.path.join(base, "work"), os.path.join(base, "state")
    shutil.copytree(os.path.join(HERE, "tasks", task, "work"), work)
    os.makedirs(state)
    name = "p23-" + run_id.lower()
    started = time.time()
    try:
        p = docker(["run", "--rm", "--name", name, "--user", "%d:%d" % (os.getuid(), os.getgid()), "--cap-drop", "ALL",
                    "--security-opt", "no-new-privileges", "--read-only", "--tmpfs", "/tmp:rw,exec,size=1g",
                    "--memory", "4g", "--memory-swap", "4g", "--cpus", "2", "--pids-limit", "512", "--network", "bridge",
                    "-v", work + ":/work", "-v", state + ":/state", "-v", HERE + ":/bench:ro", "-v", REPO + ":/opt/herdr-py:ro",
                    "-w", "/work", "-e", "OPENCODE_CONFIG_CONTENT=" + json.dumps(OPENCODE_CONFIG), "-e", "WALL=%d" % wall,
                    IMAGE, "/bench/entry.sh", "/bench/tasks/" + task, cond], timeout=wall + 300)
        team_out = p.stdout[-2000:]
    except subprocess.TimeoutExpired:
        docker(["kill", name], 60)
        team_out = "(killed: container did not finish within the wall limit + 300 s)"
    seconds = round(time.time() - started, 1)
    summary_path = os.path.join(state, "summary.json")
    summary = json.load(open(summary_path)) if os.path.exists(summary_path) else None
    g = docker(["run", "--rm", "--network", "none", "--read-only", "--tmpfs", "/tmp:rw,exec,size=256m",
                "--user", "%d:%d" % (os.getuid(), os.getgid()), "-v", work + ":/work:ro", "-v", HERE + ":/bench:ro",
                IMAGE, "python3", "/bench/tasks/%s/grade.py" % task, "/work"], 300)
    row = {"run_id": run_id, "stage": stage, "task": task, "condition": cond, "attempt": attempt, "herdr_py": git_commit(),
           "infra": summary is None, "passed": g.returncode == 0 and summary is not None,
           "grade": (g.stdout.strip().splitlines() or [""])[-1][:300], "container_seconds": seconds,
           "summary": summary, "team_output": team_out if summary is None else team_out[-400:]}
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", type=int, choices=[1, 2], required=True)
    ap.add_argument("--tasks", default=",".join(sorted(os.listdir(os.path.join(HERE, "tasks")))))
    ap.add_argument("--conditions", default="S,N,T")
    ap.add_argument("--out", default=os.path.join(os.path.expanduser("~"), ".cache", "herdr-p23"))
    ap.add_argument("--wall", type=int, default=720)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    wanted = set(a.conditions.split(","))
    plan = [(t, c) for t in a.tasks.split(",") for c in ORDER[a.stage] if c in wanted]
    print("plan: " + ", ".join("%s/%s" % p for p in plan), flush=True)
    if a.dry_run:
        return 0
    os.makedirs(a.out, exist_ok=True)
    results = os.path.join(a.out, "results.jsonl")
    for task, cond in plan:
        for attempt in (1, 2):
            row = one_run(a.out, a.stage, task, cond, a.wall, attempt)
            with open(results, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            s = row["summary"] or {}
            print("%s %-9s %s  %-5s %6.0fs  %8s tok  %s | %s" % (datetime.datetime.now().strftime("%H:%M"), task, cond,
                  "INFRA" if row["infra"] else ("PASS" if row["passed"] else "fail"), row["container_seconds"],
                  "{:,}".format(s.get("tokens", 0)), s.get("outcome", "-"), row["grade"][:70]), flush=True)
            if not row["infra"]:
                break
    return 0


if __name__ == "__main__":
    sys.exit(main())
