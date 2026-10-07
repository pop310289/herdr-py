"""codex_agents.CodexAgents against tests/fake_codex.py: sessions, resume, images, tokens, stdin, timeouts and errors."""
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "examples", "slide_team"))
from codex_agents import CodexAgents  # noqa: E402

FAKE = os.path.join(HERE, "fake_codex.py")


class CodexAgentsTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        self.log = os.path.join(self.root, "calls.jsonl")
        os.environ["FAKE_CODEX_LOG"] = self.log
        os.environ.pop("FAKE_CODEX_MODE", None)
        self.addCleanup(os.environ.pop, "FAKE_CODEX_LOG", None)
        self.addCleanup(os.environ.pop, "FAKE_CODEX_MODE", None)
        self.codex = os.path.join(self.root, "codex")  # the fake, run with this Python
        with open(self.codex, "w") as handle:
            handle.write(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
        os.chmod(self.codex, os.stat(self.codex).st_mode | stat.S_IEXEC)
        self.team = CodexAgents(os.path.join(self.root, "team"), codex=self.codex)
        self.addCleanup(self.team.close)

    def calls(self):
        with open(self.log) as handle:
            return [json.loads(line) for line in handle]

    def test_first_turn_starts_read_only_in_its_own_folder_and_later_turns_resume(self):
        first, state1 = self.team.run_turn("drawA", "You are drawA")
        second, state2 = self.team.run_turn("drawA", "Supervisor: fix it")
        (a, b) = self.calls()
        self.assertEqual((state1, state2), ("idle", "idle"))
        self.assertNotIn("resume", a["args"])
        self.assertEqual(a["args"][a["args"].index("-s") + 1], "read-only")
        self.assertEqual(a["args"][a["args"].index("-C") + 1], os.path.join(self.root, "team", "drawA"))
        self.assertEqual(b["args"][:2], ["resume", a["thread"]])
        self.assertNotIn("-s", b["args"])
        self.assertEqual(second, f"echo: Supervisor: fix it | images 0 | thread {a['thread']}")
        self.team.run_turn("drawB", "You are drawB")
        self.assertNotEqual(self.calls()[2]["thread"], a["thread"])  # every member has its own conversation

    def test_fresh_members_start_a_new_conversation_every_turn(self):
        team = CodexAgents(os.path.join(self.root, "team3"), codex=self.codex, fresh=True)
        self.addCleanup(team.close)
        team.run_turn("drawA", "You are drawA")
        team.run_turn("drawA", "Supervisor: fix it")
        a, b = self.calls()
        self.assertNotIn("resume", b["args"])
        self.assertEqual(b["args"][b["args"].index("-s") + 1], "read-only")
        self.assertNotEqual(a["thread"], b["thread"])

    def test_forget_gives_one_member_a_new_conversation(self):  # teamrun.py: fresh and keep members in one instance
        self.team.run_turn("drawA", "You are drawA")
        self.team.run_turn("art", "You are art")
        self.team.forget("drawA")
        self.team.run_turn("drawA", "Supervisor: fix it")
        self.team.run_turn("art", "again")
        a, art1, b, art2 = self.calls()
        self.assertNotIn("resume", b["args"])
        self.assertNotEqual(a["thread"], b["thread"])
        self.assertEqual(art2["args"][:2], ["resume", art1["thread"]])

    def test_images_go_with_absolute_paths_and_the_model_is_passed(self):
        team = CodexAgents(os.path.join(self.root, "team2"), codex=self.codex, model="gpt-test")
        self.addCleanup(team.close)
        reply, _ = team.run_turn("art", "You are the art director", files=["row1.png", "ours.png"])
        call = self.calls()[0]
        self.assertEqual(call["images"], [os.path.abspath("row1.png"), os.path.abspath("ours.png")])
        self.assertEqual(call["args"][call["args"].index("-m") + 1], "gpt-test")
        self.assertIn("images 2", reply)

    def test_tokens_turns_and_state_reach_agents_json(self):
        self.team.run_turn("drawA", "You are drawA")
        self.team.run_turn("drawA", "again")
        with open(os.path.join(self.root, "team", "agents.json")) as handle:
            agent = json.load(handle)["drawA"]
        self.assertEqual((agent["state"], agent["tokens"], agent["turns"]), ("idle", 220, 2))
        self.assertTrue(agent["stream"]["text"].startswith("echo: again"))

    def test_stdin_is_closed_even_when_ours_is_an_open_pipe(self):
        read_end, write_end = os.pipe()  # like the tool runner: a pipe nobody closes
        saved = os.dup(0)
        os.dup2(read_end, 0)
        try:
            reply, state = self.team.run_turn("drawA", "You are drawA", timeout=5)
        finally:
            os.dup2(saved, 0)
            for fd in (saved, read_end, write_end):
                os.close(fd)
        self.assertEqual(state, "idle")
        self.assertTrue(reply.startswith("echo: You are drawA"))

    def test_a_turn_over_the_time_limit_is_stopped(self):
        os.environ["FAKE_CODEX_MODE"] = "sleep"
        reply, state = self.team.run_turn("drawA", "You are drawA", timeout=1)
        self.assertEqual((reply, state), ("", "aborted"))

    def test_an_error_event_is_reported(self):
        os.environ["FAKE_CODEX_MODE"] = "error"
        reply, state = self.team.run_turn("drawA", "You are drawA")
        self.assertEqual((reply, state), ("", "error"))

    def test_a_failed_turn_is_an_error_even_when_codex_exits_0(self):
        os.environ["FAKE_CODEX_MODE"] = "failed-exit-0"
        reply, state = self.team.run_turn("drawA", "You are drawA")
        self.assertEqual((reply, state), ("", "error"))


if __name__ == "__main__":
    unittest.main()
