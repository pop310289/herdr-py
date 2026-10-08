"""Scenario (pending the user's review): when a member's turn ends without the backend reporting its usage (a Codex
turn stopped at the time limit never sends turn.completed), the turn's tokens are unknown (None) and the run says its
token total is incomplete; today the turn is recorded as having used 0 tokens.
Seen by: the Codex controller research ("missing time and usage cannot be reported as zero"), localA ("tokens are
counted differently by each backend") and the P26 pilot (first-round tokens missing), 2026-10-08."""
import json
import os
import shutil
import sys
import tempfile
import unittest

import herdr_py
from herdr_py.coop import CoopRun
from herdr_py.members import Members, parse_member

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(herdr_py.__file__)))
FAKE = os.path.join(ROOT, "tests", "fake_codex.py")


class UnknownUsage(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        wrapper = os.path.join(self.dir, "codex")
        with open(wrapper, "w") as handle:  # the member called "slow" (its own folder, -C .../slow) never finishes
            handle.write("#!/bin/sh\ncase \"$*\" in */slow\ *) FAKE_CODEX_MODE=sleep; export FAKE_CODEX_MODE;; esac\n"
                         f"exec {sys.executable} {FAKE} \"$@\"\n")
        os.chmod(wrapper, 0o755)
        os.environ["CODEX_BIN"] = wrapper
        self.addCleanup(os.environ.pop, "CODEX_BIN", None)
        os.environ.pop("FAKE_CODEX_MODE", None)

    def run_two(self):
        team = Members([parse_member("fast=codex"), parse_member("slow=codex")], os.path.join(self.dir, "work"))
        self.addCleanup(team.close)
        out = os.path.join(self.dir, "run")
        summary = CoopRun("Give a number.", lambda path: ("valid", 1, ""), team, team.names(), out,
                          mode="I", rounds=1, turn_timeout=2).run()
        with open(os.path.join(out, "run.jsonl")) as handle:
            return summary, {t["member"]: t for t in map(json.loads, handle)}

    def test_a_turn_without_reported_usage_has_unknown_tokens(self):
        summary, turns = self.run_two()
        self.assertEqual(turns["slow"]["state"], "timeout")
        self.assertIsNone(turns["slow"]["tokens"])  # not 0: nothing says how much it used
        self.assertEqual(turns["fast"]["tokens"], 110)  # the fake's usage still counts where it was reported
        self.assertEqual(summary["tokens_known_for_turns"], 1)


if __name__ == "__main__":
    unittest.main()
