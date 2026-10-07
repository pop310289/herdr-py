"""The live dashboard: the state model built from a fixture run, its HTTP endpoints (token, Server-Sent Events, files
inside the run only, no writes), the command, and the page's script (run data never parsed as markup)."""
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from herdr_py import dashboard as D
from herdr_py.client import ClientError
from herdr_py.opencode import OpenCode
from herdr_py.policy import Policy
from herdr_py.server import Daemon

from fake_opencode import FakeOpenCode
from test_core import wait_for

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURE = os.path.join(HERE, "fixtures", "dashboard_run")
WEB = os.path.join(ROOT, "herdr_py", "web", "dashboard")
sys.path.insert(0, os.path.join(ROOT, "examples", "slide_team"))
import imgcmp  # noqa: E402

TOKEN = "test-token-123"


def no_nan(text):
    """json.loads that refuses NaN/Infinity, as a browser's JSON.parse does."""
    def refuse(name):
        raise ValueError(f"{name} is not JSON")
    return json.loads(text, parse_constant=refuse)


def copy_fixture(test):
    tmp = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, tmp, True)
    run = os.path.join(tmp, "run")
    shutil.copytree(FIXTURE, run)
    return tmp, run


def append(path, *items):
    with open(path, "a", encoding="utf-8") as handle:
        for item in items:
            handle.write(json.dumps(item) + "\n")


def points(rows):
    return {r["row"]: [(p["turn"], p["drawer"], p["kind"], p["valid"], p["accepted"]) for p in r["points"]] for r in rows}


