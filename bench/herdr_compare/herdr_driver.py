#!/usr/bin/env python3
"""Drive the 4-agent scenario through herdr (inside the herdr bench container) and record what herdr believed.

The same four OpenCode agents as herdr-py's scenario, each an interactive `opencode` TUI in its own herdr workspace.
The controller only uses herdr's own interfaces (socket API): it reads the pane to find the permission dialog, decides
with the same allowlist, and answers with keys (Enter = allow once; Right Right Enter = reject); it sends the follow-up
with agent.prompt and aborts with Esc Esc. Ground truth: every TUI's internal OpenCode server (--port), via GET /event.

usage: herdr_driver.py OUT_DIR [--integration] [--deadline 600]
writes OUT_DIR/{herdr_states.jsonl, ground_truth.jsonl, decisions.jsonl, resources.jsonl, summary.json}
"""
import argparse
import itertools
import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.request

SOCK = os.path.join(os.environ.get("HOME", "/tmp/hh"), ".config", "herdr", "herdr.sock")
MODEL = "ollama/qwen3-8b-32k:latest"
SAFE = re.compile(r"(python3 [\w./-]+\.py( [\w./-]+)*|python3 --version|ls( -\w+)*( [\w./-]+)*|cat [\w./-]+|wc( -\w+)* [\w./-]+)")
AGENTS = [
    ("fizz", 4601, "Create the file fizz/fizzbuzz.py that prints FizzBuzz for the numbers 1 to 15, one per line. "
                   "Then run it with the shell command `python3 fizz/fizzbuzz.py` to check the output.",
     "Now change fizz/fizzbuzz.py so that it also prints the sum of the numbers 1 to 15 on the last line, "
     "then run it again with `python3 fizz/fizzbuzz.py`.", None),
    ("net", 4602, "Run the shell command `curl -sI https://example.com` and tell me the HTTP status code. "
                  "If the command is not allowed, just say so and stop.", None, None),
    ("deleg", 4603, "Use the task tool to start a subagent (subagent_type general). Ask the subagent to run the shell command `ls` "
                    "and report the file and folder names it sees. Then tell me what the subagent found.", None, None),
    ("long", 4604, "Write the English words for every number from 1 to 300, one per line, directly in your reply. Do not use any tools.",
     None, 40),
]
T0 = time.time()
ids = itertools.count(1)


def now():
    return round(time.time() - T0, 3)


def api(method, **params):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(30)
    s.connect(SOCK)
    try:
        s.sendall((json.dumps({"id": "d%d" % next(ids), "method": method, "params": params}) + "\n").encode())
        line = s.makefile("rb").readline()
    finally:
        s.close()
    reply = json.loads(line)
    if "error" in reply:
        raise RuntimeError("%s: %s" % (method, reply["error"]))
    return reply["result"]


