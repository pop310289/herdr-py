"""Daemon tests: the Unix socket API, the HTTP API (token), and the CLI, against the fake OpenCode server."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from herdr_py.client import Client, ClientError
from herdr_py.opencode import OpenCode
from herdr_py.policy import Policy
from herdr_py.server import Daemon

from fake_opencode import FakeOpenCode
from test_core import wait_for

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class DaemonTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeOpenCode().start()
        self.dir = tempfile.mkdtemp(dir="/tmp")  # AF_UNIX paths are limited to ~104 bytes on macOS
        policy = Policy([{"permission": "bash", "match": "ls", "action": "allow"}], default="ask")
        self.daemon = Daemon(OpenCode(self.fake.url), policy, self.dir, http_addr="127.0.0.1:0")
        self.daemon.start()
        self.port = self.daemon.web.server_address[1]
        self.client = Client(self.daemon.socket_path, timeout=10)
        wait_for(lambda: self.fake.stream_count() == 1, what="event stream")

    def tearDown(self):
        self.daemon.stop()
        self.fake.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def http(self, method, path, body=None, token=True):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {self.daemon.token}"
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method, headers=headers,
                                     data=None if body is None else json.dumps(body).encode())
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def test_socket_api_start_events_ask_and_approve(self):
        self.assertTrue(self.client.call("ping")["pong"])
        events = []
        stream = self.client.events()
        first = next(stream)
        self.assertTrue(first["subscribed"])
        threading.Thread(target=lambda: [events.append(e) for e in stream], daemon=True).start()
        view = self.client.call("agent.start", name="w", prompt="go", followups=["more"])
        self.assertEqual(view["state"], "starting")
        sid = view["session_id"]
        self.fake.emit("session.status", sessionID=sid, status={"type": "busy"})
        rid = self.fake.ask_permission(sid, "bash", "curl x")
        wait_for(lambda: self.client.call("permission.list")["pending"], what="pending request")
        self.assertEqual(self.client.call("agent.get", name="w")["state"], "blocked")
        self.client.call("permission.reply", id=rid, reply="once")
        wait_for(lambda: (rid, "once", None) in self.fake.replies, what="reply reached OpenCode")
        wait_for(lambda: any(e.get("type") == "permission.decided" for e in events), what="decision event")
        self.assertIn("agent.state", {e["type"] for e in events})
        with self.assertRaises(ClientError) as ctx:
            self.client.call("agent.get", name="nope")
        self.assertEqual(ctx.exception.code, "huberror")

    def test_http_needs_the_token_and_can_answer_requests(self):
        status, _ = self.http("GET", "/api/agents", token=False)
        self.assertEqual(status, 401)
        status, body = self.http("POST", "/api/agents", {"name": "h", "prompt": "hi"})
        self.assertEqual(status, 200, body)
        sid = json.loads(body)["session_id"]
        rid = self.fake.ask_permission(sid, "bash", "make")
        wait_for(lambda: json.loads(self.http("GET", "/api/agents")[1])["agents"][0]["pending"], what="pending via http")
        status, body = self.http("POST", f"/api/permissions/{rid}", {"reply": "reject", "message": "no"})
        self.assertEqual(status, 200, body)
        wait_for(lambda: (rid, "reject", "no") in self.fake.replies, what="web reply")
        status, body = self.http("GET", "/", token=False)
        self.assertEqual(status, 200)  # static page needs no token; it holds no data

    def test_cli_prints_cjk_names_under_the_c_locale(self):
        self.client.call("agent.start", name="工人", prompt="hi")
        env = dict(os.environ, PYTHONPATH=ROOT, PYTHONDONTWRITEBYTECODE="1", LANG="C", LC_ALL="C")
        env.pop("PYTHONIOENCODING", None)
        env.pop("PYTHONUTF8", None)
        p = subprocess.run([sys.executable, "-m", "herdr_py", "--socket", self.daemon.socket_path, "list"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        self.assertIn("工人".encode("utf-8"), p.stdout)

    def test_cli_lists_agents_and_stop_removes_the_socket(self):
        self.client.call("agent.start", name="c", prompt="hi")
        env = dict(os.environ, PYTHONPATH=ROOT, PYTHONDONTWRITEBYTECODE="1")
        p = subprocess.run([sys.executable, "-m", "herdr_py", "--socket", self.daemon.socket_path, "--json", "list"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, env=env, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual([a["name"] for a in json.loads(p.stdout)["agents"]], ["c"])
        p = subprocess.run([sys.executable, "-m", "herdr_py", "--socket", os.path.join(self.dir, "missing.sock"), "list"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, env=env, timeout=30)
        self.assertEqual(p.returncode, 1)
        self.assertIn("server_not_running", p.stderr)
        self.client.call("server.stop")
        wait_for(lambda: not os.path.exists(self.daemon.socket_path), what="socket removed")


if __name__ == "__main__":
    unittest.main()
