"""herdr-py command line.

    herdr-py serve --opencode http://127.0.0.1:4096 [--policy policy.toml] [--http 127.0.0.1:8765]
    herdr-py start NAME "prompt" [--budget 60] [--followup "next prompt"]...
    herdr-py list | get NAME | read NAME | pending | events | status
    herdr-py prompt NAME "text" | wait NAME --until idle | abort NAME
    herdr-py approve REQUEST_ID [--always] | reject REQUEST_ID [--message TEXT]
    herdr-py run scenario.toml      start several agents from a file and wait for all of them
    herdr-py tui                    full-screen dashboard (approve/reject/prompt/abort with the keyboard)
    herdr-py stop
"""
import argparse
import io
import json
import os
import signal
import sys
import time

from . import __version__
from .client import Client, ClientError, default_socket, default_state_dir


def out(obj, as_json):
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, indent=1))
        return
    if isinstance(obj, dict) and "agents" in obj:
        for a in obj["agents"]:
            pending = f"  pending: {len(a['pending'])}" if a["pending"] else ""
            print(f"{a['name']:<12} {a['state']:<9} {a['seconds_in_state']:>7.1f}s  {a['tokens']:>8,} tok  turns {a['turns']}{pending}")
        if not obj["agents"]:
            print("(no agents)")
    elif isinstance(obj, dict) and "pending" in obj:
        for p in obj["pending"]:
            print(f"{p['id']}  {p['agent']:<12} {'child ' if p['child'] else ''}{p['kind']}: {p['description']}")
        if not obj["pending"]:
            print("(nothing pending)")
    elif isinstance(obj, dict) and "messages" in obj:
        for m in obj["messages"]:
            if m["kind"] == "text":
                print(f"[{m['role']}] {m['text']}")
            else:
                print(f"[{m['role']}] {m['tool']} {m['status']}: {json.dumps(m['input'], ensure_ascii=False)[:120]}")
    else:
        print(json.dumps(obj, ensure_ascii=False, indent=1))


def cmd_serve(a):
    from .opencode import OpenCode, OpenCodeError, wait_healthy
    from .policy import Policy, PolicyError
    from .server import Daemon

    url = a.opencode or os.environ.get("HERDR_PY_OPENCODE")
    if not url:
        sys.exit("herdr-py serve: --opencode URL (or HERDR_PY_OPENCODE) is required")
    password = os.environ.get("HERDR_PY_OPENCODE_PASSWORD")
    if a.password_file:
        with open(a.password_file) as handle:
            password = handle.read().strip()
    try:
        policy = Policy.load(a.policy) if a.policy else Policy(default="ask")
    except (OSError, PolicyError) as exc:
        sys.exit(f"herdr-py serve: policy: {exc}")
    client = OpenCode(url, a.username or os.environ.get("HERDR_PY_OPENCODE_USERNAME"), password)
    try:
        health = wait_healthy(client, timeout=a.wait)
    except OpenCodeError as exc:
        sys.exit(f"herdr-py serve: {exc}")
    socket_path = a.socket or os.environ.get("HERDR_PY_SOCKET") or os.path.join(a.state_dir, "herdr-py.sock")
    try:
        Client(socket_path, timeout=2).call("ping")
        sys.exit(f"herdr-py serve: a daemon is already running at {socket_path}")
    except ClientError:
        pass
    daemon = Daemon(client, policy, a.state_dir, socket_path=socket_path, http_addr=a.http, model=a.model, questions=a.questions,
                    max_agents=a.max_agents, max_prompts=a.max_prompts)
    reattached = daemon.start()
    print(f"herdr-py {__version__}: OpenCode {health.get('version')} at {url}", flush=True)
    print(f"socket: {socket_path}   state: {a.state_dir}" + (f"   re-attached {reattached} agent(s)" if reattached else ""), flush=True)
    if a.http:
        print(f"web UI: http://{a.http}/#token={daemon.token}", flush=True)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: daemon.stop())
    daemon.wait()
    return 0


def load_scenario(path):
    from .policy import PolicyError, load_config
    try:
        data = load_config(path)
    except (OSError, ValueError, PolicyError) as exc:
        sys.exit(f"herdr-py run: {exc}")
    agents = data.get("agent") or []
    if not agents:
        sys.exit(f"herdr-py run: {path} has no [[agent]] entries")
    return data, agents


