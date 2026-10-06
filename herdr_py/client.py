"""Talk to a running herdr-py daemon over its Unix socket (used by the CLI and the TUI; any program can do the same)."""
import itertools
import json
import os
import socket


class ClientError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code


def default_state_dir():
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.environ.get("HERDR_PY_STATE_DIR") or os.path.join(base, "herdr-py")


def default_socket():
    return os.environ.get("HERDR_PY_SOCKET") or os.path.join(default_state_dir(), "herdr-py.sock")


class Client:
    _ids = itertools.count(1)

    def __init__(self, path=None, timeout=None):
        self.path = path or default_socket()
        self.timeout = timeout

    def _connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self.path)
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            sock.close()
            raise ClientError("server_not_running", f"no herdr-py daemon at {self.path} ({exc.strerror}); start one with `herdr-py serve`") from None
        return sock

    def call(self, method, **params):
        sock = self._connect()
        try:
            rid = f"c{next(self._ids)}"
            sock.sendall((json.dumps({"id": rid, "method": method, "params": params}) + "\n").encode())
            reader = sock.makefile("rb")
            line = reader.readline()
            if not line:
                raise ClientError("closed", "the daemon closed the connection")
            reply = json.loads(line)
            if "error" in reply:
                raise ClientError(reply["error"].get("code", "error"), reply["error"].get("message", ""))
            return reply["result"]
        finally:
            sock.close()

    def events(self):
        """Yield events forever (first item: {"subscribed": True, "agents": [...]})."""
        sock = self._connect()
        sock.settimeout(None)
        try:
            sock.sendall((json.dumps({"id": "sub", "method": "events.subscribe", "params": {}}) + "\n").encode())
            for line in sock.makefile("rb"):
                msg = json.loads(line)
                if "error" in msg:
                    raise ClientError(msg["error"].get("code", "error"), msg["error"].get("message", ""))
                yield msg.get("result") or msg.get("event")
        finally:
            sock.close()
