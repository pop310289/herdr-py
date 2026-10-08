"""Team knowledge base (herdr_py/teamkb.py): members propose, only the program judges, nothing is counted twice or lost.
The failure cases come from the 2026-10-08 controller research: a member killed after submitting and retried, a stale
or swapped artifact, a judge that crashes, made-up provenance, members that must not see each other."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from herdr_py.teamkb import TeamKB, TeamKBError, main  # noqa: E402


def score_check(path):
    """A stand-in judge: the artifact is JSON {"score": x}; a negative score is an invalid candidate."""
    with open(path) as handle:
        x = json.load(handle)["score"]
    return ("valid", x, "ok") if x >= 0 else ("invalid", None, "negative")


def candidate(x):
    return json.dumps({"score": x})


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.kb = TeamKB(self.dir)

    def events(self, kind=None):
        path = os.path.join(self.dir, "events.jsonl")
        if not os.path.exists(path):  # nothing written yet
            return []
        with open(path) as handle:
            out = [json.loads(line) for line in handle]
        return [e for e in out if kind is None or e["type"] == kind]


class ProposeTest(Base):
    def test_the_same_entry_sent_twice_is_recorded_once(self):
        a = self.kb.propose("drawA", "result", "grid of 26 circles", candidate(2.5), name="c.json")
        b = self.kb.propose("drawA", "result", "grid of 26 circles", candidate(2.5), name="c.json")  # retried after a crash
        self.assertEqual(a, b)
        self.assertEqual(len(self.events("propose")), 1)
        c = self.kb.propose("drawA", "result", "grid of 26 circles, nudged", candidate(2.5), name="c.json")
        self.assertNotEqual(a, c)

    def test_provenance_cannot_be_made_up_and_bad_input_is_refused(self):
        with self.assertRaisesRegex(TeamKBError, "parents: no entry kdeadbeef"):
            self.kb.propose("drawB", "result", "built on nothing", candidate(1), parents=["kdeadbeef"])
        for kwargs, message in ((dict(member=""), "member"), (dict(kind="guess"), "kind"), (dict(summary=" "), "summary"),
                                (dict(scope="world"), "scope")):
            args = dict(member="m", kind="note", summary="s")
            args.update(kwargs)
            with self.assertRaisesRegex(TeamKBError, message):
                self.kb.propose(**args)
        self.assertEqual(self.events(), [])

    def test_artifacts_are_stored_once_by_content(self):
        path = os.path.join(self.dir, "mine.json")
        with open(path, "w") as handle:
            handle.write(candidate(3))
        a = self.kb.propose("drawA", "result", "from a file", path=path)
        self.kb.propose("drawB", "result", "same bytes, other words", candidate(3), name="x.json")
        c = self.kb.propose("drawC", "result", "same bytes again", candidate(3), name="y.json")
        entries = self.kb.entries()
        self.assertTrue(entries[0]["artifact"].endswith(".json"))
        self.assertEqual(len(os.listdir(os.path.join(self.dir, "artifacts"))), 1)
        self.assertEqual([e["duplicate_of"] for e in entries], [None, a, a])  # the earliest one, not the latest
        self.assertEqual(entries[2]["id"], c)

    def test_text_that_looks_like_a_path_is_kept_as_text(self):
        secret = os.path.join(self.dir, "private.txt")
        with open(secret, "w") as handle:
            handle.write("do not copy me")
        self.kb.propose("drawA", "note", "the model answered with a path", secret)  # a model's output, not a file to read
        stored = os.path.join(self.dir, self.kb.entries()[0]["artifact"])
        with open(stored) as handle:
            self.assertEqual(handle.read(), secret)
        with self.assertRaisesRegex(TeamKBError, "not both"):
            self.kb.propose("drawA", "note", "both", "x", path=secret)

    def test_the_summary_limit_is_inclusive(self):
        self.kb.propose("m", "note", "x" * 4000)
        with self.assertRaisesRegex(TeamKBError, "at most 4000"):
            self.kb.propose("m", "note", "x" * 4001)


class JudgeTest(Base):
    def test_only_the_check_gives_scores_and_claims_are_ignored(self):
        a = self.kb.propose("drawA", "result", "claims a lot", candidate(1.25), name="a.json", claim=99)
        self.kb.propose("drawB", "result", "not judged yet", candidate(5), name="b.json")
        self.assertEqual(self.kb.judge(a, score_check, judge="exact@1"), ("valid", 1.25))
        entry = self.kb.entries()[0]
        self.assertEqual((entry["status"], entry["score"], entry["judge"], entry["claim"]), ("valid", 1.25, "exact@1", 99))
        text = self.kb.brief("drawC")
        self.assertIn("score 1.25", text)
        self.assertNotIn("99", text)
        self.assertNotIn("not judged yet", text)  # unjudged entries are not results

    def test_a_crashing_or_confused_judge_is_infra_error_not_invalid(self):
        a = self.kb.propose("drawA", "result", "fine candidate", candidate(1), name="a.json")
        for check, words in ((lambda p: 1 / 0, "ZeroDivisionError"), (lambda p: "yes", "the check returned"),
                             (lambda p: ("maybe", 1, ""), "the check returned 'maybe'"),
                             (lambda p: ("valid", float("nan"), ""), "finite score"), (lambda p: ("valid", True, ""), "finite score")):
            self.assertEqual(self.kb.judge(a, check), ("infra_error", None))
            self.assertIn(words, self.kb.entries()[0]["detail"])
        self.assertEqual(self.kb.judge(a, lambda p: ("invalid", 3.0, "overlap")), ("invalid", None))  # no score when invalid

    def test_a_swapped_artifact_is_not_judged_as_the_proposed_one(self):
        a = self.kb.propose("drawA", "result", "honest", candidate(1), name="a.json")
        path = os.path.join(self.dir, self.kb.entries()[0]["artifact"])
        with open(path, "w") as handle:
            handle.write(candidate(100))  # an old or someone else's answer slipped in
        self.assertEqual(self.kb.judge(a, score_check), ("infra_error", None))
        self.assertIn("changed since it was proposed", self.kb.entries()[0]["detail"])

    def test_each_entry_has_its_own_verdict(self):
        a = self.kb.propose("drawA", "result", "fine", candidate(1.5), name="a.json")
        b = self.kb.propose("drawB", "result", "negative", candidate(-1), name="b.json")
        c = self.kb.propose("drawC", "result", "not judged", candidate(2), name="c.json")
        self.kb.judge(a, score_check)
        self.kb.judge(b, score_check)
        self.assertEqual([self.kb.verdict(e) for e in (a, b, c)], [("valid", 1.5), ("invalid", None), None])
        self.assertEqual(len(self.kb), 3)

    def test_unknown_entries_are_refused(self):
        with self.assertRaisesRegex(TeamKBError, "no entry"):
            self.kb.judge("knope", score_check)


class TeamworkTest(Base):
    def test_adoption_improvement_and_duplicates_are_counted(self):
        a = self.kb.propose("drawA", "result", "hexagonal start", candidate(2.0), name="a.json")
        self.kb.judge(a, score_check)
        b = self.kb.propose("drawB", "result", "hexagonal start, then local moves", candidate(2.3), name="b.json", parents=[a])
        self.kb.judge(b, score_check)
        c = self.kb.propose("drawC", "result", "my own idea", candidate(2.0), name="c.json")  # same bytes as a
        self.kb.judge(c, score_check)
        f = self.kb.propose("drawC", "failure", "random restarts got stuck below 1.8")
        entries = {e["id"]: e for e in self.kb.entries()}
        self.assertEqual(entries[a]["adopted_by"], [b])
        self.assertEqual(entries[c]["duplicate_of"], a)
        s = self.kb.stats()
        self.assertEqual((s["entries"], s["valid"], s["unjudged"], s["duplicates"]), (4, 3, 1, 1))
        self.assertEqual((s["adoption_rate"], s["improved_after_adoption"], s["duplicate_rate"]), (0.3333, 1.0, 0.25))
        self.assertEqual((s["best"], s["best_by_member"]), (2.3, {"drawA": 2.0, "drawB": 2.3, "drawC": 2.0}))
        text = self.kb.brief("drawA", round=2)
        self.assertLess(text.index(b), text.index(a))  # best first
        self.assertIn("random restarts got stuck", text)
        self.assertEqual(self.events("read")[-1], dict(self.events("read")[-1], member="drawA", round=2))
        self.assertEqual({e["id"]: e["shown"] for e in self.kb.entries()}[f], 1)

    def test_building_on_your_own_entry_is_not_adoption(self):
        a = self.kb.propose("drawA", "result", "start", candidate(2.0), name="a.json")
        self.kb.judge(a, score_check)
        b = self.kb.propose("drawA", "result", "my own next step", candidate(2.2), name="b.json", parents=[a])
        self.kb.judge(b, score_check)
        s = self.kb.stats()
        self.assertEqual((s["adoption_rate"], s["improved_after_adoption"]), (0.0, None))

    def test_improvement_needs_a_higher_score_than_a_scored_parent(self):
        a = self.kb.propose("drawA", "result", "start", candidate(2.0), name="a.json")
        self.kb.judge(a, score_check)
        m = self.kb.propose("drawA", "method", "an idea with nothing to score")
        same = self.kb.propose("drawB", "result", "tied with its parent", candidate(2.0), name="b.json", parents=[a])
        self.kb.judge(same, score_check)
        idea = self.kb.propose("drawC", "result", "built on an unscored idea", candidate(1.0), name="c.json", parents=[m])
        self.kb.judge(idea, score_check)
        s = self.kb.stats()
        self.assertEqual(s["improved_after_adoption"], 0.0)  # a tie is not better; nothing to beat is not better
        better = self.kb.propose("drawC", "result", "beats it", candidate(2.01), name="d.json", parents=[a, m])
        self.kb.judge(better, score_check)
        self.assertEqual(self.kb.stats()["improved_after_adoption"], 0.3333)

    def test_a_brief_shows_each_failure_once_and_at_most_the_limit(self):
        for member in ("drawA", "drawB"):
            self.kb.propose(member, "failure", "random restarts got stuck")
        self.kb.propose("drawC", "failure", "overlap at the corners")
        text = self.kb.brief("drawD", failures=3)
        self.assertEqual(text.count("random restarts got stuck"), 1)
        self.assertIn("overlap at the corners", text)
        text = self.kb.brief("drawD", failures=1)
        self.assertIn("overlap at the corners", text)  # the latest one
        self.assertNotIn("random restarts", text)

    def test_a_brief_can_show_the_answers_themselves(self):
        ids = {}
        for member, data, name in (("drawA", candidate(3), "a.json"), ("drawB", b"\xff\xfe" + b"x" * 10, "b.bin"),
                                   ("drawC", "```\n" + candidate(2) + "\n```", "c.md"), ("drawD", "y" * 50, "d.txt")):
            ids[member] = self.kb.propose(member, "result", f"from {member}", data, name=name)
            self.kb.judge(ids[member], lambda path: ("valid", {"drawA": 4, "drawB": 3, "drawC": 2, "drawD": 1}[member], ""))
        text, shown = self.kb.brief("drawE", results=4, answer_bytes=40, with_ids=True)
        self.assertEqual(shown, [ids["drawA"], ids["drawB"], ids["drawC"], ids["drawD"]])
        self.assertIn("```\n" + candidate(3) + "\n```", text)
        self.assertIn("(its answer is binary, 12 bytes)", text)
        self.assertIn("~~~~\n```\n" + candidate(2) + "\n```\n~~~~", text)  # a fence inside gets a longer one around it
        self.assertIn("(its answer is longer than 40 bytes, so it is not shown here)", text)
        self.assertNotIn("```\n" + candidate(3), self.kb.brief("drawE", record=False))  # answers only when asked

    def test_a_person_looking_is_not_recorded_as_a_brief(self):
        self.kb.brief("drawA", record=False)
        self.assertEqual(self.events("read"), [])
        self.kb.brief("drawA")
        self.assertEqual(len(self.events("read")), 1)

    def test_private_entries_stay_with_their_author(self):
        a = self.kb.propose("indA", "result", "A's secret", candidate(2), name="a.json", scope="private")
        self.kb.judge(a, score_check)
        self.assertIn("A's secret", self.kb.brief("indA"))
        self.assertNotIn("A's secret", self.kb.brief("indB"))
        self.assertEqual(self.kb.brief("indB"), "Nothing verified or failed yet.")


class DurabilityTest(Base):
    def test_another_reader_sees_the_same_state(self):
        a = self.kb.propose("drawA", "result", "one", candidate(1), name="a.json")
        self.kb.judge(a, score_check)
        again = TeamKB(self.dir)
        self.assertEqual(again.entries(), self.kb.entries())
        self.assertEqual(again.stats(), self.kb.stats())

    def test_threads_writing_at_once_lose_nothing(self):
        def work(n):
            for i in range(20):
                eid = self.kb.propose(f"m{n}", "result", f"try {i}", candidate(n + i / 100), name="c.json")
                self.kb.judge(eid, score_check)
        threads = [threading.Thread(target=work, args=(n,)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual((len(self.events("propose")), len(self.events("verdict"))), (120, 120))
        self.assertEqual(TeamKB(self.dir).stats()["valid"], 120)

    def test_processes_writing_at_once_lose_nothing_and_retries_count_once(self):
        code = ("import sys, json; sys.path.insert(0, %r); from herdr_py.teamkb import TeamKB\n"
                "kb = TeamKB(%r)\n"
                "for i in range(15):\n"
                "    kb.propose('p' + sys.argv[1], 'result', 'try %%d' %% i, json.dumps({'score': i}), name='c.json')\n"
                "    kb.propose('p' + sys.argv[1], 'result', 'try %%d' %% i, json.dumps({'score': i}), name='c.json')\n") % (ROOT, self.dir)
        procs = [subprocess.Popen([sys.executable, "-B", "-c", code, str(n)]) for n in range(4)]
        self.assertEqual([p.wait(60) for p in procs], [0, 0, 0, 0])
        self.assertEqual(len(self.events("propose")), 60)
        self.assertEqual(TeamKB(self.dir).stats()["bad_lines"], 0)

    def test_a_writer_waits_for_the_lock(self):
        import fcntl
        code = ("import sys; sys.path.insert(0, %r); from herdr_py.teamkb import TeamKB\n"
                "kb = TeamKB(%r); print('ready', flush=True)\n"
                "kb.propose('late', 'note', 'waited'); print('done', flush=True)\n") % (ROOT, self.dir)
        with open(os.path.join(self.dir, ".lock"), "a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            proc = subprocess.Popen([sys.executable, "-B", "-c", code], stdout=subprocess.PIPE, universal_newlines=True)
            self.addCleanup(proc.stdout.close)
            self.addCleanup(proc.wait)
            self.assertEqual(proc.stdout.readline().strip(), "ready")
            with self.assertRaises(subprocess.TimeoutExpired):
                proc.wait(0.8)  # still waiting for the lock
            self.assertEqual(self.events(), [])
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        self.assertEqual(proc.stdout.readline().strip(), "done")
        self.assertEqual(proc.wait(30), 0)
        self.assertEqual([e["summary"] for e in self.events("propose")], ["waited"])

    def test_a_member_killed_after_submitting_and_retried_counts_once(self):
        code = ("import os, signal, sys; sys.path.insert(0, %r); from herdr_py.teamkb import TeamKB\n"
                "TeamKB(%r).propose('drawA', 'result', 'answer', '{\"score\": 2}', name='a.json')\n"
                "os.kill(os.getpid(), signal.SIGKILL)\n") % (ROOT, self.dir)
        proc = subprocess.run([sys.executable, "-B", "-c", code])
        self.assertLess(proc.returncode, 0)  # killed before it could report back
        again = self.kb.propose("drawA", "result", "answer", '{"score": 2}', name="a.json")  # the retry
        self.assertEqual([e["id"] for e in self.events("propose")], [again])

    def test_a_line_still_being_written_is_left_for_later(self):
        self.kb.propose("drawA", "note", "complete")
        with open(os.path.join(self.dir, "events.jsonl"), "a") as handle:
            handle.write('{"type": "propose", "id": "khalf", "member"')  # another writer is mid-line
        reader = TeamKB(self.dir)
        self.assertEqual([e["summary"] for e in reader.entries()], ["complete"])
        self.assertEqual(reader.bad_lines, 0)


class CommandTest(Base):
    def run_main(self, *args):
        import io
        from contextlib import redirect_stdout, redirect_stderr
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_report_json_and_brief(self):
        a = self.kb.propose("drawA", "result", "hexagonal start", candidate(2.0), name="a.json")
        self.kb.judge(a, score_check)
        b = self.kb.propose("drawB", "result", "built on it", candidate(2.5), name="b.json", parents=[a])
        self.kb.judge(b, score_check)
        code, out, _ = self.run_main(self.dir)
        self.assertEqual(code, 0)
        self.assertIn("2 entries: 2 valid", out)
        self.assertIn(f"{a}  drawA      result  score 2; used by {b}: hexagonal start", out)
        code, out, _ = self.run_main(self.dir, "--json")
        self.assertEqual(json.loads(out)["stats"]["best"], 2.5)
        code, out, _ = self.run_main(self.dir, "--brief", "drawC")
        self.assertIn(f"- {b} by drawB: score 2.5: built on it", out)
        self.assertEqual(self.events("read"), [])
        code, _, err = self.run_main(os.path.join(self.dir, "missing"))
        self.assertEqual(code, 2)
        self.assertIn("no knowledge base", err)


if __name__ == "__main__":
    unittest.main()