def herdr(*args):
    p = subprocess.run(["herdr"] + list(args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=120)
    return p.stdout


class Log:
    def __init__(self, path):
        self.handle = open(path, "a", encoding="utf-8", buffering=1)
        self.lock = threading.Lock()

    def write(self, **entry):
        with self.lock:
            self.handle.write(json.dumps({"t": now(), **entry}, ensure_ascii=False) + "\n")


def ground_truth(name, port, log, stop):
    """Subscribe to the TUI's own OpenCode server; record the state-relevant events."""
    while not stop.is_set():
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:%d/event" % port, timeout=30)
            data = []
            while not stop.is_set():
                line = resp.readline()
                if not line:
                    break
                line = line.decode("utf-8", "replace").rstrip("\n")
                if line.startswith("data:"):
                    data.append(line[5:])
                elif not line and data:
                    e = json.loads("\n".join(data))
                    data = []
                    p = e.get("properties") or {}
                    if e.get("type") in ("session.status", "session.idle", "permission.asked", "permission.replied",
                                         "session.error", "question.asked"):
                        log.write(agent=name, type=e["type"], status=(p.get("status") or {}).get("type"),
                                  reply=p.get("reply"), id=p.get("id") or p.get("requestID"), session=p.get("sessionID"),
                                  permission=p.get("permission"), command=(p.get("metadata") or {}).get("command"))
        except Exception:
            time.sleep(0.5)


def find_text(obj):
    """The `text` field of a read result, wherever herdr nests it."""
    if isinstance(obj, dict):
        if isinstance(obj.get("text"), str):
            return obj["text"]
        for value in obj.values():
            found = find_text(value)
            if found is not None:
                return found
    return None


def parse_dialog(screen):
    """(permission type, target) from OpenCode's permission dialog on the screen, or None."""
    lines = [l.strip(" ┃│") .strip() for l in screen.splitlines()]
    for i, line in enumerate(lines):
        if "Permission required" in line:
            kind, target = "", ""
            for nxt in lines[i + 1:i + 8]:
                if nxt.startswith("#") and not kind:
                    kind = nxt.lstrip("# ").strip()
                elif nxt.startswith("$ ") and not target:
                    target = nxt[2:].strip()
                elif nxt and not target and kind and not nxt.startswith(("Allow", "#")):
                    target = nxt
            return kind, target
    return None


def resources(log, stop):
    while not stop.is_set():
        out = subprocess.run(["ps", "-eo", "rss=,pcpu=,comm="], stdout=subprocess.PIPE, universal_newlines=True).stdout
        rows = [l.split(None, 2) for l in out.strip().splitlines()]
        log.write(processes=[{"rss_kb": int(r[0]), "cpu": float(r[1]), "comm": r[2]} for r in rows if len(r) == 3])
        stop.wait(2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--integration", action="store_true")
    ap.add_argument("--deadline", type=float, default=600)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    states, gt, decisions, res = (Log(os.path.join(a.out, n)) for n in
                                  ("herdr_states.jsonl", "ground_truth.jsonl", "decisions.jsonl", "resources.jsonl"))
    stop = threading.Event()
    threading.Thread(target=resources, args=(res, stop), daemon=True).start()
    if a.integration:
        decisions.write(action="integration", output=herdr("integration", "install", "opencode")[-300:])
    panes = {}
    for i, (name, port, _, _, _) in enumerate(AGENTS, start=1):
        ws = api("workspace.create", cwd="/work", label=name)
        panes[name] = ws["root_pane"]["pane_id"]
        threading.Thread(target=ground_truth, args=(name, port, gt, stop), daemon=True).start()
        herdr("agent", "start", name, "--kind", "opencode", "--pane", panes[name], "--", "--port", str(port), "--model", MODEL)
    time.sleep(3)
    for name, _, prompt, _, _ in AGENTS:
        api("agent.prompt", target=name, text=prompt)
        decisions.write(action="prompt", agent=name)
    seen_state, handled, followed, aborted, working_since, blocked_since = {}, set(), set(), set(), {}, {}
    while time.time() - T0 < a.deadline:
        agents = {x["name"]: x for x in api("agent.list")["agents"]}
        for name, info in agents.items():
            st = info.get("agent_status")
            if seen_state.get(name) != st:
                seen_state[name] = st
                states.write(agent=name, state=st, seq=info.get("state_change_seq"))
            if st == "working":
                working_since.setdefault(name, time.time())
            if st == "blocked" and (name, info.get("state_change_seq")) not in handled:
                screen = find_text(api("agent.read", target=name, source="visible", lines=40)) or ""
                dialog = parse_dialog(screen)
                if dialog is None:
                    first = blocked_since.setdefault((name, info.get("state_change_seq")), time.time())
                    if time.time() - first < 10:
                        continue  # the dialog may not be drawn yet; look again next round
                    dialog = ("(unparsed)", screen[-200:])  # never leave an agent blocked forever: reject and record it
                handled.add((name, info.get("state_change_seq")))
                kind, target = dialog
                ok = kind.lower().startswith("shell") and SAFE.fullmatch(target or "-") is not None
                api("agent.send_keys", target=name, keys=["enter"] if ok else ["right", "right", "enter"])
                decisions.write(action="allow" if ok else "reject", agent=name, kind=kind, target=target)
            spec = next(s for s in AGENTS if s[0] == name)
            if spec[3] and name not in followed and st in ("idle", "done") and name in working_since:
                followed.add(name)
                working_since.pop(name, None)
                api("agent.prompt", target=name, text=spec[3])
                decisions.write(action="follow-up", agent=name)
            if spec[4] and name not in aborted and st == "working" and time.time() - working_since.get(name, time.time()) > spec[4]:
                aborted.add(name)
                api("agent.send_keys", target=name, keys=["esc"])
                time.sleep(0.3)
                api("agent.send_keys", target=name, keys=["esc"])
                decisions.write(action="abort", agent=name)
        finished = all(agents.get(n, {}).get("agent_status") in ("idle", "done") for n, *_ in AGENTS) and \
            "fizz" in followed and time.time() - T0 > 30
        if finished and all(seen_state.get(n) in ("idle", "done") for n, *_ in AGENTS):
            decisions.write(action="all-finished")
            break
        time.sleep(0.1)
    time.sleep(3)
    stop.set()
    json.dump({"seconds": now(), "integration": a.integration, "panes": panes,
               "explain": {n: herdr("agent", "explain", n, "--format", "text")[-400:] for n in panes}},
              open(os.path.join(a.out, "summary.json"), "w"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
