"""The herdr-py daemon: owns the hub, follows OpenCode's event stream, and serves two front doors.

1. A Unix socket speaking newline-delimited JSON (requests `{"id", "method", "params"}`; responses `{"id", "result"}` or
   `{"id", "error": {"code", "message"}}`). Method names follow herdr's style: ping, agent.list, agent.get, agent.start,
   agent.prompt, agent.abort, agent.wait, agent.read, permission.list, permission.reply, events.subscribe, server.stop.
2. An optional HTTP server for the web UI: static files plus /api/* (JSON and Server-Sent Events). Every /api call needs
   the token from <state dir>/token (header `Authorization: Bearer <token>`, or `?token=` for EventSource).

Clients (CLI, TUI, web UI) can come and go: agents keep running in OpenCode and the daemon keeps answering permissions.
"""
import hmac
import http.server
import json
import os
import queue
import secrets
import socketserver
import threading
import time
import urllib.parse

from . import __version__
from .core import Hub, HubError
from .opencode import OpenCodeError, start_stream_thread

try:
    ThreadingHTTPServer = http.server.ThreadingHTTPServer
except AttributeError:  # Python 3.6 (RHEL 8's platform-python)
    class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8")}


class Daemon:
    def __init__(self, client, policy, state_dir, socket_path=None, http_addr=None, model=None, questions="ask",
                 tick_s=0.25, max_agents=None, max_prompts=None):
        os.makedirs(state_dir, exist_ok=True)
        self.state_dir = state_dir
        self.socket_path = socket_path or os.path.join(state_dir, "herdr-py.sock")
        self.http_addr = http_addr
        self.hub = Hub(client, policy, log_path=os.path.join(state_dir, "events.jsonl"),
                       state_path=os.path.join(state_dir, "state.json"), model=model, questions=questions,
                       max_agents=max_agents, max_prompts=max_prompts)
        self.client = client
        self.tick_s = tick_s
        self.stopping = threading.Event()
        self.token = self.load_token()
        self.unix = self.web = None
        self.started = time.time()

    def load_token(self):
        path = os.path.join(self.state_dir, "token")
        if not os.path.exists(path):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(secrets.token_urlsafe(24))
        with open(path) as handle:
            return handle.read().strip()

    # ------------------------------------------------------------ dispatch shared by both front doors
    def call(self, method, params):
        hub = self.hub
        p = params or {}
        if method == "ping":
            return {"pong": True, "version": __version__, "opencode": self.client.url, "connected": hub.connected,
                    "agents": len(hub.agents), "uptime_s": round(time.time() - self.started, 1)}
        if method == "agent.list":
            return {"agents": hub.list()}
        if method == "agent.get":
            return hub.get(p["name"])
        if method == "agent.start":
            view = hub.start(p["name"], p["prompt"], budget_s=p.get("budget_s"), followups=p.get("followups") or [],
                             model=p.get("model"), title=p.get("title"), files=p.get("files") or [])
            if p.get("wait"):
                return hub.wait(p["name"], until=p.get("until") or ["idle", "aborted", "error"], timeout=p.get("timeout_s"))
            return view
        if method == "agent.prompt":
            if p.get("wait"):
                return hub.prompt_and_wait(p["name"], p["text"], until=p.get("until") or ["idle"], timeout=p.get("timeout_s"),
                                           files=p.get("files") or [])
            return hub.prompt(p["name"], p["text"], files=p.get("files") or [])
        if method == "agent.abort":
            return hub.abort(p["name"], reason=p.get("reason", "user"))
        if method == "agent.wait":
            until = p.get("until") or ["idle"]
            return hub.wait(p["name"], until=[until] if isinstance(until, str) else until, timeout=p.get("timeout_s"))
        if method == "agent.read":
            return {"messages": hub.transcript(p["name"], limit=int(p.get("limit", 20)))}
        if method == "permission.list":
            return {"pending": hub.pending(p.get("name"))}
        if method == "permission.reply":
            return hub.reply(p["id"], p.get("reply", "once"), p.get("message"), by=p.get("by", "human"))
        if method == "server.stop":
            threading.Thread(target=self.stop, daemon=True).start()
            return {"stopping": True}
        raise HubError(f"unknown method {method!r}")

    # ------------------------------------------------------------ lifecycle
    def start(self):
        reattached = self.hub.load()
        self.stream_stop, _ = start_stream_thread(self.client, self.hub.on_event, self.hub.on_stream_state)
        threading.Thread(target=self.ticker, daemon=True, name="herdr-py-tick").start()
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)  # a stale socket from a crashed daemon; a live one would have refused to start
        daemon = self

        class UnixHandler(socketserver.StreamRequestHandler):
            def handle(self):
                daemon.serve_connection(self.rfile, self.wfile)

        class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        self.unix = UnixServer(self.socket_path, UnixHandler)
        os.chmod(self.socket_path, 0o600)
        threading.Thread(target=self.unix.serve_forever, daemon=True, name="herdr-py-unix").start()
        if self.http_addr:
            host, _, port = self.http_addr.rpartition(":")
            self.web = ThreadingHTTPServer((host or "127.0.0.1", int(port)), make_http_handler(self))
            self.web.daemon_threads = True
            threading.Thread(target=self.web.serve_forever, daemon=True, name="herdr-py-http").start()
        return reattached

    def ticker(self):
        while not self.stopping.wait(self.tick_s):
            try:
                self.hub.tick()
            except Exception as exc:  # keep ticking, but leave a trace
                self.hub.record({"hub": "tick_error", "error": f"{type(exc).__name__}: {exc}"})

    def stop(self):
        if self.stopping.is_set():
            return
        self.stopping.set()
        self.stream_stop.set()
        for server in (self.unix, self.web):
            if server:
                server.shutdown()
                server.server_close()
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        self.hub.save()
        self.hub.close()

    def wait(self):
        self.stopping.wait()

    # ------------------------------------------------------------ unix socket protocol
    def serve_connection(self, rfile, wfile):
        lock = threading.Lock()

        def send(obj):
            data = (json.dumps(obj, ensure_ascii=False) + "\n").encode()
            with lock:
                wfile.write(data)
                wfile.flush()

        for raw in rfile:
            try:
                req = json.loads(raw)
                rid, method, params = req.get("id"), req["method"], req.get("params") or {}
            except (ValueError, KeyError, AttributeError) as exc:
                send({"id": None, "error": {"code": "bad_request", "message": str(exc)}})
                continue
            if method == "events.subscribe":
                self.stream_events(send, rid)
                return
            try:
                send({"id": rid, "result": self.call(method, params)})
            except (HubError, OpenCodeError, KeyError, ValueError, TypeError, OSError) as exc:
                code = "missing_param" if isinstance(exc, KeyError) else type(exc).__name__.lower()
                send({"id": rid, "error": {"code": code, "message": str(exc)}})

    def stream_events(self, send, rid):
        q = self.hub.subscribe()
        try:
            send({"id": rid, "result": {"subscribed": True, "agents": self.hub.list()}})
            while not self.stopping.is_set():
                try:
                    event = q.get(timeout=1.0)
                except queue.Empty:
                    continue
                send({"event": event})
                if event.get("type") == "events_lost":
                    return
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        finally:
            self.hub.unsubscribe(q)


