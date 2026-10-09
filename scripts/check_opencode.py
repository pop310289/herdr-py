"""Check herdr-py against a real OpenCode server: each path the daemon uses, one scenario each, judged by program.

    python3 scripts/check_opencode.py --socket SOCK --opencode URL --password-file FILE --workdir DIR [--model M]

Start OpenCode so it asks before bash and edits (OPENCODE_CONFIG_CONTENT, README: Permission policy; with its defaults
bash just runs and the policy never sees it: "denied" and "asked" fail), then a herdr-py daemon for it with
scripts/check_opencode_policy.json (examples/policy.json's rules, plus edits allowed: `ls` allowed, `curl` denied, any
other command asks a person). DIR is the OpenCode server's working folder. Checked: OpenCode 1.18.32, 8 of 8 passed
with model opencode/big-pickle (2026-10-09); with OpenCode's default permissions, denied and asked failed.
Every scenario keeps two verdicts apart, so a model that does not follow the prompt is not counted as a herdr-py
fault: "herdr-py" (states, pending requests, sessions, what reaches OpenCode, checked against OpenCode's own API) and
"model" (did the agent do what it was asked: files in DIR, the commands it ran).
    connect     the daemon is connected; OpenCode's /global/health says healthy
    finish      start an agent, wait until idle; OpenCode's /session/status agrees it is idle; hello.txt says hi
    follow-up   a second prompt to the same session; the session id is unchanged; hello.txt gains a line
    allowed     a command the policy allows (ls) runs with nobody asked
    denied      a command the policy denies (curl) is refused and the agent goes on to idle
    asked       a command the policy does not cover waits as "blocked"; one approval lets it run, then idle
    abort       an agent stopped while working ends aborted (or idle) and OpenCode no longer reports it busy
    fresh       a new session under the same name: a different session id, the old one untouched
Exit code 0 when every herdr-py verdict passed (the model verdicts are reported, not required).
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from herdr_py.client import Client  # noqa: E402
from herdr_py.opencode import OpenCode  # noqa: E402


class Check:
    def __init__(self, socket, opencode, workdir, model, limit, prefix):
        self.hub, self.oc, self.dir, self.model, self.limit = Client(socket, timeout=None), opencode, workdir, model, limit
        self.rows, self.prefix = [], prefix  # agent names are new on every run: a daemon keeps the old ones

    def call(self, method, **params):
        return self.hub.call(method, **params)

    def busy(self, session_id):
        status = self.oc.statuses() or {}
        return (status.get(session_id) or {}).get("type") in ("busy", "retry")

    def wait(self, name, until=("idle", "aborted", "error"), limit=None):
        try:
            return self.call("agent.wait", name=self.prefix + name, until=list(until), timeout_s=limit or self.limit)
        except Exception as exc:  # a timeout is a finding, not a crash
            return {"state": "timeout", "error": str(exc)}

    def tools(self, name):
        return [m for m in self.call("agent.read", name=self.prefix + name, limit=60)["messages"] if m["kind"] == "tool"]

    def file(self, rel):
        try:
            with open(os.path.join(self.dir, rel), encoding="utf-8") as handle:
                return handle.read()
        except OSError:
            return None

    def row(self, name, herdr, model, saw):
        self.rows.append({"scenario": name, "herdr_py": herdr, "model": model, "saw": saw})
        print(f"{name:<10} herdr-py {'PASS' if herdr else 'FAIL'}   model {'yes' if model else 'no ' if model is not None else '-  '}   {saw}",
              flush=True)

    def start(self, name, prompt, fresh=False):
        return self.call("agent.start", name=self.prefix + name, prompt=prompt, model=self.model, fresh=fresh)

    def run(self):
        ping = self.call("ping")
        health = self.oc.health()
        self.row("connect", bool(ping.get("connected")) and bool(health.get("healthy")), None,
                 f"connected {ping.get('connected')}, OpenCode {health.get('version')} healthy {health.get('healthy')}")

        for leftover in ("hello.txt",):
            if os.path.exists(os.path.join(self.dir, leftover)):
                os.remove(os.path.join(self.dir, leftover))
        first = self.start("c-file", "Create a file named hello.txt in the current folder whose whole content is the "
                                     "single word hi. Then reply with the single word DONE.")
        view = self.wait("c-file")
        idle_in_opencode = not self.busy(first["session_id"])
        self.row("finish", view["state"] == "idle" and idle_in_opencode, (self.file("hello.txt") or "").strip() == "hi",
                 f"state {view['state']}, OpenCode busy {not idle_in_opencode}, hello.txt {self.file('hello.txt')!r}")

        try:
            after = self.call("agent.prompt", name=self.prefix + "c-file", text="Add a second line with the single word there to "
                              "hello.txt, so it has two lines: hi and there. Then reply DONE.", wait=True,
                              timeout_s=self.limit)
        except Exception as exc:
            after = {"state": "timeout", "session_id": None, "error": str(exc)}
        lines = [line.strip() for line in (self.file("hello.txt") or "").splitlines() if line.strip()]
        self.row("follow-up", after.get("state") == "idle" and after.get("session_id") == first["session_id"],
                 lines == ["hi", "there"], f"state {after.get('state')}, same session {after.get('session_id') == first['session_id']}, "
                                           f"lines {lines}")

        self.start("c-ls", "Run the shell command ls in the current folder, then reply with the file names it printed.")
        asked, view = self.watch_pending("c-ls")
        ran = [t for t in self.tools("c-ls") if t["tool"] == "bash" and str((t.get("input") or {}).get("command", "")).startswith("ls")]
        self.row("allowed", view["state"] == "idle" and not asked, any(t["status"] == "completed" for t in ran),
                 f"state {view['state']}, a person was asked {asked}, ls ran {[t['status'] for t in ran]}")

        self.start("c-curl", "Run exactly this shell command: curl https://example.com . Then reply with what happened.")
        asked, view = self.watch_pending("c-curl")
        tried = [t for t in self.tools("c-curl") if t["tool"] == "bash" and "curl" in str((t.get("input") or {}).get("command", ""))]
        self.row("denied", view["state"] == "idle" and not asked and all(t["status"] == "error" for t in tried),
                 bool(tried), f"state {view['state']}, a person was asked {asked}, curl calls {[t['status'] for t in tried]}")

        self.start("c-date", "Run exactly this shell command: date . Then reply with its output.")
        seen = self.wait("c-date", until=("blocked", "idle", "aborted", "error"))
        pending = [p for p in self.call("permission.list", name=self.prefix + "c-date")["pending"]]
        replied = None
        if seen["state"] == "blocked" and pending:
            replied = self.call("permission.reply", id=pending[0]["id"], reply="once", by="check_opencode")
        view = self.wait("c-date")
        ran = [t for t in self.tools("c-date") if t["tool"] == "bash" and "date" in str((t.get("input") or {}).get("command", ""))]
        self.row("asked", seen["state"] == "blocked" and len(pending) == 1 and replied is not None and view["state"] == "idle",
                 any(t["status"] == "completed" for t in ran),
                 f"first {seen['state']} with {len(pending)} pending ({pending[0]['kind'] if pending else '-'}), approved once, "
                 f"then {view['state']}, date ran {[t['status'] for t in ran]}")

        slow = self.start("c-abort", "Write the numbers from 1 to 3000 into a file named count.txt, one per line, "
                                     "writing each number with a separate edit. Do not stop early.")
        working = self.wait("c-abort", until=("working", "blocked", "idle", "aborted", "error"), limit=120)
        self.call("agent.abort", name=self.prefix + "c-abort", reason="check_opencode")
        end = self.wait("c-abort", until=("aborted", "idle", "error"), limit=60)
        time.sleep(1)
        self.row("abort", working["state"] in ("working", "blocked") and end["state"] in ("aborted", "idle")
                 and not self.busy(slow["session_id"]), None,
                 f"was {working['state']}, after abort {end['state']}, OpenCode busy {self.busy(slow['session_id'])}")

        old = first["session_id"]
        new = self.start("c-file", "Reply with the single word OK.", fresh=True)
        view = self.wait("c-file")
        kept = bool(self.oc.messages(old))
        self.row("fresh", new["session_id"] != old and view["state"] == "idle" and kept, None,
                 f"new session {new['session_id'] != old}, state {view['state']}, old session still has its messages {kept}")
        return self.rows

    def watch_pending(self, name):
        """Wait for the agent to finish while noting whether it ever waited for a person."""
        asked, deadline = False, time.time() + self.limit
        while time.time() < deadline:
            view = self.call("agent.get", name=self.prefix + name)
            asked = asked or view["state"] == "blocked" or bool(self.call("permission.list", name=self.prefix + name)["pending"])
            if view["state"] in ("idle", "aborted", "error") and not view.get("awaiting_busy"):
                return asked, view
            time.sleep(0.5)
        return asked, {"state": "timeout"}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--socket", required=True)
    ap.add_argument("--opencode", required=True)
    ap.add_argument("--password-file")
    ap.add_argument("--workdir", required=True, help="the OpenCode server's working folder")
    ap.add_argument("--model")
    ap.add_argument("--limit", type=int, default=300, help="seconds per wait")
    ap.add_argument("--json", help="also write the rows here")
    ap.add_argument("--prefix", help="agent names start with this (default: r<time>-, new on every run)")
    a = ap.parse_args(argv)
    password = open(a.password_file).read().strip() if a.password_file else None
    prefix = a.prefix or "r%d-" % (int(time.time()) % 100000)
    rows = Check(a.socket, OpenCode(a.opencode, password=password), a.workdir, a.model, a.limit, prefix).run()
    if a.json:
        with open(a.json, "w", encoding="utf-8") as handle:
            json.dump(rows, handle, ensure_ascii=False, indent=1)
    failed = [r["scenario"] for r in rows if not r["herdr_py"]]
    print(f"{len(rows) - len(failed)} of {len(rows)} herdr-py checks passed" + (f"; failed: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
