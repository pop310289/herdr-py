"""Scenario (pending the user's review): two cooperative runs at the same time on one herdr-py daemon, each with an
OpenCode member called "a", must not share that agent: each run gets the answer to its own prompt. Today the daemon
agent is named after the member, so the two runs drive the same agent.
Seen by: the Codex controller research's list of gaps ("workspace isolation when two teams run at once"), 2026-10-08."""
import os
import sys
import threading
import unittest

import herdr_py
from herdr_py.members import Members, parse_member

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(herdr_py.__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from test_team import Base  # noqa: E402


class TwoTeamsOneDaemon(Base):
    def test_each_run_gets_its_own_agent(self):
        def on_prompt(sid, text):  # answer slowly, with the prompt's own words
            self.fake.turn(sid, "answer to " + text.split()[0], seconds=0.5)
        self.fake.on_prompt = on_prompt
        socket = self.daemon.socket_path
        teams = {run: Members([parse_member("a=opencode")], os.path.join(self.dir, run), socket=socket) for run in ("red", "blue")}
        replies = {}

        def turn(run):
            replies[run] = teams[run].run_turn("a", f"{run} task: give a number", timeout=20)
        threads = [threading.Thread(target=turn, args=(run,)) for run in teams]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(replies.get("red"), ("answer to red", "idle"))
        self.assertEqual(replies.get("blue"), ("answer to blue", "idle"))


if __name__ == "__main__":
    unittest.main()
