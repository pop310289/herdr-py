"""Scenario (pending the user's review): a team run (team.py, condition T) is accepted only when the validator finished
its turn and said VERDICT: ACCEPT. Today supervise() returns "accepted" after the public check passes even when the
validator's turn ended in an error or its reply has no verdict at all.
Seen by: the Codex controller research, 2026-10-08 (docs/experiments/herdr_research_20261008, finding 1)."""
import os
import sys
import unittest

import herdr_py

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(herdr_py.__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from test_team import Base  # noqa: E402


class ValidatorMustFinish(Base):
    def exec_ok_then_val(self, val_turn):
        def on_prompt(sid, text):
            name = self.fake.name_of(sid)
            self.prompts.append((name, text))
            if name.startswith("exec"):
                self.write("OK", notes="Done: ok.txt\nNext: -")
                self.fake.turn(sid, "done")
            elif name == "val":
                val_turn(sid)
        self.fake.on_prompt = on_prompt

    def test_a_reply_without_a_verdict_is_not_an_accept(self):
        self.exec_ok_then_val(lambda sid: self.fake.turn(sid, "Looks fine to me, I think."))
        s = self.run_team("T", rounds=1)
        self.assertNotEqual(s["outcome"], "accepted")

    def test_a_validator_whose_turn_ended_in_an_error_is_not_an_accept(self):
        def broken(sid):
            self.fake.emit("session.status", sessionID=sid, status={"type": "busy"})
            self.fake.say(sid, "VERDICT: ACCEPT")  # words from a turn that did not end normally do not count
            self.fake.emit("session.error", sessionID=sid, error={"name": "APIError", "data": {"message": "overloaded"}})
        self.exec_ok_then_val(broken)
        s = self.run_team("T", rounds=1)
        self.assertNotEqual(s["outcome"], "accepted")

    def test_a_finished_accept_is_still_accepted(self):  # the fix must not refuse everything
        self.exec_ok_then_val(lambda sid: self.fake.turn(sid, "Checked ok.txt.\nVERDICT: ACCEPT"))
        self.assertEqual(self.run_team("T", rounds=1)["outcome"], "accepted")


if __name__ == "__main__":
    unittest.main()
