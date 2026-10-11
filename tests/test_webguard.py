"""What every web server checks before it answers (herdr_py/webguard.py), first as rules and then against the four
servers: a request for a name that is not this machine's (DNS rebinding) is refused, a token with characters outside
ASCII is a wrong token rather than a dropped connection, and a Content-Length that is not a whole number is refused
instead of leaving the thread reading until the client gives up."""
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from herdr_py import dashboard, engineview, notebookview, webguard  # noqa: E402
from herdr_py.opencode import OpenCode  # noqa: E402
from herdr_py.policy import Policy  # noqa: E402
from herdr_py.server import Daemon  # noqa: E402
from fake_opencode import FakeOpenCode  # noqa: E402
from test_core import wait_for  # noqa: E402


class Headers(dict):
    def get(self, key, default=None):
        return super().get(key, default)


class RuleTest(unittest.TestCase):
    def test_host_names(self):
        cases = {"localhost": "localhost", "LocalHost:8790": "localhost", "127.0.0.1:1": "127.0.0.1", "[::1]:8790": "::1",
                 "[::1]": "::1", "example.com:": "example.com", "a.example": "a.example", None: None, "": None,
                 ":8790": None, "::1": None, "[::1": None, "[::1]x": None, "host:port": None, "[]:1": None}
        for header, name in cases.items():
            self.assertEqual(webguard.host_name(header), name, header)

    def test_a_loopback_server_answers_this_machine_names_only(self):
        for host in ("localhost", "LOCALHOST:80", "127.0.0.1:8790", "127.9.9.9", "[::1]:8790", "app.localhost:1",
                     "[::ffff:127.0.0.1]:1"):
            self.assertTrue(webguard.host_allowed(host, "127.0.0.1"), host)
        for host in ("attacker.example:8790", "localhost.attacker.example", "127.0.0.1.attacker.example", "0.0.0.0",
                     "10.0.0.5", "[::2]", "localhost.", None, "", "::1", "[::ffff:10.0.0.1]", "evil-localhost",
                     "attackerlocalhost:80"):
            self.assertFalse(webguard.host_allowed(host, "127.0.0.1"), host)
        self.assertTrue(webguard.host_allowed("box.tailnet.example:8443", "127.0.0.1", ["Box.Tailnet.Example"]))
        self.assertFalse(webguard.host_allowed("other.example", "127.0.0.1", ["box.tailnet.example", ""]))

    def test_what_counts_as_a_loopback_bind(self):
        for bound in ("127.0.0.1", "localhost", "::1", "[::1]", "127.1.2.3"):
            self.assertTrue(webguard.is_loopback(bound), bound)
        for bound in ("0.0.0.0", "", None, "::", "192.168.1.5", "box.example"):
            self.assertFalse(webguard.is_loopback(bound), bound)
            self.assertTrue(webguard.host_allowed("attacker.example", bound))  # exposed on purpose: any Host

    def test_tokens(self):
        self.assertTrue(webguard.token_ok("abc", "abc"))
        for given in ("abd", "ab", "abcd", "", None, "\xff\xfe", "abç"):
            self.assertFalse(webguard.token_ok(given, "abc"), given)
        self.assertFalse(webguard.token_ok("", ""))  # a server without a token takes no commands
        self.assertEqual(webguard.bearer(Headers({"Authorization": "Bearer xyz"})), "xyz")
        self.assertEqual(webguard.bearer(Headers({"Authorization": "Basic xyz"})), "")
        self.assertEqual(webguard.bearer(Headers({})), "")

    def test_body_lengths(self):
        self.assertEqual(webguard.body_length(Headers({}), 10), (0, None))
        self.assertEqual(webguard.body_length(Headers({"Content-Length": " 10 "}), 10), (10, None))
        self.assertEqual(webguard.body_length(Headers({"Content-Length": "0"}), 10), (0, None))
        self.assertEqual(webguard.body_length(Headers({"Content-Length": "11"}), 10)[1][0], 413)
        for raw in ("-1", "+5", "1e3", "5.0", "", "١", "²", "0x10", "1 2"):
            self.assertEqual(webguard.body_length(Headers({"Content-Length": raw}), 10)[1][0], 400, raw)


