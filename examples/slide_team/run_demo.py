#!/usr/bin/env python3
"""Run the slide-team demo end to end and record it.

1. OpenCode server in a RHEL 8 container (image herdr-py/slides:rhel8: UBI 8 + Python 3.11 + OpenCode), with
   open-slide-py mounted read-only; models: a text model for the drawers and a vision model for the art director.
2. herdr-py daemon on the host, connected to that server, with examples/slide_team/policy.json.
3. view.py recorded through a pseudo-terminal (tools/rec.py), slide_team.py as the supervisor.
4. tools/cast2mp4.py makes the video: the conversation on top, the latest render and the original below.

usage: run_demo.py --reference ref.png --open-slide /path/to/open-slide-py [--rounds 4] [--out DIR] [--check-vision]
"""
import argparse
import datetime
import json
import os
import secrets
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
PY = sys.executable
TEXT_MODEL, VISION_MODEL = "qwen3-8b-32k:latest", "qwen3-vl-32k:latest"


def opencode_config():
    models = {TEXT_MODEL: {"name": "qwen3-8b-32k", "limit": {"context": 32768, "output": 8192}},
              VISION_MODEL: {"name": "qwen3-vl-32k", "limit": {"context": 32768, "output": 4096}, "attachment": True,
                             "modalities": {"input": ["text", "image"], "output": ["text"]}}}
    return {"autoupdate": False, "share": "disabled", "model": "ollama/" + TEXT_MODEL,
            "provider": {"ollama": {"npm": "@ai-sdk/openai-compatible", "name": "Ollama (host)",
                                    "options": {"baseURL": "http://host.lima.internal:11434/v1"}, "models": models}},
            "permission": {"edit": "ask", "bash": "ask", "webfetch": "deny", "external_directory": "ask", "doom_loop": "ask"}}


def wait_for(cond, timeout, what):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return
        time.sleep(0.5)
    raise SystemExit(f"timed out waiting for {what}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reference", required=True)
    ap.add_argument("--open-slide", required=True)
    ap.add_argument("--rounds", type=int, default=4)
    ap.add_argument("--target", type=float, default=8)
    ap.add_argument("--out", default=os.path.join(os.path.expanduser("~"), ".cache", "herdr-slides"))
    ap.add_argument("--port", type=int, default=4540)
    ap.add_argument("--check-vision", action="store_true", help="only check that the vision model sees attached images")
    a = ap.parse_args()
    run = os.path.join(a.out, datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    work, state = os.path.join(run, "work"), os.path.join(run, "state")
    os.makedirs(work)
    os.makedirs(state)
    password = secrets.token_hex(16)
    pw_file = os.path.join(state, "opencode-password")
    with open(pw_file, "w") as handle:
        handle.write(password)
    os.chmod(pw_file, 0o600)
    container = "herdr-slides-" + os.path.basename(run)
    subprocess.run(["docker", "run", "-d", "--rm", "--name", container, "--user", "%d:%d" % (os.getuid(), os.getgid()),
                    "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only", "--tmpfs", "/tmp:rw,exec,size=1g",
                    "--memory", "4g", "--memory-swap", "4g", "--cpus", "2", "--pids-limit", "512", "--network", "bridge",
                    "-p", "127.0.0.1:%d:4096" % a.port, "-v", work + ":/work", "-v", os.path.abspath(a.open_slide) + ":/opt/open-slide-py:ro",
                    "-w", "/work", "-e", "HOME=/tmp/home", "-e", "PYTHONPATH=/opt/open-slide-py", "-e", "PYTHONDONTWRITEBYTECODE=1",
                    "-e", "OPENCODE_SERVER_PASSWORD=" + password, "-e", "OPENCODE_DISABLE_AUTOUPDATE=1",
                    "-e", "OPENCODE_CONFIG_CONTENT=" + json.dumps(opencode_config()),
                    "herdr-py/slides:rhel8", "opencode", "serve", "--hostname", "0.0.0.0", "--port", "4096"],
                   check=True, stdout=subprocess.DEVNULL)
    sock = "/tmp/hps-%s.sock" % os.path.basename(run)[-6:]
    env = dict(os.environ, PYTHONPATH=REPO, PYTHONDONTWRITEBYTECODE="1")
    daemon = subprocess.Popen([PY, "-m", "herdr_py", "--socket", sock, "serve", "--opencode", "http://127.0.0.1:%d" % a.port,
                               "--password-file", pw_file, "--policy", os.path.join(HERE, "policy.json"), "--state-dir", state,
                               "--questions", "reject", "--max-agents", "6", "--max-prompts", "12", "--wait", "90"],
                              env=env, stdout=open(os.path.join(state, "daemon.out"), "w"), stderr=subprocess.STDOUT)
    try:
        wait_for(lambda: os.path.exists(sock), 120, "the herdr-py daemon")
        if a.check_vision:
            sys.path.insert(0, REPO)
            from herdr_py.client import Client
            small = os.path.join(state, "ref.png")
            subprocess.run(["sips", "-Z", "800", a.reference, "--out", small], stdout=subprocess.DEVNULL, check=True)
            c = Client(sock, timeout=None)
            c.call("agent.start", name="art", model="ollama/" + VISION_MODEL, files=[small],
                   prompt="Describe this picture in three short lines: its title, how many rows it has, and two labels you can read.")
            wait_for(lambda: c.call("agent.get", name="art")["state"] in ("idle", "aborted", "error"), 600, "the vision model")
            texts = [m["text"] for m in c.call("agent.read", name="art")["messages"] if m["kind"] == "text"]
            print("vision check reply:\n" + (texts[-1] if texts else "(none)"))
            return 0
        chat = os.path.join(work, "chat.jsonl")
        cast = os.path.join(run, "view.cast")
        viewer = subprocess.Popen([PY, os.path.join(REPO, "tools", "rec.py"), "--cols", "66", "--rows", "34", "--out", cast,
                                   "--title", "herdr-py slide team", "--", PY, os.path.join(HERE, "view.py"), "--socket", sock,
                                   "--chat", chat], env=env)
        code = subprocess.call([PY, os.path.join(HERE, "slide_team.py"), "--socket", sock, "--workdir", work, "--reference", a.reference,
                                "--open-slide", a.open_slide, "--rounds", str(a.rounds), "--target", str(a.target), "--chat", chat], env=env)
        viewer.wait(timeout=120)
        print(json.dumps({"run": run, "team_exit": code, "cast": cast}))
        return code
    finally:
        subprocess.run([PY, "-m", "herdr_py", "--socket", sock, "stop"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        daemon.wait(timeout=30)
        subprocess.run(["docker", "stop", "-t", "5", container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    sys.exit(main())