def cmd_run(a, client):
    data, agents = load_scenario(a.file)
    started = time.time()
    for spec in agents:
        client.call("agent.start", name=spec["name"], prompt=spec["prompt"], budget_s=spec.get("budget_s"),
                    followups=spec.get("followups") or [], model=spec.get("model") or data.get("model"))
        print(f"started {spec['name']}", flush=True)
    names = {s["name"] for s in agents}
    deadline = started + float(a.timeout or data.get("timeout_s", 1800))
    while True:
        views = {v["name"]: v for v in client.call("agent.list")["agents"] if v["name"] in names}
        finished = {n for n, v in views.items() if v["state"] in ("idle", "aborted", "error") and v["followups_left"] == 0
                    and not v["pending"] and v["turns"] > 0}
        if finished == names:
            break
        if time.time() > deadline:
            for n in names - finished:
                client.call("agent.abort", name=n, reason="scenario timeout")
            print("scenario timeout: aborted " + ", ".join(sorted(names - finished)), flush=True)
            break
        time.sleep(0.5)
    summary = {"seconds": round(time.time() - started, 1), "agents": [client.call("agent.get", name=s["name"]) for s in agents]}
    for v in summary["agents"]:
        print(f"{v['name']:<12} {v['state']:<8} turns {v['turns']}  tokens {v['tokens']:,}", flush=True)
    if a.summary:
        with open(a.summary, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, ensure_ascii=False, indent=1)
    return 0