def status(port, method, path, host="127.0.0.1", headers=(), body=b"", wait=5):
    """The status code of one raw request (None when the server closed without answering or did not answer in time):
    raw, because the requests this test sends are ones that urllib refuses to make."""
    lines = [f"{method} {path} HTTP/1.1"] + ([f"Host: {host}"] if host is not None else []) + list(headers)
    data = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body
    with socket.create_connection(("127.0.0.1", port), timeout=wait) as s:
        s.sendall(data)
        try:
            first = s.recv(64)
        except socket.timeout:
            return None
    parts = first.split(b" ")
    return int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None


class CommandLineTest(unittest.TestCase):
    """--allow-host reaches each server from its command line."""

    def test_allow_host_is_passed_on(self):
        out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, out, True)
        with open(os.path.join(out, "engine.jsonl"), "w") as handle:
            handle.write("{}\n")
        with mock.patch.object(engineview, "serve") as serve:
            engineview.main([out, "--serve", "--allow-host", "a.example", "--allow-host", "b.example"])
        self.assertEqual(serve.call_args[1]["allow_hosts"], ["a.example", "b.example"])
        with mock.patch.object(notebookview, "serve") as serve:
            from herdr_py import notebook
            notebook.main([out, "serve", "--allow-host", "n.example"])
        self.assertEqual(serve.call_args[1]["allow_hosts"], ["n.example"])
        os.makedirs(os.path.join(out, "work"))
        with mock.patch.object(dashboard.Dashboard, "start"), mock.patch.object(dashboard.ThreadingHTTPServer, "serve_forever",
                                                                                side_effect=KeyboardInterrupt):
            with mock.patch.object(dashboard, "Dashboard", wraps=dashboard.Dashboard) as made:
                dashboard.main([out, "--port", "0", "--allow-host", "d.example"])
        self.assertEqual(made.call_args[1]["allow_hosts"], ["d.example"])
        from herdr_py import cli
        with mock.patch("herdr_py.server.Daemon") as daemon, mock.patch("herdr_py.opencode.wait_healthy", return_value={}), \
                mock.patch("herdr_py.opencode.unasked", return_value=[]), mock.patch.object(cli, "Client") as client, \
                mock.patch("signal.signal"):
            client.return_value.call.side_effect = cli.ClientError("unreachable", "none running")
            daemon.return_value.start.return_value = 0
            code = cli.main(["serve", "--opencode", "http://127.0.0.1:1", "--state-dir", out, "--allow-host", "s.example"])
        self.assertEqual(code, 0)
        self.assertEqual(daemon.call_args[1]["allow_hosts"], ["s.example"])


class Attacks:
    """Shared by the four servers below: self.port, self.page (a GET that needs no token), self.post (a POST path),
    self.token, self.answers (the status of an authorized POST of {}), and self.allowed (a name given to --allow-host)."""

    def test_a_request_for_another_name_is_refused(self):
        for host in ("attacker.example:%d" % self.port, "127.0.0.1.attacker.example", None):
            self.assertEqual(status(self.port, "GET", self.page, host=host), 403, host)
        for host in ("127.0.0.1:%d" % self.port, "localhost:%d" % self.port, "[::1]:%d" % self.port, self.allowed):
            self.assertNotEqual(status(self.port, "GET", self.page, host=host), 403, host)
        auth = ["Authorization: Bearer " + self.token, "Content-Type: application/json", "Content-Length: 2"]
        self.assertEqual(status(self.port, "POST", self.post, host="attacker.example", headers=auth, body=b"{}"), 403)

    def test_a_token_outside_ascii_is_a_wrong_token(self):
        bad = ["Authorization: Bearer \xff" + self.token, "Content-Type: application/json", "Content-Length: 2"]
        self.assertIn(status(self.port, "POST", self.post, headers=bad, body=b"{}"), (401, 403))

    def test_a_body_length_that_is_not_a_whole_number_is_refused(self):
        for raw in ("-1", "abc", "1e3"):
            head = ["Authorization: Bearer " + self.token, "Content-Type: application/json", "Content-Length: " + raw]
            self.assertEqual(status(self.port, "POST", self.post, headers=head, body=b"{}", wait=3), 400, raw)
        head = ["Authorization: Bearer " + self.token, "Content-Type: application/json", "Content-Length: 2"]
        self.assertEqual(status(self.port, "POST", self.post, headers=head, body=b"{}"), self.answers)


