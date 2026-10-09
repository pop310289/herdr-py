"""herdr_py/members.py: one contract for every kind of member (OpenCode through the daemon, Codex CLI, Claude Code CLI,
any program on stdin and stdout), and one team that mixes them."""
import json
import os
import shutil
import sys
import tempfile
import textwrap
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from herdr_py.client import ClientError  # noqa: E402
from herdr_py.members import CommandMembers, DaemonMembers, MemberError, Members, parse_member, this_turn_reply  # noqa: E402


def script(folder, name, body):
    path = os.path.join(folder, name)
    with open(path, "w") as handle:
        handle.write(textwrap.dedent(body))
    return path


class ParseTest(unittest.TestCase):
    def test_member_specs(self):
        self.assertEqual(parse_member("a=codex"), {"name": "a", "backend": "codex", "model": None, "command": None})
        self.assertEqual(parse_member("b=opencode:ollama/qwen3-8b-32k:latest")["model"], "ollama/qwen3-8b-32k:latest")
        self.assertEqual(parse_member("c=command:python3 loop.py --seed 2")["command"], "python3 loop.py --seed 2")
        for bad, words in (("codex", "NAME="), ("1a=codex", "NAME="), ("a/b=codex", "NAME="), ("a=gpt", "one of"),
                           ("a=command:", "give the command"), ("a=command:  ", "give the command")):
            with self.assertRaisesRegex(MemberError, words):
                parse_member(bad)


class CommandTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_the_prompt_goes_in_and_the_reply_comes_out(self):
        echo = script(self.dir, "echo.py", """
            import os, sys
            print(os.environ["HERDR_MEMBER"], os.environ.get("HERDR_MODEL"), sys.stdin.read().upper())
            print("to stderr", file=sys.stderr)
            """)
        members = CommandMembers(os.path.join(self.dir, "logs"), cwd=self.dir)
        members.add("a", [sys.executable, "-B", echo])
        self.assertEqual(members.run_turn("a", "hello", model="m1"), ("a m1 HELLO", "idle"))
        with open(os.path.join(self.dir, "logs", "a.stderr.log")) as handle:
            self.assertIn("to stderr", handle.read())
        self.assertIsNone(members.tokens("a"))

    def test_a_workdir_turn_runs_the_program_there(self):
        where = script(self.dir, "where.py", """
            import os, sys
            sys.stdin.read()
            print(os.getcwd(), os.environ.get("HERDR_ACCESS"))
            """)
        ws = os.path.join(self.dir, "ws")
        os.makedirs(ws)
        members = Members([{"name": "a", "backend": "command", "model": None, "command": f"{sys.executable} -B {where}"}],
                          os.path.join(self.dir, "work"), cwd=self.dir)
        self.assertEqual(members.run_turn("a", "x", workdir=ws, access="read"), (f"{os.path.realpath(ws)} read", "idle"))
        self.assertEqual(members.run_turn("a", "x"), (f"{os.path.realpath(self.dir)} None", "idle"))

    def test_a_failing_program_is_an_error_and_its_words_are_kept(self):
        members = CommandMembers(os.path.join(self.dir, "logs"), cwd=self.dir)
        members.add("a", [sys.executable, "-c", "print('half an answer'); raise SystemExit(3)"])
        self.assertEqual(members.run_turn("a", "x"), ("half an answer", "error"))

    def test_the_time_limit_stops_the_whole_process_group(self):
        beat = os.path.join(self.dir, "grandchild.beat")
        slow = script(self.dir, "slow.py", f"""
            import subprocess, sys, time
            subprocess.Popen([sys.executable, "-c", "import time\\nwhile True:\\n    open({beat!r}, 'w').write(repr(time.time()))\\n    time.sleep(0.05)"])
            sys.stdin.read()
            time.sleep(30)
            """)
        members = CommandMembers(os.path.join(self.dir, "logs"), cwd=self.dir)
        members.add("a", [sys.executable, "-B", slow])
        start = time.time()
        self.assertEqual(members.run_turn("a", "x", timeout=1), ("", "timeout"))
        self.assertLess(time.time() - start, 10)
        # "stopped" means its heartbeat stopped: a killed process can stay a zombie for a while (in a container
        # nobody may reap it), so whether its pid still exists says nothing
        def heartbeat():
            try:
                with open(beat) as handle:
                    return handle.read()
            except FileNotFoundError:  # killed before its first beat: stopped too
                return None
        time.sleep(0.3)
        first = heartbeat()
        time.sleep(0.6)
        self.assertEqual(heartbeat(), first, "the grandchild still runs after the time limit")


