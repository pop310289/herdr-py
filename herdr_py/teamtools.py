"""Team tools: MCP servers the members write for each other. A verified answer whose first line is "ARTIFACT: mcp" is
a one-file MCP server (the rest of the answer); the engine offers it to the members' later turns as an MCP server,
run only by the sandbox command the person gave (--mcp-sandbox), never on this machine as it is.

The sandbox command is a template: {file} is the server's code, {kb} the run's knowledge base folder and {board} the
members' board folder, for example (no network, read-only, the code and the knowledge base mounted read-only):

    docker run --rm -i --network none --read-only --tmpfs /tmp:rw,size=16m --memory 256m --cpus 1 --pids-limit 64
      -v {file}:/srv/tool.py:ro -v {kb}:/kb:ro IMAGE python3 /srv/tool.py

Before a tool is offered, the engine starts it once in the sandbox and asks server/discover and tools/list (MCP
revision 2026-07-28, as Claude Code 2.1.295 asks); a tool that does not answer, or has no tools, is not offered and
says why. When a verified tool builds on another (its parents), only the newer one is offered."""
import json
import os
import queue
import shlex
import subprocess
import threading
import time

from .teamkb import TAG

KIND = "mcp"
VERSION = "2026-07-28"
PROBE_SECONDS = 30.0


def meta():
    return {"io.modelcontextprotocol/protocolVersion": VERSION,
            "io.modelcontextprotocol/clientInfo": {"name": "herdr-py", "version": "1"},
            "io.modelcontextprotocol/clientCapabilities": {}}


def probe(argv, timeout=PROBE_SECONDS):
    """Start an MCP server, ask server/discover and tools/list, stop it. Returns (tools, None) or (None, why)."""
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        return None, f"the sandbox command did not start: {exc}"
    lines = queue.Queue()

    def read():
        for raw in proc.stdout:
            lines.put(raw)
        lines.put(None)

    threading.Thread(target=read, daemon=True).start()
    deadline = time.monotonic() + timeout

    def ask(rid, method):
        proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": {"_meta": meta()}}) + "\n").encode("utf-8"))
        proc.stdin.flush()
        while True:
            try:
                raw = lines.get(timeout=max(0.05, deadline - time.monotonic()))
            except queue.Empty:
                raise RuntimeError(f"no reply to {method} in {timeout:g} s")
            if raw is None:
                raise RuntimeError(f"the server exited before it answered {method}")
            try:
                msg = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                raise RuntimeError(f"a line on stdout that is not JSON: {raw[:80]!r}")
            if isinstance(msg, dict) and msg.get("id") == rid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: error {msg['error'].get('code')} {str(msg['error'].get('message'))[:80]}")
                return msg.get("result") or {}

    try:
        found = ask(1, "server/discover")
        if VERSION not in (found.get("supportedVersions") or []):
            return None, f"server/discover does not list {VERSION}"
        tools = [t for t in ask(2, "tools/list").get("tools") or [] if isinstance(t, dict) and t.get("name")]
        if not tools:
            return None, "tools/list has no tools"
        return [{"name": str(t["name"]), "description": " ".join(str(t.get("description") or "").split())[:200]} for t in tools], None
    except (RuntimeError, OSError) as exc:
        return None, str(exc)
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


class TeamTools:
    """The team's MCP tools for one run: refresh() after the knowledge base changes; offer() for a member's turn."""

    def __init__(self, out, sandbox, kb_dir, board_dir, probe_fn=probe):
        self.dir = os.path.join(out, "tools")
        os.makedirs(self.dir, exist_ok=True)
        self.sandbox, self.kb_dir, self.board_dir, self.probe = sandbox, kb_dir, board_dir, probe_fn
        self.tools = {}  # entry id -> {"id", "server", "member", "summary", "file", "tools", "ok", "why", "parents"}
        self.lock = threading.Lock()

    def argv(self, path):
        words = shlex.split(self.sandbox)
        return [w.replace("{file}", path).replace("{kb}", self.kb_dir).replace("{board}", self.board_dir) for w in words]

    def refresh(self, entries, folder):
        """Look at the verified entries (TeamKB.entries()) of the knowledge base in folder; probe the new tools once.
        Returns the records of the tools probed now."""
        new = []
        for e in entries:
            if e.get("status") != "valid" or not e.get("artifact") or e["id"] in self.tools:
                continue
            path = os.path.join(folder, e["artifact"])
            try:
                with open(path, encoding="utf-8", errors="replace") as handle:
                    first, _, code = handle.read().partition("\n")
            except OSError:
                continue
            m = TAG.match(first)
            if not (m and m.group(2).lower() == KIND):
                continue
            file = os.path.join(self.dir, e["id"] + ".py")
            with open(file, "w", encoding="utf-8") as handle:
                handle.write(code)
            os.chmod(file, 0o644)
            tools, why = self.probe(self.argv(file))
            rec = {"id": e["id"], "server": "team_" + e["id"], "member": e.get("member"), "summary": e.get("summary") or "",
                   "file": file, "tools": tools or [], "ok": tools is not None, "why": why, "parents": list(e.get("parents") or [])}
            with self.lock:
                self.tools[e["id"]] = rec
            new.append(rec)
        return new

    def offered(self):
        """The tools a turn gets: every tool that answered, except one a newer offered tool builds on."""
        with self.lock:
            ok = {i: t for i, t in self.tools.items() if t["ok"]}
        older = {p for t in ok.values() for p in t["parents"]}
        return [t for i, t in ok.items() if i not in older]

    def config(self):
        """(path of an MCP config for Claude Code, allow rules, the lines for the member's prompt), or None."""
        tools = self.offered()
        if not tools:
            return None
        servers = {}
        for t in tools:
            argv = self.argv(t["file"])
            servers[t["server"]] = {"command": argv[0], "args": argv[1:]}
        path = os.path.join(self.dir, "mcp.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"mcpServers": servers}, handle, indent=1)
        os.replace(tmp, path)
        lines = [f"- mcp__{t['server']}__{x['name']}: {x['description'] or '(no description)'} (made by {t['member']}: {t['summary'][:120]})"
                 for t in tools for x in t["tools"]]
        return path, [f"mcp__{t['server']}" for t in tools], lines