class EngineViewAttackTest(Attacks, unittest.TestCase):
    page, post, answers, allowed = "/", "/control", 400, "run.example"

    def setUp(self):
        self.out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.out, True)
        with open(os.path.join(self.out, "engine.jsonl"), "w") as handle:
            handle.write(json.dumps({"t": 100.0, "kind": "start", "members": ["a"], "planner": "plan", "turns": 3}) + "\n")
        got, ready = {}, threading.Event()

        def up(server, token):
            got.update(server=server, token=token)
            ready.set()
        threading.Thread(target=engineview.serve, args=(self.out,), daemon=True,
                         kwargs={"port": 0, "ready": up, "allow_hosts": [self.allowed]}).start()
        self.assertTrue(ready.wait(5))
        self.addCleanup(got["server"].shutdown)
        self.port, self.token = got["server"].server_address[1], got["token"]


class NotebookAttackTest(Attacks, unittest.TestCase):
    page, post, answers, allowed = "/", "/api/act", 409, "notes.example:8443"

    def setUp(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        got, ready = {}, threading.Event()

        def up(url):
            got["url"] = url
            ready.set()
        self.token = "tok"
        threading.Thread(target=notebookview.serve, args=(folder,), daemon=True,
                         kwargs={"port": 0, "token": self.token, "ready": up, "allow_hosts": ["notes.example"]}).start()
        self.assertTrue(ready.wait(10))
        self.port = int(got["url"].split(":")[2].split("/")[0])


class DaemonAttackTest(Attacks, unittest.TestCase):
    page, post, answers, allowed = "/", "/api/agents", 400, "daemon.example"

    def setUp(self):
        self.fake = FakeOpenCode().start()
        self.addCleanup(self.fake.stop)
        self.dir = tempfile.mkdtemp(dir="/tmp")  # AF_UNIX paths are limited to ~104 bytes on macOS
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.daemon = Daemon(OpenCode(self.fake.url), Policy([], default="ask"), self.dir, http_addr=":0",
                             allow_hosts=[self.allowed])
        self.daemon.start()
        self.addCleanup(self.daemon.stop)
        wait_for(lambda: self.fake.stream_count() == 1, what="event stream")
        self.port, self.token = self.daemon.web.server_address[1], self.daemon.token

    def test_the_socket_is_never_open_to_others(self):
        self.assertEqual(os.stat(self.daemon.socket_path).st_mode & 0o777, 0o600)
        before = os.umask(0o022)
        os.umask(before)
        with mock.patch("os.umask", wraps=os.umask) as umask:  # born 0600: the mask is set before the socket exists
            other = Daemon(OpenCode(self.fake.url), Policy([], default="ask"), os.path.join(self.dir, "b"))
            other.start()
            other.stop()
        self.assertIn(mock.call(0o177), umask.call_args_list)
        after = os.umask(before)
        self.assertEqual(after, before)  # and the process's own mask is back


class DashboardAttackTest(Attacks, unittest.TestCase):
    page, post, answers, allowed = "/", "/api/state", 405, "dash.example"

    def setUp(self):
        run = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, run, True)
        os.makedirs(os.path.join(run, "work"))
        self.token = "t" * 20
        self.dash = dashboard.Dashboard(run, port=0, token=self.token, allow_hosts=[self.allowed]).start()
        self.addCleanup(self.dash.stop)
        self.port = self.dash.web.server_address[1]

    def test_a_token_outside_ascii_is_a_wrong_token(self):  # read-only: no POST; the token guards GET /api
        self.assertEqual(status(self.port, "GET", "/api/state", headers=["Authorization: Bearer \xff" + self.token]), 401)
        self.assertEqual(status(self.port, "GET", "/api/state", headers=["Authorization: Bearer " + self.token]), 200)

    def test_a_body_length_that_is_not_a_whole_number_is_refused(self):  # nothing reads a body: every write is 405
        self.assertEqual(status(self.port, "POST", "/api/state", headers=["Content-Length: -1"]), 405)


if __name__ == "__main__":
    unittest.main()