class FakeDaemon:
    """The daemon's socket API as DaemonMembers uses it: agent.list/start/prompt/get/read/abort."""

    def __init__(self, replies, stuck=(), unasked=(), names=None, tools=(), view=None):
        self.replies, self.stuck = list(replies), set(stuck)
        self.calls, self.agents, self.messages = [], {}, {}
        self.unasked, self.names, self.tools = unasked, names, list(tools)  # names: what OpenCode lists in a folder
        self.view = view or (lambda name: {})  # extra agent.get fields (provider_wait_s, a state that changes)

    def call(self, method, **p):
        self.calls.append((method, p))
        name = p.get("name")
        if method == "ping":
            return {"pong": True, "opencode_does_not_ask": None if self.unasked is None else list(self.unasked)}
        if method == "folder.list":
            if self.names is None:
                raise ClientError("error", "OpenCode cannot list it: HTTP 500")
            return {"names": self.names}
        if method == "agent.list":
            return {"agents": [dict({"name": n, "tokens": 42}, **{k: v for k, v in self.view(n).items() if k != "state"})
                               for n in self.agents]}
        if method in ("agent.start", "agent.prompt"):
            if method == "agent.start" and (p.get("fresh") or name not in self.agents):
                self.messages[name] = []
            self.agents[name] = "working" if name in self.stuck else "idle"
            self.messages[name].append({"role": "user", "kind": "text", "text": p.get("prompt") or p.get("text")})
            reply = self.replies.pop(0)
            self.messages[name] += [{"role": "assistant", "kind": "tool", "tool": t} for t in self.tools]
            if reply:
                self.messages[name].append({"role": "assistant", "kind": "text", "text": reply})
            return {}
        if method == "agent.get":
            return dict({"state": self.agents[name], "followups_left": 0, "tokens": 42}, **self.view(name))
        if method == "agent.read":
            return {"messages": self.messages[name][-p["limit"]:]}
        if method == "agent.abort":
            self.agents[name] = "aborted"
            return {}
        raise AssertionError(method)


