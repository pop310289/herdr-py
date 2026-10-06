"""A fake `opencode serve` for tests: the endpoints herdr-py calls, with events pushed by the test.

    fake = FakeOpenCode(password="pw"); fake.start()
    fake.emit("session.status", sessionID="ses_1", status={"type": "busy"})
    fake.ask_permission("ses_1", "bash", "ls")        # emits permission.asked and keeps it pending until replied
    fake.replies -> [(request_id, reply, message)]; fake.prompts; fake.aborts
"""
import base64
import itertools
import json
import queue
import threading
import urllib.parse
import socketserver
from http.server import BaseHTTPRequestHandler, HTTPServer


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):  # http.server's own needs Python 3.7
    daemon_threads = True


class FakeOpenCode:
    def __init__(self, password=None, username="opencode"):
        self.auth = None if password is None else "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
        self.ids = itertools.count(1)
        self.sessions = {}            # id -> {"id", "parentID", "title"}
        self.status = {}              # id -> {"type": ...} (only non-idle)
        self.pending = {}             # request id -> permission request
        self.questions = {}
        self.messages = {}            # session id -> list
        self.replies, self.prompts, self.aborts, self.question_rejects = [], [], [], []
        self.streams = []
        self.on_prompt = None          # callable(session_id, text) run in a thread after each prompt_async
        self.lock = threading.Lock()
        self.httpd = None

    # ---- test helpers
    def start(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n)) if n else None

            def send(self, obj, status=200):
                data = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def check(self):
                if fake.auth and self.headers.get("Authorization") != fake.auth:
                    self.send({"error": "unauthorized"}, 401)
                    return False
                return True

            def do_GET(self):
                if not self.check():
                    return
                path = urllib.parse.urlparse(self.path).path
                parts = path.strip("/").split("/")
                if path == "/global/health":
                    return self.send({"healthy": True, "version": "fake"})
                if path == "/event":
                    return fake.serve_events(self)
                if path == "/session/status":
                    return self.send(dict(fake.status))
                if path == "/permission":
                    return self.send(list(fake.pending.values()))
                if path == "/question":
                    return self.send(list(fake.questions.values()))
                if len(parts) == 2 and parts[0] == "session":
                    s = fake.sessions.get(parts[1])
                    return self.send(s) if s else self.send({"error": "not found"}, 404)
                if len(parts) == 3 and parts[0] == "session" and parts[2] == "message":
                    return self.send(fake.messages.get(parts[1], []))
                self.send({"error": "not found"}, 404)

            def do_POST(self):
                if not self.check():
                    return
                parts = urllib.parse.urlparse(self.path).path.strip("/").split("/")
                body = self.body()
                if parts == ["session"]:
                    sid = f"ses_{next(fake.ids)}"
                    fake.sessions[sid] = {"id": sid, "title": (body or {}).get("title")}
                    return self.send(fake.sessions[sid])
                if len(parts) == 3 and parts[0] == "session" and parts[2] == "prompt_async":
                    fake.prompts.append((parts[1], body))
                    self.send_response(204)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    if fake.on_prompt:  # scripted agent behaviour, after the HTTP reply like the real server
                        text = (body or {}).get("parts", [{}])[0].get("text", "")
                        threading.Thread(target=fake.on_prompt, args=(parts[1], text), daemon=True).start()
                    return
                if len(parts) == 3 and parts[0] == "session" and parts[2] == "abort":
                    fake.aborts.append(parts[1])
                    return self.send(True)
                if len(parts) == 3 and parts[0] == "permission" and parts[2] == "reply":
                    rid = parts[1]
                    req = fake.pending.pop(rid, None)
                    if req is None:
                        return self.send({"error": "no such request"}, 404)
                    fake.replies.append((rid, body.get("reply"), body.get("message")))
                    fake.emit("permission.replied", sessionID=req["sessionID"], requestID=rid, reply=body.get("reply"))
                    return self.send(True)
                if len(parts) == 3 and parts[0] == "question" and parts[2] == "reject":
                    req = fake.questions.pop(parts[1], None)
                    fake.question_rejects.append(parts[1])
                    if req:
                        fake.emit("question.rejected", sessionID=req["sessionID"], requestID=parts[1])
                    return self.send(True)
                self.send({"error": "not found"}, 404)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        return self

    def stop(self):
        with self.lock:
            for q in self.streams:
                q.put(None)
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()

    def serve_events(self, handler):
        q = queue.Queue()
        with self.lock:
            self.streams.append(q)
        handler.close_connection = True  # end of stream = closed socket, as when the real server restarts
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.end_headers()
        try:
            handler.wfile.write(b'data: {"type":"server.connected","properties":{}}\n\n')
            handler.wfile.flush()
            while True:
                event = q.get()
                if event is None:
                    return
                handler.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        finally:
            with self.lock:
                if q in self.streams:
                    self.streams.remove(q)

    def drop_streams(self):
        """Close every event stream (the client should reconnect and resync)."""
        with self.lock:
            for q in self.streams:
                q.put(None)

    def emit(self, kind, **props):
        with self.lock:
            for q in self.streams:
                q.put({"type": kind, "properties": props})

    def stream_count(self):
        with self.lock:
            return len(self.streams)

    def child(self, parent, sid=None, announce=True):
        sid = sid or f"ses_{next(self.ids)}"
        self.sessions[sid] = {"id": sid, "parentID": parent}
        if announce:
            self.emit("session.created", info={"id": sid, "parentID": parent})
        return sid

    def ask_permission(self, session_id, permission, command=None, patterns=None, announce=True):
        rid = f"per_{next(self.ids)}"
        req = {"id": rid, "sessionID": session_id, "permission": permission,
               "patterns": patterns or ([command] if command else []), "metadata": {"command": command} if command else {},
               "always": []}
        self.pending[rid] = req
        if announce:
            self.emit("permission.asked", **req)
        return rid

    def name_of(self, session_id):
        return (self.sessions.get(session_id, {}).get("title") or "").replace("herdr-py: ", "")

    def say(self, session_id, text, tools=()):
        """Append an assistant message (text plus tool parts) as GET /session/{id}/message will return it."""
        parts = [{"type": "tool", "tool": tool, "state": {"status": status, "input": inp, "output": out}}
                 for tool, status, inp, out in tools] + [{"type": "text", "text": text}]
        self.messages.setdefault(session_id, []).append({"info": {"role": "assistant"}, "parts": parts})

    def turn(self, session_id, text, tools=(), seconds=0.05):
        """A whole agent turn: busy, a message, idle."""
        import time
        self.emit("session.status", sessionID=session_id, status={"type": "busy"})
        time.sleep(seconds)
        self.say(session_id, text, tools)
        self.emit("session.status", sessionID=session_id, status={"type": "idle"})
        self.emit("session.idle", sessionID=session_id)

