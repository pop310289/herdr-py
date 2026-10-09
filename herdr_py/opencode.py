"""OpenCode server client (`opencode serve`): HTTP calls and the SSE event stream.

Only the endpoints herdr-py needs, checked against OpenCode 1.18.32 (`GET /doc`):
POST /session, POST /session/{id}/prompt_async, POST /session/{id}/abort, GET /session/{id},
GET /session/{id}/message, GET /session/status, GET /permission, POST /permission/{id}/reply,
GET /question, POST /question/{id}/reject, GET /global/health, GET /event (SSE).
"""
import base64
import http.client
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


class OpenCodeError(Exception):
    pass


# The permission kinds a herdr-py policy answers. OpenCode sends a request only for a kind its own config sets to "ask";
# with its defaults (checked with OpenCode 1.18.32) bash and edits just run, and the policy never sees them.
ASKED = ("edit", "bash", "webfetch", "external_directory", "doom_loop")
ASK_CONFIG = '{"permission": {"edit": "ask", "bash": "ask", "webfetch": "ask", "external_directory": "ask", "doom_loop": "ask"}}'


def unasked(client):
    """The kinds OpenCode does not ask about, as "kind=value" (value "unset" when its config leaves it out), from its
    GET /config; None when the config cannot be read. A bash table with any action that is not "ask" counts."""
    try:
        config = client.call("GET", "/config") or {}
    except OpenCodeError:
        return None
    permission = config.get("permission") if isinstance(config.get("permission"), dict) else {}
    out = []
    for kind in ASKED:
        value = permission.get(kind)
        values = list(value.values()) if isinstance(value, dict) else [value]
        if not values or any(v != "ask" for v in values):
            out.append(f"{kind}={json.dumps(value) if value is not None else 'unset'}")
    return out


class OpenCode:
    def __init__(self, url, username=None, password=None, timeout=30):
        self.url = url.rstrip("/")
        parsed = urllib.parse.urlparse(self.url)
        self.host, self.port = parsed.hostname or "127.0.0.1", parsed.port or 80
        self.headers = {"Content-Type": "application/json"}
        if password:
            token = base64.b64encode(f"{username or 'opencode'}:{password}".encode()).decode()
            self.headers["Authorization"] = "Basic " + token
        self.timeout = timeout

    def call(self, method, path, body=None, timeout=None):
        req = urllib.request.Request(self.url + path, method=method, headers=self.headers,
                                     data=None if body is None else json.dumps(body).encode())
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                data = resp.read()
        except urllib.error.HTTPError as exc:
            raise OpenCodeError(f"{method} {path} -> HTTP {exc.code}: {exc.read()[:300].decode(errors='replace')}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise OpenCodeError(f"{method} {path} -> {exc}") from None
        return json.loads(data) if data else None

    # --- sessions -------------------------------------------------------
    def health(self):
        return self.call("GET", "/global/health", timeout=5)

    def create_session(self, title):
        return self.call("POST", "/session", {"title": title})

    def session(self, session_id):
        return self.call("GET", f"/session/{session_id}")

    def prompt(self, session_id, text, model=None, agent=None, files=()):
        """files: [{"mime": "image/png", "url": "data:...", "filename": "x.png"}] (OpenCode's FilePartInput)."""
        body = {"parts": [{"type": "text", "text": text}] + [dict(f, type="file") for f in files]}
        if model:
            provider, _, model_id = model.partition("/")
            body["model"] = {"providerID": provider, "modelID": model_id}
        if agent:
            body["agent"] = agent
        return self.call("POST", f"/session/{session_id}/prompt_async", body)

    def abort(self, session_id):
        return self.call("POST", f"/session/{session_id}/abort")

    def messages(self, session_id):
        return self.call("GET", f"/session/{session_id}/message")

    def statuses(self):
        """{session_id: {"type": "idle"|"busy"|"retry", ...}} for sessions the server knows are not idle."""
        return self.call("GET", "/session/status") or {}

    # --- permissions and questions -------------------------------------
    def pending_permissions(self):
        return self.call("GET", "/permission") or []

    def reply_permission(self, request_id, reply, message=None):
        body = {"reply": reply}
        if message:
            body["message"] = message
        return self.call("POST", f"/permission/{request_id}/reply", body)

    def pending_questions(self):
        return self.call("GET", "/question") or []

    def reject_question(self, request_id):
        return self.call("POST", f"/question/{request_id}/reject")

    # --- events ---------------------------------------------------------
    def stream(self, on_event, stop, on_state=None, retry=1.0, max_retry=15.0):
        """Read GET /event until `stop` is set, reconnecting with backoff.

        on_event(event_dict) is called for every event. on_state("connected"|"disconnected", detail) lets the
        caller resynchronise after a reconnect (events sent while disconnected are not replayed by OpenCode).
        """
        delay = retry
        while not stop.is_set():
            conn = None
            try:
                conn = http.client.HTTPConnection(self.host, self.port, timeout=60)
                conn.request("GET", "/event", headers={k: v for k, v in self.headers.items() if k != "Content-Type"})
                resp = conn.getresponse()
                if resp.status != 200:
                    raise OpenCodeError(f"GET /event -> HTTP {resp.status}")
                if on_state:
                    on_state("connected", "")
                delay = retry
                data = []
                while not stop.is_set():
                    line = resp.readline()  # the server sends a heartbeat event every few seconds
                    if not line:
                        raise OpenCodeError("event stream closed")
                    line = line.decode("utf-8", "replace").rstrip("\r\n")
                    if line.startswith("data:"):
                        data.append(line[5:].lstrip())
                    elif not line and data:
                        try:
                            event = json.loads("\n".join(data))
                        except ValueError:
                            event = None
                        data = []
                        if event is not None:
                            on_event(event)
            except Exception as exc:  # network errors, timeouts, server restarts: report and reconnect
                if stop.is_set():
                    break
                if on_state:
                    on_state("disconnected", f"{type(exc).__name__}: {exc}")
                stop.wait(delay)
                delay = min(max_retry, delay * 2)
            finally:
                if conn is not None:
                    conn.close()


def wait_healthy(client, timeout=30):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            return client.health()
        except OpenCodeError as exc:
            last = exc
            time.sleep(0.5)
    raise OpenCodeError(f"OpenCode server at {client.url} not healthy after {timeout}s: {last}")


def start_stream_thread(client, on_event, on_state=None):
    stop = threading.Event()
    thread = threading.Thread(target=client.stream, args=(on_event, stop, on_state), daemon=True, name="opencode-events")
    thread.start()
    return stop, thread