class DaemonTest(unittest.TestCase):
    def test_turns_start_prompt_or_start_fresh(self):
        daemon = FakeDaemon(["first", "second", "third"])
        members = DaemonMembers(client=daemon, sleep=lambda s: None)
        self.assertEqual(members.run_turn("a", "p1", model="ollama/x"), ("first", "idle"))
        self.assertEqual(members.run_turn("a", "p2"), ("second", "idle"))  # the session is kept
        members.forget("a")
        self.assertEqual(members.run_turn("a", "p3"), ("third", "idle"))
        sent = [(m, p.get("fresh")) for m, p in daemon.calls if m in ("agent.start", "agent.prompt")]
        self.assertEqual(sent, [("agent.start", False), ("agent.prompt", None), ("agent.start", True)])
        self.assertEqual([p.get("model") for m, p in daemon.calls if m == "agent.start"], ["ollama/x", None])
        self.assertEqual((members.tokens("a"), members.tokens("not-yet")), (42, 0))  # none yet is 0, not unknown

    def test_a_turn_without_words_does_not_return_the_last_turns_answer(self):
        daemon = FakeDaemon(["the old answer", ""])
        members = DaemonMembers(client=daemon, sleep=lambda s: None)
        members.run_turn("a", "p1")
        self.assertEqual(members.run_turn("a", "p2"), ("", "idle"))

    def test_the_time_limit_aborts_the_agent(self):
        now = [0.0]
        daemon = FakeDaemon(["late"], stuck={"a"})
        members = DaemonMembers(client=daemon, clock=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s))
        self.assertEqual(members.run_turn("a", "p", timeout=5)[1], "timeout")
        self.assertIn(("agent.abort", {"name": "a", "reason": "turn time limit"}), daemon.calls)

    def test_a_turn_in_a_folder_is_a_new_session_there_with_its_refusals(self):
        work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, work, True)
        open(os.path.join(work, "deck.json"), "w").close()
        daemon = FakeDaemon(["one", "two", "three"], names=["deck.json"])
        members = DaemonMembers(client=daemon, sleep=lambda s: None)
        members.run_turn("a", "p1", workdir=work, access="write")
        members.run_turn("a", "p2", workdir=work, access="read")  # no forget(): a turn in a folder never continues one
        members.run_turn("a", "p3", workdir=work)
        starts = [p for m, p in daemon.calls if m == "agent.start"]
        self.assertEqual([(p["fresh"], p["directory"], p["deny"]) for p in starts],
                         [(False, work, ["external_directory"]), (True, work, ["external_directory", "edit", "bash"]),
                          (True, work, ["external_directory"])])  # no access given: write
        self.assertEqual(len([m for m, _ in daemon.calls if m == "ping"]), 1)  # OpenCode's config is read once

    def test_a_folder_turn_is_refused_before_anything_starts_when_bounds_cannot_hold(self):
        work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, work, True)
        open(os.path.join(work, "deck.json"), "w").close()
        cases = [(dict(unasked=None, names=["deck.json"]), "write", "could not read OpenCode's permission config"),
                 (dict(unasked=["edit=unset"], names=["deck.json"]), "read", "does not ask before edit"),
                 (dict(unasked=['bash="allow"'], names=["deck.json"]), "read", "does not ask before bash"),
                 (dict(unasked=["external_directory=unset"], names=["deck.json"]), "write", "does not ask before external_directory"),
                 (dict(unasked=[], names=None), "write", "OpenCode cannot see"),
                 (dict(unasked=[], names=["deck.json", "other.txt"]), "write", "lists other files")]
        for kw, access, says in cases:
            daemon = FakeDaemon(["x"], **kw)
            with self.assertRaises(MemberError) as caught:
                DaemonMembers(client=daemon, sleep=lambda s: None).run_turn("a", "p", workdir=work, access=access)
            self.assertIn(says, str(caught.exception))
            self.assertFalse([m for m, _ in daemon.calls if m in ("agent.start", "agent.prompt")], says)
        # edits not asked about only matter to a step that reads: a writing step's edits are its job
        daemon = FakeDaemon(["fine"], unasked=["edit=unset", "bash=unset"], names=["deck.json"])
        self.assertEqual(DaemonMembers(client=daemon, sleep=lambda s: None).run_turn("a", "p", workdir=work), ("fine", "idle"))

    def test_this_turns_tool_calls_are_logged_once(self):
        log = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, log, True)
        daemon = FakeDaemon(["one", "two"], tools=["read"])
        members = DaemonMembers(client=daemon, sleep=lambda s: None, log_dir=os.path.join(log, "opencode"))
        members.run_turn("a", "p1")
        members.run_turn("a", "p2")  # the same session: turn 1's call is in the transcript again
        with open(os.path.join(log, "opencode", "events.jsonl")) as handle:
            rows = [json.loads(line) for line in handle]
        self.assertEqual([(r["agent"], r["event"]["tool"]) for r in rows], [("a", "read"), ("a", "read")])

    def test_time_the_provider_lost_is_added_to_the_limit_up_to_one_more_limit(self):
        now = [0.0]
        clock, sleep = (lambda: now[0]), (lambda s: now.__setitem__(0, now[0] + s))

        # 50 s lost to the provider: the member still has its 100 s, and finishes at 140 s
        daemon = FakeDaemon(["done"], stuck={"a"}, view=lambda n: {"provider_wait_s": 50 if now[0] >= 30 else 0,
                                                                     **({"state": "idle"} if now[0] >= 140 else {})})
        self.assertEqual(DaemonMembers(client=daemon, clock=clock, sleep=sleep).run_turn("a", "p", timeout=100), ("done", "idle"))

        # without the provider's 50 s the same member would have been stopped at 100 s
        now[0] = 0.0
        daemon = FakeDaemon(["late"], stuck={"a"}, view=lambda n: {"state": "idle"} if now[0] >= 140 else {})
        self.assertEqual(DaemonMembers(client=daemon, clock=clock, sleep=sleep).run_turn("a", "p", timeout=100)[1], "timeout")

        # a provider that eats everything: stopped after two limits, as a broken setup
        now[0] = 0.0
        daemon = FakeDaemon(["x"], stuck={"a"}, view=lambda n: {"provider_wait_s": now[0]})
        reply, state = DaemonMembers(client=daemon, clock=clock, sleep=sleep).run_turn("a", "p", timeout=100)
        self.assertEqual(state, "provider_stall")
        self.assertTrue(200 < now[0] <= 202)
        self.assertTrue(reply.startswith("(the model provider stalled:"))
        self.assertIn("the model provider stalled", [p["reason"] for m, p in daemon.calls if m == "agent.abort"][0])

    def test_only_this_turns_provider_time_counts_in_a_kept_session(self):
        now = [0.0]
        daemon = FakeDaemon(["one", "two"], view=lambda n: {"provider_wait_s": 500})  # lost in an earlier turn
        members = DaemonMembers(client=daemon, clock=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s))
        members.run_turn("a", "p1")
        daemon.stuck.add("a")
        self.assertEqual(members.run_turn("a", "p2", timeout=100)[1], "timeout")  # not 500 s more
        self.assertTrue(100 < now[0] <= 102)

    def test_the_reply_is_what_came_after_the_last_prompt(self):
        messages = [{"role": "user", "kind": "text", "text": "p1"}, {"role": "assistant", "kind": "text", "text": "a1"},
                    {"role": "user", "kind": "text", "text": "p2"}, {"role": "assistant", "kind": "tool", "tool": "bash"},
                    {"role": "assistant", "kind": "text", "text": "thinking"}, {"role": "assistant", "kind": "text", "text": " a2 "}]
        self.assertEqual(this_turn_reply(messages), "a2")
        self.assertEqual(this_turn_reply(messages[:4]), "")
        self.assertEqual(this_turn_reply(messages[3:]), "a2")  # the prompt scrolled out of the window: all of it is this turn

    def test_a_long_turns_tool_calls_are_all_logged(self):
        log = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, log, True)
        daemon = FakeDaemon(["done"], tools=["read"] * 150)  # one turn, 150 tool calls
        DaemonMembers(client=daemon, sleep=lambda s: None, log_dir=log).run_turn("a", "p")
        with open(os.path.join(log, "events.jsonl")) as handle:
            self.assertEqual(len(handle.readlines()), 150)

    def test_a_compaction_in_the_middle_of_a_turn_does_not_start_a_new_turn(self):
        # as OpenCode 1.18.32 wrote it when a step's session filled up: a compaction, its summary, then a synthetic prompt
        messages = [{"role": "user", "kind": "text", "text": "the step's prompt"},
                    {"role": "assistant", "kind": "tool", "tool": "read"}, {"role": "assistant", "kind": "tool", "tool": "grep"},
                    {"role": "user", "kind": "compaction"}, {"role": "assistant", "kind": "text", "text": "## Objective ..."},
                    {"role": "user", "kind": "text", "text": "Continue if you have next steps, or stop and ask for clarification "
                     "if you are unsure how to proceed.", "synthetic": True},
                    {"role": "assistant", "kind": "tool", "tool": "read"}, {"role": "assistant", "kind": "text", "text": "done"}]
        self.assertEqual(this_turn_reply(messages), "done")
        log = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, log, True)
        members = DaemonMembers(client=FakeDaemon([]), log_dir=log)
        members.note_tools("r", messages)
        with open(os.path.join(log, "events.jsonl")) as handle:
            self.assertEqual([json.loads(line)["event"]["tool"] for line in handle], ["read", "grep", "read"])


class MixedTeamTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        for key, fake in (("CODEX_BIN", "fake_codex.py"), ("CLAUDE_BIN", "fake_claude.py")):
            wrapper = os.path.join(self.dir, fake[:-3])  # run the fake with this Python, as the adapters' own tests do
            with open(wrapper, "w") as handle:
                handle.write(f"#!/bin/sh\nexec {sys.executable} {os.path.join(HERE, fake)} \"$@\"\n")
            os.chmod(wrapper, 0o755)
            self.addCleanup(os.environ.pop, key, None)
            os.environ[key] = wrapper
        os.environ["FAKE_CLAUDE_STATE"] = os.path.join(self.dir, "claude_state.json")
        self.addCleanup(os.environ.pop, "FAKE_CLAUDE_STATE", None)

    def test_one_team_of_codex_claude_and_a_program(self):
        prog = script(self.dir, "prog.py", "import sys; print('program got ' + sys.stdin.read()[:9])")
        specs = [parse_member("cx=codex:gpt-test"), parse_member("cl=claude:haiku"),
                 parse_member(f"pg=command:{sys.executable} -B {prog}")]
        team = Members(specs, os.path.join(self.dir, "work"))
        self.addCleanup(team.close)
        self.assertEqual(team.names(), ["cx", "cl", "pg"])
        # before anyone ran: Codex and Claude have used 0 tokens (not "unknown"), so a first turn's tokens can be counted
        self.assertEqual([team.tokens(n) for n in team.names()], [0, 0, None])
        replies = {name: team.run_turn(name, "the same task", timeout=60) for name in team.names()}
        self.assertEqual({n: r[1] for n, r in replies.items()}, {"cx": "idle", "cl": "idle", "pg": "idle"})
        self.assertTrue(replies["cx"][0].startswith("echo: the same task"))
        self.assertTrue(replies["cl"][0].startswith("echo: the same task"))
        self.assertEqual(replies["pg"][0], "program got the same")  # the reply is stripped
        summary = {s["name"]: s for s in team.summary()}
        self.assertEqual({n: s["turns"] for n, s in summary.items()}, {"cx": 1, "cl": 1, "pg": 1})
        self.assertIsInstance(summary["cl"]["tokens"], int)
        self.assertIsNone(summary["pg"]["tokens"])

    def test_a_coop_run_counts_the_tokens_of_every_turn_including_the_first(self):
        from herdr_py.coop import CoopRun
        team = Members([parse_member("cx=codex"), parse_member("cl=claude")], os.path.join(self.dir, "work"))
        self.addCleanup(team.close)
        out = os.path.join(self.dir, "run")
        CoopRun("Give a number.", lambda path: ("valid", 1, ""), team, team.names(), out, mode="I", rounds=2).run()
        with open(os.path.join(out, "run.jsonl")) as handle:
            turns = [json.loads(line) for line in handle]
        self.assertEqual(len(turns), 4)
        self.assertTrue(all(isinstance(t["tokens"], int) and t["tokens"] > 0 for t in turns), turns)
        self.assertEqual([t["tokens"] for t in turns if t["member"] == "cx"], [110, 110])  # the fake's usage, every turn

    def test_a_missing_program_fails_its_turn_and_says_why(self):
        team = Members([parse_member("pg=command:/no/such/program")], os.path.join(self.dir, "work"))
        text, state = team.run_turn("pg", "x")
        self.assertEqual(state, "error")
        self.assertIn("could not run", text)
        self.assertEqual(team.summary()[0]["states"], {"error": 1})

    def test_fresh_sessions_start_every_turn_anew(self):
        class Recorder:
            def __init__(self):
                self.forgotten = []

            def forget(self, name):
                self.forgotten.append(name)

            def run_turn(self, name, prompt, model=None, files=(), timeout=600):
                return "ok", "idle"

            def close(self):
                pass
        for sessions, expected in (("fresh", ["a", "a"]), ("keep", [])):
            team = Members([parse_member("a=command:true")], os.path.join(self.dir, sessions), sessions=sessions)
            team.backends["command"] = recorder = Recorder()
            team.run_turn("a", "x")
            team.run_turn("a", "y")
            self.assertEqual(recorder.forgotten, expected, sessions)

    def test_setup_mistakes_are_refused(self):
        with self.assertRaisesRegex(MemberError, "socket"):
            Members([parse_member("oc=opencode")], self.dir)
        with self.assertRaisesRegex(MemberError, "two members"):
            Members([parse_member("a=codex"), parse_member("a=claude")], self.dir)
        with self.assertRaisesRegex(MemberError, "fresh or keep"):
            Members([parse_member("a=codex")], self.dir, sessions="sometimes")


if __name__ == "__main__":
    unittest.main()
