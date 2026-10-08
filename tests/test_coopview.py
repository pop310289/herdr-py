"""herdr_py/coopview.py: the one-page view of a cooperative run. Every turn lands in its round and member's cell with
the right outcome, who built on whom is shown, model text is escaped, a stopped run says so, the backend's log adds
commands and how a turn ended, and coop.py writes the page after every round without ever failing a run for it."""
import contextlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from herdr_py import coopview  # noqa: E402
from herdr_py.coop import CoopRun  # noqa: E402
from test_coop import BEST, Scripted, answer  # noqa: E402

CELL = re.compile(r'<td class="cell ([a-z ]+)">')


def judge(path):
    x = int(open(path).read().strip()) if path else None
    if x == 13:
        raise RuntimeError("the judge broke on 13")
    return ("valid", x, "ok") if x is not None and x <= 100 else ("invalid", None, "over 100")


def builder(prompt):
    best = BEST.search(prompt)
    return answer(int(best.group(2)) + 1, "one more", [best.group(1)]) if best else answer(50, "start at fifty")


class ViewTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.out = os.path.join(self.dir, "run")

    def run_team(self, behaviour, mode="C", rounds=2, **kw):
        members = Scripted(**behaviour)
        run = CoopRun("Give a whole number up to 100.", judge, members, list(behaviour), self.out, mode=mode, rounds=rounds, **kw)
        return run.run()

    def page(self):
        with open(os.path.join(self.out, "view.html"), encoding="utf-8") as handle:
            return handle.read()

    def test_every_outcome_lands_in_its_cell(self):
        n = {"x": 0}

        def twice(prompt):  # the same answer every turn: the second time is a resend
            return answer(7, "seven")

        def dropper(prompt):
            return answer(8, "eight", ["k000000000000"])
        self.run_team({"good": builder, "bad": lambda p: answer(500, "too high"), "slow": lambda p: ("", "timeout"),
                       "broke": lambda p: ("(codex failed)", "error"), "mute": lambda p: "I think 7",
                       "quit": lambda p: "FAILED: no idea", "judgebreak": lambda p: answer(13, "thirteen"),
                       "same": twice, "drop": dropper})
        page = self.page()
        classes = [c.split() for c in CELL.findall(page)]
        expected = ["valid", "invalid", "timeout", "error", "noanswer", "failure", "infra", "valid", "valid"]
        self.assertEqual([c[0] for c in classes], expected * 2)
        self.assertEqual(sum("best" in c for c in classes), 1)
        self.assertIn('<span class="badge">judge error</span>', page)
        self.assertIn("RuntimeError: the judge broke on 13", page)
        bad_cell = page.split('<td class="cell invalid">')[1].split("</td>")[0]
        self.assertIn('<div class="why">over 100</div>', bad_cell)  # the judge's reason, in the cell itself
        self.assertIn("the turn ended timeout", page)
        self.assertIn("I think 7", page)  # the end of a reply without an answer
        self.assertIn("resent", page)
        self.assertIn("named entries it was never shown (ignored): k000000000000", page)
        self.assertEqual(page.count('<circle '), 2)  # one point per round
        self.assertNotIn("still running", page)

    def test_built_on_a_teammate_or_on_its_own_entry(self):
        self.run_team({"a": lambda p: answer(50, "start"), "b": builder}, rounds=2)
        page = self.page()
        self.assertIn("↳ built on", page)
        self.assertRegex(page, r"<li>b \(round 2, valid\) built on a's <code>k[0-9a-f]{12}</code> \(round 1\): \+1</li>")
        shutil.rmtree(self.out)
        self.run_team({"a": builder}, mode="S", rounds=2)
        page = self.page()
        self.assertIn("↳ own", page)
        self.assertNotIn("↳ built on", page)
        self.assertIn("(turn 1, 50)", page)  # mode S counts turns
        self.assertIn("No member built on a teammate", page)

    def test_model_text_is_escaped(self):
        evil = '<script>alert(1)</script><img src=x onerror=alert(2)>'
        self.run_team({"a": lambda p: f"SUMMARY: {evil}\n```\n50\n```", "b": lambda p: f"FAILED: {evil}",
                       "c": lambda p: evil}, rounds=1)
        page = self.page()
        self.assertNotIn("<script>", page)
        self.assertNotIn("<img", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)

    def test_a_stopped_run_says_where_and_why(self):
        self.run_team({"ok": builder, "bad": lambda p: ("", "aborted")}, rounds=3, stop_on_infra_error=True)
        page = self.page()
        self.assertIn('<div class="stopped">Stopped after round 1 of 3: bad round 1: the member&#x27;s backend ended aborted', page)
        self.assertNotIn("round 2", page.split("Every turn")[1].split("Best after")[0])

    def test_the_backend_log_adds_commands_and_how_a_turn_ended(self):
        self.run_team({"a": lambda p: answer(5, "five"), "b": lambda p: ("", "timeout")}, rounds=2)
        log = os.path.join(self.out, "members", "codex")
        os.makedirs(log)
        rows = []
        for name in ("a", "b"):
            for turn in range(2):
                rows.append({"agent": name, "argv": ["codex", "exec"]})
                for _ in range(3 if name == "a" else turn + 1):
                    rows.append({"agent": name, "event": {"type": "item.completed", "item": {"type": "command_execution"}}})
                if name == "b":
                    rows.append({"agent": name, "exit": -9, "timeout": 900, "stderr": "Reading additional input <stdin>"})
        with open(os.path.join(log, "events.jsonl"), "w") as handle:
            handle.writelines(json.dumps(r) + "\n" for r in rows)
        page = coopview.build(self.out)
        cells = page.split('<td class="cell ')[1:]
        self.assertIn("3 commands", cells[0])
        self.assertIn("1 commands", cells[1])  # b's first turn: the n-th logged turn is the n-th turn
        self.assertIn("2 commands", cells[3])
        self.assertIn("codex: exit -9, stopped at the 900 s limit", cells[1])
        self.assertIn("Reading additional input &lt;stdin&gt;", cells[1])

    def test_the_page_is_written_after_every_round(self):
        seen = []

        def peek(prompt):
            path = os.path.join(self.out, "view.html")
            seen.append(len(CELL.findall(open(path).read())) if os.path.exists(path) else None)
            return answer(len(seen), "count")
        self.run_team({"a": peek}, mode="I", rounds=3)
        self.assertEqual(seen, [None, 1, 2])  # before round 1 no page; then one more cell each round

    def test_a_page_that_cannot_be_written_never_fails_the_run(self):
        err = io.StringIO()
        with mock.patch.object(coopview, "save", side_effect=OSError("disk full")), contextlib.redirect_stderr(err):
            s = self.run_team({"a": lambda p: answer(5, "five")}, rounds=2)
        self.assertEqual(s["valid"], 1)
        self.assertIn("warning: view.html not written: OSError: disk full", err.getvalue())

    def test_a_run_still_going(self):
        n = []

        def worse(prompt):  # 9, then 4: the best so far stays 9
            n.append(1)
            return answer(9 if len(n) == 1 else 4, "nine, then four")
        self.run_team({"a": lambda p: answer(5, "five"), "b": worse}, rounds=2)
        os.remove(os.path.join(self.out, "summary.json"))
        with open(os.path.join(self.out, "run.jsonl"), "a") as handle:
            handle.write('{"round": 3, "member": "a", "sta')  # a line still being written
        page = coopview.build(self.out)
        self.assertIn("still running", page)
        self.assertIn("1 line(s) of run.jsonl could not be read", page)
        self.assertIn('<text x="4"', page)
        self.assertEqual(re.findall(r"<title>round (\d): (\d+)</title>", page), [("1", "9"), ("2", "9")])  # best so far

    def test_close_scores_stay_apart_and_the_chart_says_its_scale(self):
        def judge_fine(path):  # answers 1 and 3 score 2.6359830847 and 2.6359830849: apart only in the 10th digit
            return "valid", 2.6359830846 + int(open(path).read()) * 1e-10, "ok"
        CoopRun("x", judge_fine, Scripted(a=lambda p: answer(1 if "Nothing" in p else 3, "go")), ["a"], self.out,
                mode="I", rounds=2).run()
        page = self.page()
        self.assertIn('<span class="score">2.6359830847</span>', page)
        self.assertIn('<span class="score">2.6359830849</span>', page)
        self.assertIn("The axis does not start at 0: from the first to the best point is +2e-10.", page)

    def test_command_line(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(coopview.main([os.path.join(self.dir, "nothing")]), 2)
        self.assertIn("run.jsonl is missing", err.getvalue())
        self.run_team({"a": lambda p: answer(5, "five")}, rounds=1)
        target = os.path.join(self.dir, "page.html")
        with contextlib.redirect_stdout(out):
            self.assertEqual(coopview.main([self.out, "--html", target]), 0)
        self.assertTrue(open(target).read().startswith("<!doctype html>"))


if __name__ == "__main__":
    unittest.main()
