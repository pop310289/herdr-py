"""A fake `opencode serve` for tests: the endpoints herdr-py calls, with events pushed by the test.

    fake = FakeOpenCode(password="pw"); fake.start()
    fake.emit("session.status", sessionID="ses_1", status={"type": "busy"})
    fake.ask_permission("ses_1", "bash", "ls")        # emits permission.asked and keeps it pending until replied
    fake.replies -> [(request_id, reply, message)]; fake.prompts; fake.aborts
FakeOpenCode(global_events=True) behaves as OpenCode 1.18 does with folders (checked on the real server, 2026-10-09):
a session made with ?directory= belongs to that folder; its events come only on GET /global/event (each wrapped as
{"directory", "project", "payload"}), never on GET /event; status, permissions and questions are answered per folder.
fake.requests -> [(method, path, directory)] for every call.
"""
import base64
import itertools
import json
import os
import queue
import threading
import urllib.parse
import socketserver
from http.server import BaseHTTPRequestHandler, HTTPServer


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):  # http.server's own needs Python 3.7
    daemon_threads = True


class FakeOpenCode:
    def __init__(self, password=None, username="opencode", global_events=False):
        self.auth = None if password is None else "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
        self.ids = itertools.count(1)
        self.sessions = {}            # id -> {"id", "parentID", "title"}
        self.status = {}              # id -> {"type": ...} (only non-idle)
        self.pending = {}             # request id -> permission request
        self.questions = {}
        self.messages = {}            # session id -> list
        self.replies, self.prompts, self.aborts, self.question_rejects = [], [], [], []
        self.streams = []             # (queue, wrapped): /global/event streams get every folder's events, wrapped
        self.global_events = global_events
        self.requests = []
        self.home = "/fake/home"       # the server's own folder
        self.unseen = False            # True: GET /file fails for every folder, as for one not mounted in a container
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

            def folder(self):
                return (urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("directory") or [None])[0]

            def do_GET(self):
                if not self.check():
                    return
                path = urllib.parse.urlparse(self.path).path
                parts = path.strip("/").split("/")
                directory = self.folder()
                fake.requests.append(("GET", path, directory))
                if path == "/global/health":
                    return self.send({"healthy": True, "version": "fake"})
                if path == "/event":
                    return fake.serve_events(self, wrapped=False)
                if path == "/global/event" and fake.global_events:
                    return fake.serve_events(self, wrapped=True)
                if path == "/file":  # what is in a folder, as this machine sees it; a missing folder is HTTP 500 as in 1.18
                    query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                    folder = os.path.join(directory or ".", (query.get("path") or ["."])[0])
                    if fake.unseen or not os.path.isdir(folder):
                        return self.send({"name": "UnknownError", "data": {"message": "Unexpected server error."}}, 500)
                    return self.send([{"name": n, "path": n, "type": "directory" if os.path.isdir(os.path.join(folder, n)) else "file"}
                                      for n in sorted(os.listdir(folder))])
                if path == "/session/status":
                    return self.send({sid: st for sid, st in fake.status.items() if fake.folder_of(sid) == directory})
                if path == "/permission":
                    return self.send([r for r in fake.pending.values() if fake.folder_of(r["sessionID"]) == directory])
                if path == "/question":
                    return self.send([q for q in fake.questions.values() if fake.folder_of(q["sessionID"]) == directory])
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
                fake.requests.append(("POST", urllib.parse.urlparse(self.path).path, self.folder()))
                if parts == ["session"]:
                    sid = f"ses_{next(fake.ids)}"
                    fake.sessions[sid] = {"id": sid, "title": (body or {}).get("title"), "directory": self.folder()}
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

    def folder_of(self, session_id):
        return self.sessions.get(session_id, {}).get("directory")

    def folder_of_event(self, event):
        props = event.get("properties") or {}
        info, part = props.get("info") or {}, props.get("part") or {}
        sid = props.get("sessionID") or part.get("sessionID") or info.get("sessionID") or info.get("id")
        return self.folder_of(sid) if sid else None

    def stop(self):
        with self.lock:
            for q, _ in self.streams:
                q.put(None)
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()

    def serve_events(self, handler, wrapped=False):
        q = queue.Queue()
        with self.lock:
            self.streams.append((q, wrapped))
        handler.close_connection = True  # end of stream = closed socket, as when the real server restarts
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.end_headers()
        try:
            hello = {"type": "server.connected", "properties": {}}
            if wrapped:
                hello = {"directory": self.home, "project": "fake", "payload": hello}
            handler.wfile.write(f"data: {json.dumps(hello)}\n\n".encode())
            handler.wfile.flush()
            while True:
                event = q.get()
                if event is None:
                    return
                folder = self.folder_of_event(event)
                if wrapped:
                    event = {"directory": folder or self.home, "project": "fake", "payload": event}
                elif folder is not None:
                    continue  # another folder's session: only /global/event carries it
                handler.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        finally:
            with self.lock:
                if (q, wrapped) in self.streams:
                    self.streams.remove((q, wrapped))

    def drop_streams(self):
        """Close every event stream (the client should reconnect and resync)."""
        with self.lock:
            for q, _ in self.streams:
                q.put(None)

    def emit(self, kind, **props):
        with self.lock:
            for q, _ in self.streams:
                q.put({"type": kind, "properties": props})

    def stream_count(self):
        with self.lock:
            return len(self.streams)

    def child(self, parent, sid=None, announce=True):
        sid = sid or f"ses_{next(self.ids)}"
        self.sessions[sid] = {"id": sid, "parentID": parent, "directory": self.folder_of(parent)}  # a subagent works where its parent does
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

