"""herdr_py.rundiag on run folders made by hand: each rule on both sides of where it starts to say something."""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from herdr_py import rundiag  # noqa: E402
from herdr_py.teamkb import TeamKB  # noqa: E402


def ok(score):
    return lambda path: ("valid", score, "ok")


def bad(detail):
    return lambda path: ("invalid", 0, detail)


class RunDiagTest(unittest.TestCase):
    def setUp(self):
        self.run = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.run, True)
        self.kb = TeamKB(os.path.join(self.run, "kb"))
        self.turns, self.wakes = [], []
        self.summary = {"stopped": "the planner says the task is done", "turns": 0, "turn_budget": 10, "seconds": 100.0,
                        "idle_seconds": {}}
        self.n = 0

    def entry(self, member, text, judged, parents=(), kind="result"):
        self.n += 1
        eid = self.kb.propose(member, kind, f"answer {self.n}", artifact=text, name=f"a{self.n}.txt", parents=list(parents))
        self.kb.judge(eid, judged)
        return eid

    def turn(self, member, entry, start, end, tokens=1000, **more):
        e = next(x for x in self.kb.entries() if x["id"] == entry)
        rec = {"member": member, "start": start, "end": end, "tokens": tokens, "entry": entry, "kind": "result",
               "status": e["status"], "score": e["score"]}
        rec.update(more)
        self.turns.append(rec)
        return rec

    def findings(self):
        with open(os.path.join(self.run, "run.jsonl"), "w") as handle:
            handle.writelines(json.dumps(r) + "\n" for r in self.turns)
        with open(os.path.join(self.run, "engine.jsonl"), "w") as handle:
            handle.writelines(json.dumps(r) + "\n" for r in [{"t": 0, "kind": "start"}] + self.wakes)
        self.summary["turns"] = len(self.turns)
        with open(os.path.join(self.run, "summary.json"), "w") as handle:
            json.dump(self.summary, handle)
        return rundiag.findings(self.run)

    def codes(self):
        return {f["code"]: f for f in self.findings()}

    def test_the_same_reason_twice_is_a_finding_once_or_different_reasons_are_not(self):
        a = self.entry("a", "x", bad("the first line is ARTIFACT: data"))
        self.turn("a", a, 0, 10)
        self.assertNotIn("same_failure", self.codes())
        b = self.entry("b", "y", bad("the first line is ARTIFACT: data"))
        self.turn("b", b, 0, 12)
        c = self.entry("b", "z", bad("something else"))
        self.turn("b", c, 12, 20)
        f = self.codes()["same_failure"]
        self.assertEqual(f["numbers"]["answers"], 2)
        self.assertIn("the first line is ARTIFACT: data", f["title"])

    def test_a_result_adds_nothing_when_it_is_no_better_does_not_build_on_the_best_and_nothing_builds_on_it(self):
        first = self.entry("a", "50 by a", ok(50))
        self.turn("a", first, 0, 10)
        twice = self.entry("b", "50 by b", ok(50))  # as good, made on its own, never built on: the same work twice
        self.turn("b", twice, 0, 11, tokens=7000)
        kept = self.entry("a", "50 kept up", ok(50), parents=[first])  # a revision of the best that keeps its score
        self.turn("a", kept, 11, 20)
        low = self.entry("b", "40, built on later", ok(40))
        self.turn("b", low, 11, 21)
        later = self.entry("a", "60 on top", ok(60), parents=[low])
        self.turn("a", later, 21, 30)
        skill = self.entry("b", "ARTIFACT: skill\n---\nname: s\n---\n1. x\n", ok(20), parents=[twice])  # a skill does not count
        self.turn("b", skill, 21, 25)
        f = self.codes()["no_gain"]
        self.assertEqual((f["numbers"]["results"], f["numbers"]["tokens"]), (1, 7000))
        self.assertIn(twice, f["title"])

    def test_a_repaired_turn_naming_its_best_version_again_is_counted_once(self):
        first = self.entry("a", "50 by a", ok(50))
        self.turn("a", first, 0, 10)
        wrong = self.entry("b", "oops", bad("not a number"))  # b's first try: only the repair's "of" names it
        again = self.entry("b", "50 again by b", ok(50), parents=[wrong])  # its repair: as good as the best, not on it
        self.turn("b", again, 0, 12, tokens=4000, repairs=[{"entry": again, "status": "valid", "score": 50, "tokens": 900, "of": wrong}])
        wrong2 = self.entry("c", "nope", bad("not a number"))  # c's one answer, named by the turn and by its repair
        self.turn("c", wrong2, 0, 13, repairs=[{"entry": wrong2, "status": "invalid", "tokens": 100, "of": wrong2, "repeat": True}])
        codes = self.codes()
        self.assertEqual(codes["no_gain"]["numbers"]["results"], 1)  # b's version, once
        self.assertEqual(codes["same_failure"]["numbers"]["answers"], 2)  # b's first try and c's answer, each once

    def test_a_skill_counts_when_built_on_or_opened(self):
        used = self.entry("a", "ARTIFACT: skill\n---\nname: used\n---\n", ok(20))
        opened = self.entry("a", "ARTIFACT: skill\n---\nname: opened\n---\n", ok(20))
        alone = self.entry("a", "ARTIFACT: skill\n---\nname: alone\n---\n", ok(20))
        work = self.entry("b", "70", ok(70), parents=[used])
        for i, e in enumerate((used, opened, alone, work)):
            self.turn("a" if e != work else "b", e, i * 10, i * 10 + 5)
        with open(os.path.join(self.run, "kb", "events.jsonl"), "a") as handle:
            handle.write(json.dumps({"type": "read", "agent": "b", "entry": opened, "t": 1}) + "\n")
        f = self.codes()["unused_skill"]
        self.assertEqual(f["numbers"]["skills"], 1)
        self.assertIn(alone, f["title"])

    def test_pages_opened_by_two_members_overlap_but_one_member_twice_does_not(self):
        e = self.entry("a", "1", ok(1))
        self.turn("a", e, 0, 1)
        os.makedirs(os.path.join(self.run, "members", "claude"))
        calls = [("a", "https://x.example/docs/"), ("a", "https://x.example/docs"), ("a", "https://y.example"),
                 ("b", "https://Y.example/")]
        with open(os.path.join(self.run, "members", "claude", "events.jsonl"), "w") as handle:
            for agent, url in calls:
                handle.write(json.dumps({"t": 1, "agent": agent, "event": {"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "name": "WebFetch", "input": {"url": url}}]}}}) + "\n")
        f = self.codes()["page_overlap"]
        self.assertEqual(f["numbers"], {"pages": 1, "fetches": 4, "unique": 2})  # y by a and b; x by a alone

    def test_turns_after_the_best_are_counted_and_wrap_up_turns_told_apart(self):
        e1 = self.entry("a", "90", ok(90))
        self.turn("a", e1, 0, 10)
        e2 = self.entry("b", "100", ok(100))
        self.turn("b", e2, 5, 20)
        e3 = self.entry("a", "ARTIFACT: skill\n---\nname: s\n---\n", ok(20))
        self.turn("a", e3, 19, 25)  # started before the best was in: not counted
        e4 = self.entry("a", "ARTIFACT: skill\n---\nname: t\n---\n", ok(20))
        self.turn("a", e4, 20, 30, tokens=500)
        e5 = self.entry("b", "ARTIFACT: skill\n---\nname: u\n---\n", ok(20))
        self.turn("b", e5, 21, 31, tokens=300, wrap_up=True)
        found = [f for f in self.findings() if f["code"] == "after_best"]
        self.assertEqual([(f["numbers"]["turns"], f["numbers"]["tokens"], f["numbers"].get("wrap_up")) for f in found],
                         [(1, 500, None), (1, 300, True)])

    def test_budget_left_only_when_the_wakes_ran_out_before_the_turns(self):
        e = self.entry("a", "1", ok(1))
        self.turn("a", e, 0, 1)
        self.summary["stopped"] = "the planner's wakes are used up and no todo is left"
        self.assertEqual(self.codes()["budget_left"]["numbers"]["turns_left"], 9)
        self.summary["turn_budget"] = 1
        self.assertNotIn("budget_left", self.codes())

    def test_waiting_starts_at_a_quarter_of_the_run(self):
        e = self.entry("a", "1", ok(1))
        self.turn("a", e, 0, 1)
        self.summary["idle_seconds"] = {"a": 25.0, "b": 24.9}
        f = self.codes()["waiting"]
        self.assertEqual(f["numbers"], {"a": 0.25})

    def test_sent_back_repairs_and_broken(self):
        e = self.entry("a", "oops", bad("not a number"))
        fixed = self.entry("a", "7", ok(7), parents=[e])
        self.turn("a", fixed, 0, 10, repairs=[{"entry": fixed, "status": "valid", "score": 7, "tokens": 300, "of": e}])
        self.wakes = [{"kind": "wake", "problems": ["add 1: a review for a, who makes what it reviews"]},
                      {"kind": "wake", "problems": []}]
        self.summary["broken"] = ["a turn 1: the judge failed on k1"]
        codes = self.codes()
        self.assertEqual(codes["sent_back"]["numbers"], {"sent_back": 1})
        self.assertEqual({k: codes["repairs"]["numbers"][k] for k in ("turns", "repairs", "helped")}, {"turns": 1, "repairs": 1, "helped": 1})
        self.assertIn("the judge failed on k1", codes["broken"]["title"])

    def test_a_quiet_run_says_nothing_and_an_empty_folder_does_not_break_it(self):
        e = self.entry("a", "1", ok(1))
        self.turn("a", e, 0, 1)
        self.assertEqual(self.findings(), [])
        self.assertEqual(rundiag.findings(tempfile.mkdtemp()), [])


if __name__ == "__main__":
    unittest.main()