def make_http_handler(daemon):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = f"herdr-py/{__version__}"

        def log_message(self, fmt, *args):  # keep the daemon's stdout quiet
            pass

        def authorized(self, query):
            header = self.headers.get("Authorization", "")
            given = header[7:] if header.startswith("Bearer ") else (query.get("token") or [""])[0]
            return hmac.compare_digest(given.encode(), daemon.token.encode())

        def reply(self, status, obj):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(url.query)
            if url.path in STATIC:
                name, ctype = STATIC[url.path]
                with open(os.path.join(WEB_DIR, name), "rb") as handle:
                    body = handle.read()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if not url.path.startswith("/api/"):
                return self.reply(404, {"error": "not found"})
            if not self.authorized(query):
                return self.reply(401, {"error": "missing or wrong token"})
            parts = url.path.strip("/").split("/")
            try:
                if parts == ["api", "agents"]:
                    return self.reply(200, daemon.call("agent.list", {}))
                if len(parts) == 4 and parts[:2] == ["api", "agents"] and parts[3] == "transcript":
                    return self.reply(200, daemon.call("agent.read", {"name": urllib.parse.unquote(parts[2])}))
                if parts == ["api", "status"]:
                    return self.reply(200, daemon.call("ping", {}))
                if parts == ["api", "events"]:
                    return self.sse()
            except (HubError, OpenCodeError, KeyError) as exc:
                return self.reply(400, {"error": str(exc)})
            return self.reply(404, {"error": "not found"})

        def do_POST(self):
            url = urllib.parse.urlparse(self.path)
            if not self.authorized(urllib.parse.parse_qs(url.query)):
                return self.reply(401, {"error": "missing or wrong token"})
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            except ValueError:
                return self.reply(400, {"error": "body must be JSON"})
            parts = url.path.strip("/").split("/")
            try:
                if parts == ["api", "agents"]:
                    return self.reply(200, daemon.call("agent.start", body))
                if len(parts) == 4 and parts[:2] == ["api", "agents"] and parts[3] in ("prompt", "abort"):
                    name = urllib.parse.unquote(parts[2])
                    method = "agent.prompt" if parts[3] == "prompt" else "agent.abort"
                    return self.reply(200, daemon.call(method, {"name": name, **body}))
                if len(parts) == 3 and parts[:2] == ["api", "permissions"]:
                    return self.reply(200, daemon.call("permission.reply", {"id": parts[2], "by": "web", **body}))
            except (HubError, OpenCodeError, KeyError, ValueError) as exc:
                return self.reply(400, {"error": str(exc)})
            return self.reply(404, {"error": "not found"})

        def sse(self):
            q = daemon.hub.subscribe()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                snapshot = {"type": "snapshot", "agents": daemon.hub.list(), "status": daemon.call("ping", {})}
                self.wfile.write(f"data: {json.dumps(snapshot, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()
                while not daemon.stopping.is_set():
                    try:
                        event = q.get(timeout=15)
                    except queue.Empty:
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                        continue
                    self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode())
                    self.wfile.flush()
                    if event.get("type") == "events_lost":
                        return
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
            finally:
                daemon.hub.unsubscribe(q)

    return Handler