def utf8_stdout():
    """Python 3.6 under LANG=C (cron, systemd, ssh without a locale) writes ASCII and crashes on any CJK text."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if (getattr(stream, "encoding", "") or "").lower().replace("-", "") != "utf8" and hasattr(stream, "buffer"):
            setattr(sys, name, io.TextIOWrapper(stream.buffer, encoding="utf-8", errors="replace", line_buffering=True))


def main(argv=None):
    utf8_stdout()
    ap = argparse.ArgumentParser(prog="herdr-py", description="Drive many OpenCode agents with live state from OpenCode's own events.")
    ap.add_argument("--version", action="version", version=f"herdr-py {__version__}")
    ap.add_argument("--socket", default=None, help="daemon socket (default: $HERDR_PY_SOCKET or <state dir>/herdr-py.sock)")
    ap.add_argument("--json", action="store_true", help="print raw JSON")
    sub = ap.add_subparsers(dest="cmd")
    sub.required = True  # (the keyword argument needs Python 3.7)

    s = sub.add_parser("serve", help="run the daemon")
    s.add_argument("--opencode", help="OpenCode server URL, e.g. http://127.0.0.1:4096 (or $HERDR_PY_OPENCODE)")
    s.add_argument("--username", help="OpenCode server username (default: opencode)")
    s.add_argument("--password-file", help="file holding the OpenCode server password (or $HERDR_PY_OPENCODE_PASSWORD)")
    s.add_argument("--policy", help="permission policy (TOML); without one every request waits for a human")
    s.add_argument("--http", help="serve the web UI at HOST:PORT, e.g. 127.0.0.1:8765")
    s.add_argument("--state-dir", default=default_state_dir())
    s.add_argument("--model", help="default model, provider/model")
    s.add_argument("--questions", choices=["ask", "reject"], default="ask", help="what to do with questions agents ask")
    s.add_argument("--wait", type=float, default=30, help="seconds to wait for OpenCode to become healthy")
    s.add_argument("--max-agents", type=int, help="refuse to start more agents than this (guards against a runaway manager)")
    s.add_argument("--max-prompts", type=int, help="refuse more than this many prompts per agent")

    sub.add_parser("status", help="is the daemon running?")
    sub.add_parser("list", help="list agents")
    g = sub.add_parser("get", help="one agent")
    g.add_argument("name")
    st = sub.add_parser("start", help="create an agent (an OpenCode session) and send its first prompt")
    st.add_argument("name")
    st.add_argument("prompt")
    st.add_argument("--budget", type=float, help="abort after this many seconds of continuous work")
    st.add_argument("--followup", action="append", default=[], help="prompt to send when the agent goes idle (repeatable)")
    st.add_argument("--model")
    st.add_argument("--file", action="append", default=[], help="attach a file (e.g. an image for a vision model); repeatable")
    st.add_argument("--wait", action="store_true", help="return when the first turn finishes")
    st.add_argument("--timeout", type=float, help="with --wait: give up after this many seconds")
    pr = sub.add_parser("prompt", help="send another prompt")
    pr.add_argument("name")
    pr.add_argument("text")
    pr.add_argument("--file", action="append", default=[], help="attach a file; repeatable")
    pr.add_argument("--wait", action="store_true", help="return when the turn this prompt starts has finished")
    pr.add_argument("--timeout", type=float, help="with --wait: give up after this many seconds")
    w = sub.add_parser("wait", help="wait until an agent reaches a state")
    w.add_argument("name")
    w.add_argument("--until", action="append", default=[], help="state to wait for (repeatable; default idle)")
    w.add_argument("--timeout", type=float)
    r = sub.add_parser("read", help="the agent's conversation")
    r.add_argument("name")
    r.add_argument("--limit", type=int, default=20)
    ab = sub.add_parser("abort", help="interrupt an agent")
    ab.add_argument("name")
    pe = sub.add_parser("pending", help="requests waiting for a human")
    pe.add_argument("name", nargs="?")
    ap_ = sub.add_parser("approve", help="allow a pending permission request")
    ap_.add_argument("id")
    ap_.add_argument("--always", action="store_true", help="allow this pattern for the rest of the session")
    rj = sub.add_parser("reject", help="reject a pending permission request (or dismiss a question)")
    rj.add_argument("id")
    rj.add_argument("--message", help="reason the agent will read")
    sub.add_parser("events", help="stream events as JSON lines")
    ru = sub.add_parser("run", help="start the agents in a scenario file and wait for all of them")
    ru.add_argument("file")
    ru.add_argument("--timeout", type=float)
    ru.add_argument("--summary", help="write a JSON summary here")
    tm = sub.add_parser("team", help="run one task under a condition: S single, N nudged, T supervised team")
    tm.add_argument("task", help="task file (.json, or .toml on Python 3.11+): name, prompt, check")
    tm.add_argument("--condition", choices=["S", "N", "T"], required=True)
    tm.add_argument("--workdir", default=".", help="where the agents work and the check runs")
    tm.add_argument("--rounds", type=int, default=3)
    tm.add_argument("--wall", type=float, default=720, help="wall-clock limit in seconds")
    tm.add_argument("--stall", type=float, default=120, help="T: seconds without progress before the executor is replaced")
    tm.add_argument("--checkpoint", type=float, default=240, help="T: interrupt and check an executor that has worked this long without stopping")
    tm.add_argument("--summary", help="write the run summary (JSON) here")
    tm.add_argument("--log", help="append the supervisor's decisions (JSON lines) here")
    sub.add_parser("tui", help="full-screen dashboard")
    sub.add_parser("stop", help="stop the daemon (agents' sessions stay in OpenCode)")
    a = ap.parse_args(argv)

    if a.cmd == "serve":
        a.socket = a.socket
        return cmd_serve(a)
    client = Client(a.socket or default_socket())
    try:
        if a.cmd == "status":
            out(client.call("ping"), True)
        elif a.cmd == "list":
            out(client.call("agent.list"), a.json)
        elif a.cmd == "get":
            out(client.call("agent.get", name=a.name), True)
        elif a.cmd == "start":
            out(client.call("agent.start", name=a.name, prompt=a.prompt, budget_s=a.budget, followups=a.followup, model=a.model,
                            wait=a.wait, timeout_s=a.timeout, files=[os.path.abspath(f) for f in a.file]), a.json)
        elif a.cmd == "prompt":
            out(client.call("agent.prompt", name=a.name, text=a.text, wait=a.wait, timeout_s=a.timeout,
                            files=[os.path.abspath(f) for f in a.file]), a.json)
        elif a.cmd == "wait":
            out(client.call("agent.wait", name=a.name, until=a.until or ["idle"], timeout_s=a.timeout), a.json)
        elif a.cmd == "read":
            out(client.call("agent.read", name=a.name, limit=a.limit), a.json)
        elif a.cmd == "abort":
            out(client.call("agent.abort", name=a.name), a.json)
        elif a.cmd == "pending":
            out(client.call("permission.list", name=a.name), a.json)
        elif a.cmd == "approve":
            out(client.call("permission.reply", id=a.id, reply="always" if a.always else "once", by="cli"), a.json)
        elif a.cmd == "reject":
            out(client.call("permission.reply", id=a.id, reply="reject", message=a.message, by="cli"), a.json)
        elif a.cmd == "events":
            for event in client.events():
                print(json.dumps(event, ensure_ascii=False), flush=True)
        elif a.cmd == "run":
            return cmd_run(a, client)
        elif a.cmd == "team":
            from .team import TeamRun, load_task
            summary = TeamRun(client, load_task(a.task), a.condition, os.path.abspath(a.workdir), rounds=a.rounds,
                              wall_s=a.wall, stall_s=a.stall, checkpoint_s=a.checkpoint, log_path=a.log).run()
            if a.summary:
                with open(a.summary, "w", encoding="utf-8") as handle:
                    json.dump(summary, handle, ensure_ascii=False, indent=1)
            print(f"{summary['task']} {summary['condition']}: {summary['outcome']}, check {'passed' if summary['final_check'] else 'failed'}, "
                  f"{summary['seconds']}s, {summary['tokens']:,} tokens", flush=True)
        elif a.cmd == "tui":
            from .tui import run_tui
            return run_tui(client)
        elif a.cmd == "stop":
            out(client.call("server.stop"), a.json)
    except ClientError as exc:
        print(f"herdr-py: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0