class ModelTest(unittest.TestCase):
    def test_progress_names_the_art_director_while_it_reviews(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder)
        os.makedirs(os.path.join(folder, "work"))
        lines = [(1000.0, "manager", "drawA", "message", "round 3: row 2 draft from the checklist"),
                 (1010.0, "drawA", "supervisor", "message", "22 components: r2.title"),
                 (1011.0, "picture", "team", "check", "row 2: match 0.834, 0 labels missing: first version kept"),
                 (1012.0, "render", "art", "check", "row 2: original and ours ready")]
        with open(os.path.join(folder, "work", "chat.jsonl"), "w") as handle:
            for t, who, to, kind, text in lines:
                handle.write(json.dumps({"t": t, "from": who, "to": to, "kind": kind, "text": text}) + "\n")
        run = D.RunFolder(folder)
        run.refresh()
        self.assertEqual(run.state()["run"]["progress"], {"round": 3, "row": 2, "kind": "review", "member": "art", "t": 1012.0})
        with open(os.path.join(folder, "work", "chat.jsonl"), "a") as handle:  # the notes arrive: back to the drawer of the round
            handle.write(json.dumps({"t": 1050.0, "from": "art", "to": "drawB", "kind": "message", "text": "DIFF: x"}) + "\n")
        run.refresh()
        self.assertEqual(run.state()["run"]["progress"]["member"], "drawA")

    def test_a_strict_run_shows_its_strict_score_and_what_decided(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder)
        shutil.copytree(os.path.join(FIXTURE, "work"), os.path.join(folder, "work"))
        path = os.path.join(folder, "work", "summary.json")
        with open(path) as handle:
            summary = json.load(handle)
        summary["final"].update(strict=0.8728, score_mode="strict")
        with open(path, "w") as handle:
            json.dump(summary, handle)
        run = D.RunFolder(folder)
        run.refresh()
        self.assertEqual({k: run.state()["final"][k] for k in ("match", "strict", "score_mode")},
                         {"match": 0.6551, "strict": 0.8728, "score_mode": "strict"})
        with open(os.path.join(D.WEB_DIR, "dashboard.js"), encoding="utf-8") as handle:
            self.assertIn('"Strict score"', handle.read())  # the page has a tile for it

    def test_the_fixture_pictures_are_16_by_16_pngs(self):
        for name in ("01-row1-draft.png", "row1-original.png"):
            self.assertEqual(imgcmp.read_png(os.path.join(FIXTURE, "work", "renders", name))[:2], (16, 16))

    def test_state_of_the_fixture_run(self):
        run = D.RunFolder(FIXTURE)
        run.refresh()
        s = run.state()
        no_nan(json.dumps(s, allow_nan=False))
        self.assertEqual((s["run"]["work"], s["run"]["ended"], s["run"]["summary"], s["run"]["bad_lines"]), ("work", True, True, 0))
        self.assertEqual(s["run"]["progress"], {"round": 5, "row": 2, "kind": "revise", "member": "drawA", "t": 1070.0})
        self.assertEqual((s["chat_total"], s["chat"][0]["from"], s["chat"][-1]["kind"]), (33, "supervisor", "end"))
        members = {m["name"]: m for m in s["members"]}
        self.assertEqual(s["run"]["members_from"], "codex")
        self.assertEqual([(m["name"], m["state"], m["tokens"], m["turns"], m["sessions"]) for m in s["members"]],
                         [("drawA", "idle", 12000, 3, 2), ("drawB", "error", 8000, 2, 1), ("art", "aborted", 3000, 3, None)])
        self.assertEqual((members["drawA"]["words"], members["drawA"]["words_from"]), ("(no reply)", "stream"))
        self.assertEqual(members["drawB"]["words_from"], "chat")  # nothing streamed: its last line in the conversation
        self.assertTrue(members["drawB"]["words"].startswith("10 components: r2.longPrompt"))
        self.assertEqual(points(s["rows"]), {1: [(1, "drawA", "draft", True, True), (2, "drawB", "revise", True, False),
                                                 (3, "drawA", "revise", True, True)],
                                             2: [(4, "drawB", "draft", True, True), (5, "drawA", "revise", False, False)]})
        row1, row2 = s["rows"]
        self.assertEqual(([p["match"] for p in row1["points"]], row1["kept"], row1["best"]), ([0.6121, 0.5984, 0.7012], [0.6121, 0.6121, 0.7012], 0.7012))
        self.assertEqual((row2["points"][1]["match"], row2["kept"], row2["points"][0]["missing"]), (None, [0.5403, 0.5403], ["WAIT", "Chunk 3"]))
        self.assertEqual(s["final"], {"match": 0.6551, "psnr": 14.21, "missing": ["WAIT", "Chunk 3", "KV copy"]})
        self.assertEqual(s["lessons"], [{"text": "a revision that lowered the picture match was rejected: change only what the notes ask", "count": 1},
                                        {"text": "the reply had no ```json block with a JSON list", "count": 2}])

    def test_pictures_are_found_inside_the_run_even_when_it_was_moved(self):
        run = D.RunFolder(FIXTURE)
        run.refresh()
        first, second, third = run.state()["pictures"]
        # written as /elsewhere/runs/demo/work/renders/01-row1-draft.png: the folder moved, so renders/<name> is used
        self.assertEqual(first["ours"], {"path": "work/renders/01-row1-draft.png", "size": [16, 16]})
        self.assertEqual(first["original"], {"path": "work/renders/row1-original.png", "size": [16, 16]})
        self.assertEqual((first["round"], first["row"], first["drawer"], first["kind"], first["match"]), (1, 1, "drawA", "draft", 0.6121))
        self.assertEqual((second["ours"], second["name"], second["original"]["path"]), (None, "03-row1-rev.png", "work/renders/row1-original.png"))
        self.assertEqual((third["row"], third["ours"], third["original"]), (2, None, None))  # no crop of row 2 in the folder

    def test_live_rows_from_the_conversation_agree_with_the_summary(self):
        _, run_dir = copy_fixture(self)
        final = D.RunFolder(run_dir)
        final.refresh()
        from_summary = final.state()
        os.remove(os.path.join(run_dir, "work", "summary.json"))  # as during the run: summary.json comes at the end
        live = D.RunFolder(run_dir)
        live.refresh()
        from_chat = live.state()
        self.assertFalse(from_chat["run"]["summary"])
        self.assertEqual(points(from_chat["rows"]), points(from_summary["rows"]))
        self.assertEqual([p["pending"] for r in from_chat["rows"] for p in r["points"]], [False] * 5)  # the run has ended
        for a, b in zip(from_chat["rows"], from_summary["rows"]):
            for p, q in zip(a["points"], b["points"]):
                self.assertEqual(p["missing_count"], q["missing_count"], p)
                if q["match"] is None:
                    self.assertIsNone(p["match"])
                else:
                    self.assertAlmostEqual(p["match"], q["match"], delta=0.0005)  # the chat rounds to three decimals
            self.assertEqual([k is None for k in a["kept"]], [k is None for k in b["kept"]])
            for k, m in zip(a["kept"], b["kept"]):
                if m is not None:
                    self.assertAlmostEqual(k, m, delta=0.0005)
        self.assertEqual([(l["text"], l["count"]) for l in from_chat["lessons"]],
                         [("a revision that lowered the picture match was rejected", None),
                          ("the reply had no ```json block with a JSON list", None)])
        self.assertIsNone(from_chat["final"])

    def test_a_turn_without_a_picture_is_pending_until_the_next_turn_or_the_end(self):
        def line(frm, to, text, kind="message"):
            return {"t": 1.0, "from": frm, "to": to, "kind": kind, "text": text}
        chat = [line("manager", "drawA", "round 1: row 1 draft from the checklist"),
                line("check", "drawA", "2 errors: r1.a: missing \"y\"", "check")]
        (row,) = D.rows_from_chat(chat, ended=False)
        self.assertEqual([(p["pending"], p["valid"]) for p in row["points"]], [(True, False)])
        (row,) = D.rows_from_chat(chat, ended=True)  # the run ended without a picture: that turn could not be drawn
        self.assertEqual([(p["pending"], p["valid"]) for p in row["points"]], [(False, False)])
        chat.append(line("manager", "drawB", "round 2: row 1 revise drawA's version: 0 art notes, 1 program notes"))
        (row,) = D.rows_from_chat(chat, ended=False)
        self.assertEqual([(p["pending"], p["valid"]) for p in row["points"]], [(False, False), (True, False)])
        chat.append(line("picture", "team", "row 1: match 0.400, 0 labels missing: first version kept", "check"))
        (row,) = D.rows_from_chat(chat, ended=True)
        self.assertEqual([(p["pending"], p["valid"], p["accepted"], p["match"]) for p in row["points"]],
                         [(False, False, False, None), (False, True, True, 0.4)])
        self.assertEqual(row["kept"], [None, 0.4])

    def test_a_rerun_in_the_same_folder_leaves_the_old_summary_and_pictures_out(self):
        _, run_dir = copy_fixture(self)
        append(os.path.join(run_dir, "work", "chat.jsonl"),
               {"t": 2000.0, "from": "supervisor", "to": "team", "kind": "control", "text": "task: rebuild the infographic again"},
               {"t": 2001.0, "from": "manager", "to": "drawA", "kind": "message", "text": "round 1: row 1 draft from the checklist"})
        run = D.RunFolder(run_dir)
        run.refresh()
        s = run.state()
        self.assertEqual((s["run"]["ended"], s["run"]["summary"], s["run"]["earlier_lines"], s["final"]), (False, False, 33, None))
        self.assertEqual(points(s["rows"]), {1: [(1, "drawA", "draft", False, False)]})
        self.assertTrue(s["rows"][0]["points"][0]["pending"])
        self.assertEqual(s["pictures"], [])  # every picture in the manifest is older than the new run
        self.assertEqual(s["chat_total"], 35)  # the conversation itself keeps both runs

    def test_lines_wait_for_the_newline_skip_bad_lines_and_start_over_when_replaced(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        path = os.path.join(tmp, "x.jsonl")
        with open(path, "w") as handle:
            handle.write('{"a": 1}\n{"b": ')
        lines = D.Lines(tmp, ["x.jsonl"])
        self.assertTrue(lines.read())
        self.assertEqual(lines.items, [{"a": 1}])
        self.assertFalse(lines.read())
        with open(path, "a") as handle:
            handle.write('2}\nnot json\n[1]\n')
        self.assertTrue(lines.read())
        self.assertEqual((lines.items, lines.bad, lines.resets), ([{"a": 1}, {"b": 2}], 2, 0))
        with open(path + ".new", "w") as handle:
            handle.write('{"c": 3}\n')
        os.replace(path + ".new", path)
        self.assertTrue(lines.read())
        self.assertEqual((lines.items, lines.bad, lines.resets), ([{"c": 3}], 0, 1))
        os.remove(path)
        self.assertTrue(lines.read())
        self.assertEqual((lines.items, lines.resets), ([], 2))

    def test_a_half_written_json_file_keeps_the_last_good_value(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        path = os.path.join(tmp, "summary.json")
        with open(path, "w") as handle:
            handle.write('{"final": {"match": 0.5}}')
        summary = D.JsonFile(tmp, ["summary.json"])
        self.assertTrue(summary.read())
        with open(path, "w") as handle:
            handle.write('{"final": {"ma')  # json.dump is still writing it
        self.assertFalse(summary.read())
        self.assertEqual(summary.value, {"final": {"match": 0.5}})
        with open(path, "w") as handle:
            handle.write('{"final": {"match": 0.75}}')
        self.assertTrue(summary.read())
        self.assertEqual(summary.value, {"final": {"match": 0.75}})

    def test_an_infinite_psnr_reaches_the_page_as_text(self):
        _, run_dir = copy_fixture(self)
        path = os.path.join(run_dir, "work", "summary.json")
        with open(path) as handle:
            summary = json.load(handle)
        summary["final"]["psnr"] = float("inf")  # identical pictures; json.dump writes Infinity, which JSON.parse rejects
        with open(path, "w") as handle:
            json.dump(summary, handle)
        run = D.RunFolder(run_dir)
        run.refresh()
        state = run.state()
        self.assertEqual(state["final"]["psnr"], "inf")
        no_nan(json.dumps(state, allow_nan=False))


class FakeClient:
    def __init__(self, agents):
        self.agents, self.error = agents, None

    def call(self, method, **params):
        if self.error:
            raise self.error
        return {"agents": self.agents}


class DaemonMembersTest(unittest.TestCase):
    def test_sessions_pending_requests_and_counters_from_agent_views(self):
        views = [{"name": "a", "state": "blocked", "tokens": 5, "turns": 2, "since": 100.5, "past_sessions": ["s1", "s2"],
                  "pending": [{"id": "per_1", "description": "bash: curl x"}], "stream": {"kind": "reasoning", "text": "hmm"},
                  "compactions": 2, "truncated_turns": 0, "counters": {"reasoning_only_turns": 1, "retries": 4, "flag": True}},
                 {"name": "b", "state": "working", "tokens": 1, "turns": 9, "past_sessions": ["s"] * 5},
                 {"name": "c", "state": "idle", "tokens": 0, "turns": 1, "sessions": 9, "past_sessions": []}]
        run = D.RunFolder(FIXTURE, socket_path=os.path.join(FIXTURE, "no.sock"))
        run.client = FakeClient(views)
        run.refresh()
        s = run.state()
        a, b, c = s["members"]
        self.assertEqual(s["run"]["members_from"], "daemon")
        self.assertEqual((a["state"], a["sessions"], a["sessions_more"], a["since"], a["pending"], a["words"], a["words_kind"]),
                         ("blocked", 3, False, 100.5, ["bash: curl x"], "hmm", "reasoning"))
        self.assertEqual([(x["label"], x["value"]) for x in a["counters"]],
                         [("compactions", 2), ("truncated turns", 0), ("reasoning-only turns", 1), ("retries", 4)])
        self.assertEqual((b["sessions"], b["sessions_more"], b["counters"]), (6, True, []))  # the view lists 5 earlier sessions at most
        self.assertEqual((c["sessions"], c["sessions_more"]), (9, False))

    def test_a_daemon_that_stops_answering_keeps_the_last_members(self):
        run = D.RunFolder(FIXTURE, socket_path=os.path.join(FIXTURE, "no.sock"), member_poll_s=0)
        run.client = FakeClient([{"name": "a", "state": "working"}])
        run.refresh()
        run.client.error = ClientError("server_not_running", "no herdr-py daemon")
        run.refresh()
        s = run.state()
        self.assertEqual([m["name"] for m in s["members"]], ["a"])
        self.assertIn("server_not_running", s["run"]["members_error"])

    def test_members_from_a_running_daemon(self):
        fake = FakeOpenCode().start()
        self.addCleanup(fake.stop)
        tmp = tempfile.mkdtemp(dir="/tmp")  # AF_UNIX paths are limited to ~104 bytes on macOS
        self.addCleanup(shutil.rmtree, tmp, True)
        daemon = Daemon(OpenCode(fake.url), Policy([], default="ask"), tmp)
        daemon.start()
        self.addCleanup(daemon.stop)
        wait_for(lambda: fake.stream_count() == 1, what="event stream")
        daemon.call("agent.start", {"name": "w", "prompt": "go"})
        run = D.RunFolder(FIXTURE, socket_path=daemon.socket_path)
        run.refresh()
        (w,) = run.state()["members"]
        self.assertEqual((w["name"], w["state"], w["turns"], w["sessions"]), ("w", "starting", 1, 1))


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp, self.run_dir = copy_fixture(self)
        renders = os.path.join(self.run_dir, "work", "renders")
        with open(os.path.join(self.tmp, "secret.png"), "wb") as handle:  # outside the run folder
            handle.write(b"\x89PNG\r\n\x1a\nSECRET-OUTSIDE")
        os.makedirs(os.path.join(self.run_dir, "state"))
        with open(os.path.join(self.run_dir, "state", "token"), "w") as handle:  # run_demo.py keeps the daemon state here
            handle.write("SECRET-TOKEN")
        with open(os.path.join(renders, ".hidden.png"), "wb") as handle:
            handle.write(b"SECRET-HIDDEN")
        os.symlink(os.path.join(self.tmp, "secret.png"), os.path.join(renders, "link.png"))
        os.symlink("01-row1-draft.png", os.path.join(renders, "inside.png"))
        os.symlink(self.tmp, os.path.join(self.run_dir, "work", "up"))
        os.mkfifo(os.path.join(renders, "pipe.png"))
        self.dash = D.Dashboard(self.run_dir, port=0, token=TOKEN, poll_s=0.05, keepalive_s=0.3).start()
        self.addCleanup(self.dash.stop)
        self.port = self.dash.web.server_address[1]

    def get(self, path, token=True, method="GET", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            sep = "&" if "?" in path else "?"
            conn.request(method, path + (f"{sep}token={TOKEN}" if token else ""), headers=headers or {})
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()

    def test_the_page_needs_no_token_and_is_sent_with_a_strict_policy(self):
        status, headers, body = self.get("/", token=False)
        self.assertEqual(status, 200)
        self.assertIn(b"dashboard.js", body)
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        for path in ("/dashboard.js", "/dashboard.css", "/icon.svg"):
            self.assertEqual(self.get(path, token=False)[0], 200, path)

    def test_the_api_needs_the_token(self):
        self.assertEqual(self.get("/api/state", token=False)[0], 401)
        self.assertEqual(self.get("/api/state?token=wrong", token=False)[0], 401)
        self.assertEqual(self.get("/files/work/renders/01-row1-draft.png", token=False)[0], 401)
        self.assertEqual(self.get("/api/state", token=False, headers={"Authorization": "Bearer " + TOKEN})[0], 200)

    def test_api_state_is_the_runs_state_as_json(self):
        status, headers, body = self.get("/api/state")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        state = no_nan(body.decode())
        self.assertEqual((state["chat_total"], len(state["chat"]), state["run"]["name"]), (33, 33, "run"))
        self.assertEqual([r["row"] for r in state["rows"]], [1, 2])
        self.assertEqual(state["pictures"][0]["ours"]["path"], "work/renders/01-row1-draft.png")

    def read_event(self, resp, deadline=5.0):
        """The next `data:` event of a Server-Sent Events stream (comments and retry lines are skipped)."""
        data, end = [], time.time() + deadline
        while time.time() < end:
            line = resp.readline().decode()
            if not line:
                raise AssertionError("the event stream ended")
            line = line.rstrip("\n")
            if line.startswith("data: "):
                data.append(line[6:])
            elif line == "" and data:
                return no_nan("\n".join(data))
        raise AssertionError("no event in time")

    def test_events_start_with_a_snapshot_and_then_send_new_lines(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(conn.close)
        conn.request("GET", "/api/events?token=" + TOKEN)
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertTrue(resp.getheader("Content-Type").startswith("text/event-stream"))
        first = self.read_event(resp)
        self.assertEqual((first["type"], first["chat_from"], first["chat_total"], len(first["chat"])), ("snapshot", 0, 33, 33))
        append(os.path.join(self.run_dir, "work", "chat.jsonl"),
               {"t": 1100.0, "from": "operator", "to": "team", "kind": "control", "text": "a new line"})
        update = self.read_event(resp)
        self.assertEqual((update["type"], update["chat_from"], update["chat_total"]), ("update", 33, 34))
        self.assertEqual([c["text"] for c in update["chat"]], ["a new line"])
        with open(os.path.join(self.run_dir, "work", "chat.jsonl"), "w") as handle:  # the conversation starts over
            handle.write(json.dumps({"t": 1.0, "from": "supervisor", "to": "team", "kind": "control", "text": "task: again"}) + "\n")
        again = self.read_event(resp)
        self.assertEqual((again["type"], again["chat_from"], again["chat_total"]), ("snapshot", 0, 1))

    def test_files_serves_images_inside_the_run(self):
        status, headers, body = self.get("/files/work/renders/01-row1-draft.png")
        self.assertEqual((status, headers["Content-Type"]), (200, "image/png"))
        with open(os.path.join(self.run_dir, "work", "renders", "01-row1-draft.png"), "rb") as handle:
            self.assertEqual(body, handle.read())
        status, _, body = self.get("/files/work/renders/01-row1-draft.png", headers={"If-None-Match": headers["ETag"]})
        self.assertEqual((status, body), (304, b""))

    def test_files_refuses_traversal_symlinks_hidden_files_fifos_and_non_images(self):
        refused = ["/files/../secret.png", "/files/work/../../secret.png", "/files/%2e%2e/secret.png",
                   "/files/work/renders/..%2f..%2f..%2fsecret.png", "/files//etc/passwd", "/files/" + self.tmp + "/secret.png",
                   "/files/work/up/secret.png",              # a symlinked folder that leads out
                   "/files/work/renders/link.png",           # a symlink out of the run folder
                   "/files/work/renders/inside.png",         # any symlink, even one that stays inside
                   "/files/work/renders/.hidden.png", "/files/state/token", "/files/work/chat.jsonl",
                   "/files/work/renders/pipe.png",           # a FIFO must not hang the server
                   "/files/work/renders/missing.png", "/files/work/renders", "/files/"]
        for path in refused:
            status, _, body = self.get(path)
            self.assertEqual(status, 404, path)
            self.assertNotIn(b"SECRET", body, path)

    def test_there_are_no_write_endpoints(self):
        before = sorted(os.path.join(d, f) for d, _, files in os.walk(self.run_dir) for f in files)
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            for path in ("/api/state", "/files/work/renders/01-row1-draft.png", "/"):
                self.assertEqual(self.get(path, method=method)[0], 405, (method, path))
        after = sorted(os.path.join(d, f) for d, _, files in os.walk(self.run_dir) for f in files)
        self.assertEqual(before, after)

    def test_it_listens_on_loopback_by_default(self):
        a = D.parse_args(["RUN"])
        self.assertEqual((a.host, a.port, a.socket), ("127.0.0.1", 8770, None))
        dash = D.Dashboard(self.run_dir, port=0)
        self.addCleanup(dash.web.server_close)
        self.assertEqual(dash.web.server_address[0], "127.0.0.1")


class CommandTest(unittest.TestCase):
    def test_the_command_prints_a_link_with_the_token_and_serves_it(self):
        env = dict(os.environ, PYTHONPATH=ROOT, PYTHONDONTWRITEBYTECODE="1")
        proc = subprocess.Popen([sys.executable, "-m", "herdr_py.dashboard", FIXTURE, "--port", "0"], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, universal_newlines=True, env=env)

        def stop():
            proc.terminate()
            proc.wait(10)
            proc.stdout.close()
        self.addCleanup(stop)
        lines = []
        reader = threading.Thread(target=lambda: lines.extend(iter(proc.stdout.readline, "")), daemon=True)
        reader.start()
        wait_for(lambda: any(l.startswith("open: ") for l in lines), timeout=20, what="the printed link")
        link = [l for l in lines if l.startswith("open: ")][0].split()[1]
        found = re.match(r"http://127\.0\.0\.1:(\d+)/#token=([\w-]+)$", link)
        self.assertTrue(found, link)
        conn = http.client.HTTPConnection("127.0.0.1", int(found.group(1)), timeout=5)
        conn.request("GET", "/api/state?token=" + found.group(2))
        resp = conn.getresponse()
        self.assertEqual((resp.status, no_nan(resp.read().decode())["chat_total"]), (200, 33))
        conn.close()

    def test_a_missing_folder_is_an_error(self):
        env = dict(os.environ, PYTHONPATH=ROOT, PYTHONDONTWRITEBYTECODE="1")
        p = subprocess.run([sys.executable, "-m", "herdr_py.dashboard", os.path.join(FIXTURE, "missing")], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True, env=env, timeout=30)
        self.assertEqual(p.returncode, 2)
        self.assertIn("is not a folder", p.stderr)


class PageTest(unittest.TestCase):
    def read(self, name):
        with open(os.path.join(WEB, name), encoding="utf-8") as handle:
            return handle.read()

    def test_the_script_never_turns_data_into_markup(self):
        """Run data is model output: it may only enter the page as text (textContent, createElement, setAttribute)."""
        script = re.sub(r"//[^\n]*", "", self.read("dashboard.js"))  # comments may name what is forbidden
        for pattern in (r"\binnerHTML\b", r"\bouterHTML\b", r"insertAdjacentHTML", r"document\.write", r"DOMParser",
                        r"createContextualFragment", r"\beval\s*\(", r"\bnew\s+Function\b", r"setAttribute\(\s*[\"']on",
                        r"\bsrcdoc\b", r"javascript:", r"setAttribute\(\s*[\"']style"):
            self.assertIsNone(re.search(pattern, script), pattern)
        self.assertIn("textContent", script)

    def test_the_page_loads_nothing_from_elsewhere(self):
        page, css, script = self.read("index.html"), self.read("dashboard.css"), self.read("dashboard.js")
        self.assertIsNone(re.search(r"https?://", page))
        self.assertIsNone(re.search(r"<script(?![^>]*\bsrc=)", page))        # no inline script
        self.assertIsNone(re.search(r"\son[a-z]+\s*=", page))                # no inline event handlers
        self.assertIsNone(re.search(r"style\s*=", page))                     # no inline styles (the policy forbids them)
        self.assertIsNone(re.search(r"url\(|@import", css))
        self.assertEqual(re.findall(r"https?://[^\"'\s]+", script), ["http://www.w3.org/2000/svg"])  # the SVG namespace only


if __name__ == "__main__":
    unittest.main()
