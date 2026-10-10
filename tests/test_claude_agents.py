"""herdr_py/claude_agents.ClaudeAgents against tests/fake_claude.py: sessions, resume, images, tokens, stdin, time limits, errors
and the environment a member starts with."""
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "examples", "slide_team"))
from herdr_py.claude_agents import ClaudeAgents  # noqa: E402

FAKE = os.path.join(HERE, "fake_claude.py")
ENV = ("FAKE_CLAUDE_LOG", "FAKE_CLAUDE_STATE", "FAKE_CLAUDE_MODE")


class ClaudeAgentsTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        self.log = os.path.join(self.root, "calls.jsonl")
        saved = {k: os.environ.get(k) for k in ENV}
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v) for k, v in saved.items()])
        os.environ["FAKE_CLAUDE_LOG"] = self.log
        os.environ["FAKE_CLAUDE_STATE"] = os.path.join(self.root, "sessions.json")
        os.environ.pop("FAKE_CLAUDE_MODE", None)
        self.claude = os.path.join(self.root, "claude")  # the fake, run with this Python
        with open(self.claude, "w") as handle:
            handle.write(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
        os.chmod(self.claude, os.stat(self.claude).st_mode | stat.S_IEXEC)
        self.team = ClaudeAgents(os.path.join(self.root, "team"), claude=self.claude)
        self.addCleanup(self.team.close)

    def calls(self):
        with open(self.log) as handle:
            return [json.loads(line) for line in handle]

    def test_first_turn_has_no_tools_in_its_own_folder_and_later_turns_resume(self):
        first, state1 = self.team.run_turn("drawA", "-You are drawA")  # a prompt that starts with "-" is still the prompt
        second, state2 = self.team.run_turn("drawA", "Supervisor: fix it")
        a, b = self.calls()
        self.assertEqual((state1, state2), ("idle", "idle"))
        self.assertTrue(first.startswith("echo: -You are drawA"), first)
        for flag in ("--safe-mode", "--verbose"):
            self.assertIn(flag, a["args"])
        self.assertEqual(a["args"][a["args"].index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(a["tools"], [])
        self.assertNotIn("--resume", a["args"])
        self.assertEqual(a["cwd"], os.path.realpath(os.path.join(self.root, "team", "drawA")))
        self.assertEqual(b["args"][b["args"].index("--resume") + 1], a["session"])
        self.assertTrue(b["resumed"])
        self.assertEqual(second, f"echo: Supervisor: fix it | images 0 | session {a['session']}")
        self.team.run_turn("drawB", "You are drawB")
        self.assertNotEqual(self.calls()[2]["session"], a["session"])  # every member has its own conversation

    def test_a_workspace_turn_runs_there_with_file_tools_only(self):  # flags checked against the real CLI (module notes)
        ws = os.path.join(self.root, "ws")
        os.makedirs(ws)
        self.team.run_turn("w1", "plain first")  # a conversation the member keeps outside any workspace
        reply, state = self.team.run_turn("w1", "edit things", workdir=ws)
        self.team.run_turn("w1", "look only", workdir=ws, access="read")
        self.team.run_turn("w1", "plain again")
        plain, write, read, again = self.calls()
        self.assertEqual(state, "idle")
        self.assertEqual(write["cwd"], os.path.realpath(ws))
        self.assertEqual(write["args"].count("--permission-mode"), 1)  # replaced, not given twice
        self.assertEqual(write["args"][write["args"].index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual(write["tools"], ["Read", "Edit", "Write", "Glob", "Grep"])
        self.assertNotIn("--allowedTools", write["args"])  # with it the real CLI let the member write anywhere
        self.assertIn("--safe-mode", write["args"])
        self.assertEqual(read["args"][read["args"].index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(read["tools"], ["Read", "Glob", "Grep"])
        self.assertFalse(write["resumed"] or read["resumed"])  # a workspace turn is always a new conversation
        self.assertEqual(again["session"], plain["session"])  # and it never becomes the one resumed elsewhere
        self.assertEqual((again["tools"], again["cwd"]), ([], os.path.realpath(os.path.join(self.root, "team", "w1"))))
        with self.assertRaises(ValueError):
            self.team.run_turn("w1", "x", workdir=ws, access="admin")

    def test_a_research_turn_reads_the_folder_and_may_only_use_the_web_tools(self):  # checked against the real CLI
        ws = os.path.join(self.root, "ws2")
        os.makedirs(ws)
        self.team.run_turn("r1", "find facts", workdir=ws, access="research")
        call = self.calls()[-1]
        args = call["args"]
        self.assertEqual(args[args.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(call["tools"], ["Read", "Glob", "Grep", "WebSearch", "WebFetch"])
        rules = json.loads(args[args.index("--settings") + 1])
        self.assertEqual(rules, {"permissions": {"allow": ["WebSearch", "WebFetch"]}})  # without it dontAsk refused WebSearch
        self.assertNotIn("--allowedTools", args)
        self.assertEqual(call["cwd"], os.path.realpath(ws))

    def test_a_repair_continues_the_workspace_turn_that_just_ended_and_nothing_else(self):
        ws, other = os.path.join(self.root, "ws3"), os.path.join(self.root, "ws4")
        os.makedirs(ws)
        os.makedirs(other)
        self.assertFalse(self.team.can_continue("r1", ws, "research"))  # nothing to continue yet
        self.team.run_turn("r1", "find facts", workdir=ws, access="research")
        first = self.calls()[-1]
        self.assertTrue(self.team.can_continue("r1", ws, "research"))
        self.assertFalse(self.team.can_continue("r1", other, "research"))  # another folder
        self.assertFalse(self.team.can_continue("r1", ws, "read"))  # another access
        self.team.run_turn("r1", "the judge said: fix it", workdir=ws, access="research", cont=True)
        fix = self.calls()[-1]
        self.assertTrue(fix["resumed"])
        self.assertEqual(fix["args"][fix["args"].index("--resume") + 1], first["session"])
        self.assertEqual(fix["tools"], first["tools"])  # still a research turn, in the same folder
        self.assertEqual(fix["cwd"], first["cwd"])
        self.team.forget("r1")
        self.assertFalse(self.team.can_continue("r1", ws, "research"))
        with self.assertRaises(ValueError):
            self.team.run_turn("r1", "again", workdir=ws, access="research", cont=True)
        self.team.run_turn("r1", "a new todo", workdir=ws, access="research")
        self.assertFalse(self.calls()[-1]["resumed"])  # a new turn is a new conversation

    def test_fresh_members_and_forget_start_a_new_conversation(self):
        team = ClaudeAgents(os.path.join(self.root, "team3"), claude=self.claude, fresh=True)
        self.addCleanup(team.close)
        team.run_turn("drawA", "You are drawA")
        team.run_turn("drawA", "Supervisor: fix it")
        self.team.run_turn("art", "one")
        self.team.forget("art")
        self.team.run_turn("art", "two")
        calls = self.calls()
        self.assertEqual([c["resumed"] for c in calls], [False, False, False, False])
        self.assertNotEqual(calls[0]["session"], calls[1]["session"])
        self.assertNotEqual(calls[2]["session"], calls[3]["session"])

    def test_images_are_listed_and_their_folders_made_readable(self):
        team = ClaudeAgents(os.path.join(self.root, "team2"), claude=self.claude, model="claude-test")
        self.addCleanup(team.close)
        renders = os.path.join(self.root, "renders")
        os.makedirs(renders)
        paths = [os.path.join(renders, name) for name in ("row1-original.png", "row1-ours.png")]
        for path in paths:
            with open(path, "wb") as handle:
                handle.write(b"\x89PNG\r\n\x1a\n")
        reply, state = team.run_turn("art", "You are the art director", files=paths)
        call = self.calls()[0]
        self.assertEqual(state, "idle")
        self.assertEqual(call["args"][call["args"].index("--model") + 1], "claude-test")
        self.assertEqual(call["tools"], ["Read"])
        self.assertEqual(call["listed"], [os.path.abspath(p) for p in paths])
        self.assertIn("Image 2: " + os.path.abspath(paths[1]), call["prompt"])
        self.assertIn("images 2", reply)  # readable only because --add-dir names their folder

    def test_tokens_come_from_the_result_and_reach_agents_json(self):
        self.team.run_turn("drawA", "You are drawA")
        self.team.run_turn("drawA", "again")
        with open(os.path.join(self.root, "team", "agents.json")) as handle:
            agent = json.load(handle)["drawA"]
        self.assertEqual((agent["state"], agent["tokens"], agent["turns"]), ("idle", 2 * 67, 2))
        self.assertTrue(agent["stream"]["text"].startswith("echo: again"))

    def test_a_turn_without_a_result_counts_its_messages_once(self):
        os.environ["FAKE_CLAUDE_MODE"] = "no-result"
        reply, state = self.team.run_turn("drawA", "You are drawA")
        self.assertEqual((reply, state), ("", "error"))
        self.assertEqual(self.team.agents["drawA"]["tokens"], 61)  # one message in two events, usage of its start

    def test_stdin_is_closed_even_when_ours_is_an_open_pipe(self):
        read_end, write_end = os.pipe()  # like the tool runner: a pipe nobody closes
        saved = os.dup(0)
        os.dup2(read_end, 0)
        try:
            reply, state = self.team.run_turn("drawA", "You are drawA", timeout=10)
        finally:
            os.dup2(saved, 0)
            for fd in (saved, read_end, write_end):
                os.close(fd)
        self.assertEqual(state, "idle")
        self.assertLess(self.calls()[0]["stdin_wait"], 1.0)  # the real CLI waits 3 s on an open pipe

    def test_a_turn_over_the_time_limit_is_stopped_with_what_it_started(self):
        os.environ["FAKE_CLAUDE_MODE"] = "sleep-child"
        start = time.time()
        reply, state = self.team.run_turn("drawA", "You are drawA", timeout=1)
        self.assertEqual((reply, state), ("", "timeout"))
        with open(os.path.join(self.root, "team", "events.jsonl")) as handle:
            self.assertEqual(json.loads(handle.read().splitlines()[-1]).get("timeout"), 1)
        self.assertLess(time.time() - start, 10)  # a child holding the pipe would keep us reading for 30 s

    def test_an_api_error_is_an_error_and_its_text_is_not_the_reply(self):
        for mode in ("error", "error-exit-0"):
            os.environ["FAKE_CLAUDE_MODE"] = mode
            self.assertEqual(self.team.run_turn("drawA", "You are drawA"), ("", "error"), mode)
        os.environ["FAKE_CLAUDE_MODE"] = "echo"
        self.assertEqual(self.calls()[1]["resumed"], True)  # an API error keeps the conversation
        self.assertEqual(self.team.run_turn("drawA", "again")[1], "idle")

    def test_a_session_that_cannot_be_resumed_is_dropped(self):
        self.team.run_turn("drawA", "You are drawA")
        os.remove(os.environ["FAKE_CLAUDE_STATE"])  # the saved conversation is gone
        self.assertEqual(self.team.run_turn("drawA", "again"), ("", "error"))
        reply, state = self.team.run_turn("drawA", "and again")
        self.assertEqual(state, "idle")
        self.assertEqual([c["resumed"] for c in self.calls()], [False, True, False])

    def test_a_missing_cli_raises_and_the_member_shows_an_error(self):
        team = ClaudeAgents(os.path.join(self.root, "team4"), claude=os.path.join(self.root, "no-such-claude"))
        self.addCleanup(team.close)
        with self.assertRaises(OSError):
            team.run_turn("drawA", "You are drawA")
        with open(os.path.join(self.root, "team4", "agents.json")) as handle:
            self.assertEqual(json.load(handle)["drawA"]["state"], "error")

    def test_the_member_does_not_inherit_the_parent_claude_session(self):
        saved = {k: os.environ.get(k) for k in ("CLAUDECODE", "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_EFFORT", "ANTHROPIC_BASE_URL")}
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v) for k, v in saved.items()])
        os.environ.update({"CLAUDECODE": "1", "CLAUDE_CODE_MESSAGING_SOCKET": "/tmp/x.sock", "CLAUDE_EFFORT": "max",
                           "ANTHROPIC_BASE_URL": "http://127.0.0.1:9"})
        self.team.run_turn("drawA", "You are drawA")
        self.assertEqual(self.calls()[0]["env"], ["ANTHROPIC_BASE_URL"])  # the user's own settings still pass


if __name__ == "__main__":
    unittest.main()
