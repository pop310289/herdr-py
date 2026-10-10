"""A notebook of pages (herdr_py/notebook.py): a draft never runs, an approval covers the definition and the task, a run
goes on from the last one with what passed and the notes nobody used yet and without what a person excluded, decisions
are only appended, and what waits for a person is worked out from the records. The teams are the stand-in planner,
members and judge of test_engine.py, run by the real engine in its own process."""
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from herdr_py import engine, notebook  # noqa: E402
from herdr_py.notebook import Notebook, NotebookError  # noqa: E402
from test_engine import JUDGE, MEMBER, PLANNER, add  # noqa: E402


# A judge for pages: an answer with a paragraph in it is valid with score 100, a skill 10; otherwise as JUDGE (a number is its score).
PAGE_JUDGE = r'''
import json, sys
text = open(sys.argv[-1]).read().strip()
if "<p>" in text:
    print(json.dumps({"status": "valid", "score": 100, "detail": "a page"}))
elif text.startswith("ARTIFACT: skill"):
    print(json.dumps({"status": "valid", "score": 10, "detail": "a skill"}))
else:
    try:
        print(json.dumps({"status": "valid", "score": float(text), "detail": "a number"}))
    except ValueError:
        print(json.dumps({"status": "invalid", "score": 0, "detail": "neither"}))
'''


def read(path, mode="r"):
    with open(path, mode, **({} if "b" in mode else {"encoding": "utf-8"})) as handle:
        return handle.read()


def first_line(path):
    with open(path, encoding="utf-8") as handle:
        return json.loads(handle.readline())


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        for name, body in (("planner.py", PLANNER), ("member.py", MEMBER), ("judge.py", JUDGE), ("page_judge.py", PAGE_JUDGE)):
            with open(os.path.join(self.dir, name), "w") as handle:
                handle.write(textwrap.dedent(body))
        self.write("task.md", "Give a number as high as you can.\n")
        self.log = os.path.join(self.dir, "log")
        saved = {k: os.environ.get(k) for k in ("ENGINE_TEST_LOG", "ENGINE_TEST_PLANNER", "ENGINE_TEST_MEMBERS")}
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v) for k, v in saved.items()])
        os.environ.update(ENGINE_TEST_LOG=self.log, ENGINE_TEST_PLANNER=os.path.join(self.dir, "planner.json"),
                          ENGINE_TEST_MEMBERS=os.path.join(self.dir, "members.json"))
        self.folder = os.path.join(self.dir, "notebook")
        os.makedirs(self.folder)
        with open(os.path.join(self.folder, "notebook.json"), "w") as handle:
            json.dump({"title": "Test notebook", "lang": "en"}, handle)
        self.nb = Notebook(self.folder)

    def write(self, name, text):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as handle:
            handle.write(text)

    def script(self, planner, members):
        with open(os.path.join(self.dir, "planner.json"), "w") as handle:
            json.dump(planner, handle)
        with open(os.path.join(self.dir, "members.json"), "w") as handle:
            json.dump(members, handle)
        if os.path.isdir(self.log):
            shutil.rmtree(self.log)

    def definition(self, **over):
        d = {"id": "p1", "title": "A page", "day": "2026-10-10", "goal": "the highest number", "cwd": self.dir,
             "task": "task.md", "judge": [sys.executable, "-B", "judge.py"],
             "team": {"planner": f"plan=command:{sys.executable} -B planner.py",
                      "members": [f"a=command:{sys.executable} -B member.py"]},
             "budget": {"turns": 1, "planner_wakes": 2}}
        d.update(over)
        return d

    def draft(self, request=None, **over):
        path = os.path.join(self.dir, "draft.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.definition(**over), handle, ensure_ascii=False)
        return notebook.draft(self.nb, path, "claude", request=request)

    def page(self, pid="p1"):
        return self.nb.page(pid)  # read again: what another process wrote is seen

    def run_page(self, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = notebook.start_run(self.page(), "tester", out=out, **kw)
        return code, out.getvalue()

    def engine_run(self, name, answers=("5",)):
        """A run made outside the notebook (as engine.main would make it), to attach."""
        self.script([add(*[{"text": f"answer {i}", "for": "a", "parents": []} for i in range(len(answers))])]
                    + [add() for _ in answers] + [add(done=True)],  # done only when every answer is in
                    {"a": [{"answer": x, "summary": f"answer {i}"} for i, x in enumerate(answers)]})
        out = os.path.join(self.dir, name)
        argv = ["--task", os.path.join(self.dir, "task.md"), "--judge", f"{sys.executable} -B page_judge.py",
                "--planner", f"plan=command:{sys.executable} -B planner.py", "--member", f"a=command:{sys.executable} -B member.py",
                "--turns", str(len(answers)), "--max-open", str(max(2, len(answers))), "--out", out]
        cwd = os.getcwd()
        os.chdir(self.dir)
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                engine.main(argv)
        finally:
            os.chdir(cwd)
        return out

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = notebook.main([self.folder] + list(args))
        return code, out.getvalue(), err.getvalue()


class PageTest(Base):
    def test_a_draft_never_runs_and_an_approval_covers_the_definition_and_the_task(self):
        page = self.draft()
        self.assertEqual(page.state(), ("draft", [("approve", None)]))
        with self.assertRaisesRegex(NotebookError, "draft"):
            self.run_page()
        notebook.approve(self.page(), "person")
        self.assertEqual(self.page().state(), ("approved", []))
        with self.assertRaisesRegex(NotebookError, "already approved"):
            notebook.approve(self.page(), "person")
        self.write("task.md", "Give a number, the higher the better.\n")  # the task the team reads changed
        self.assertIsNone(self.page().approval())
        with self.assertRaisesRegex(NotebookError, "draft"):
            self.run_page(dry=True)
        notebook.approve(self.page(), "person")
        with open(os.path.join(self.dir, "judge.py"), "a") as handle:  # the judge's program changed: what passes may change
            handle.write("\n# stricter now\n")
        self.assertIsNone(self.page().approval())
        with self.assertRaisesRegex(NotebookError, "draft"):
            self.run_page(dry=True)
        notebook.approve(self.page(), "person")
        self.draft(budget={"turns": 2, "planner_wakes": 2})  # a new version of the definition
        self.assertEqual(self.page().state()[0], "draft")
        with self.assertRaisesRegex(NotebookError, "already the page's"):
            self.draft(budget={"turns": 2, "planner_wakes": 2})

    def test_a_run_goes_on_from_the_last_with_unused_notes_and_without_excluded_entries(self):
        self.draft()
        notebook.approve(self.page(), "person")
        self.script([add("give a number"), add(done=True)], {"a": [{"answer": "5"}]})
        code, _ = self.run_page()
        self.assertEqual(code, 0)
        page = self.page()
        first = page.facts(1)
        self.assertEqual((first["state"], first["valid"], len(first["carried"])), ("ended", 1, 0))
        good = first["made"][0]["id"]
        self.assertEqual(page.state(), ("review", [("review", 1)]))
        notebook.add_note(page, "aim higher than 5", "person")
        self.assertEqual(self.page().state(), ("approved", []))  # a person looked at it
        self.assertEqual([n["used_by"] for n in self.page().notes()], [None])

        self.script([add({"text": "beat $best", "for": "a", "parents": ["$best"]}), add(done=True)], {"a": [{"answer": "7"}]})
        code, _ = self.run_page()
        self.assertEqual(code, 0)
        page = self.page()
        run2 = page.runs()[-1]
        self.assertEqual((run2["n"], run2["from"], run2["notes"]), (2, 1, ["n1"]))
        with open(os.path.join(run2["folder"], "task.md"), encoding="utf-8") as handle:
            task = handle.read()
        self.assertIn("Give a number as high as you can.", task)
        self.assertIn("aim higher than 5", task)  # the note went into this run's task
        start = first_line(os.path.join(run2["folder"], "engine.jsonl"))
        self.assertEqual(start["seeded"]["carried"], [good])
        second = page.facts(2)
        self.assertEqual(([e["id"] for e in second["carried"]], second["used"]), ([good], [good]))
        self.assertEqual([n["used_by"] for n in page.notes()], [2])  # used once, never again

        code, out, err = self.cli("exclude", "p1", good, "--why", "wrong", "--by", "person")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.cli("exclude", "p1", good)[0], 2)  # already excluded
        self.assertEqual(self.cli("include", "p1", good)[0], 0)  # a person changed their mind
        self.assertNotIn("--seed-skip", self.run_page(dry=True)[1])
        self.assertEqual(self.cli("include", "p1", good)[0], 2)  # not excluded any more
        self.assertEqual(self.cli("exclude", "p1", good, "--why", "wrong after all")[0], 0)
        _, dry = self.run_page(dry=True)
        self.assertIn(f"--seed-skip {good}", dry)
        self.assertIn("going on from run 2", dry)
        self.assertNotIn("aim higher", dry)  # a used note is not added again
        self.script([add("give a number"), add(done=True)], {"a": [{"answer": "3"}]})
        self.assertEqual(self.run_page()[0], 0)
        start = first_line(os.path.join(self.page().runs()[-1]["folder"], "engine.jsonl"))
        self.assertNotIn(good, start["seeded"]["carried"])
        self.assertEqual(start["seeded"]["left_out"].get("excluded"), 1)
        self.assertEqual(len(start["seeded"]["carried"]), 1)  # run 2's own 7 came along

    def test_a_revision_after_a_run_needs_approval_and_a_run_after_a_hold_opens_the_page(self):
        self.draft()
        notebook.approve(self.page(), "person")
        self.script([add("give a number"), add(done=True)], {"a": [{"answer": "5"}]})
        self.run_page()
        self.cli("hold", "p1", "--why", "later")
        self.assertEqual(self.page().state(), ("hold", []))
        self.script([add("give a number"), add(done=True)], {"a": [{"answer": "6"}]})
        self.run_page()
        self.assertEqual(self.page().state(), ("review", [("review", 2)]))  # the run after the hold opened it again
        self.cli("accept", "p1")
        self.assertEqual(self.page().state(), ("approved", []))
        self.draft(goal="the highest number, and say why")
        self.assertEqual(self.page().state(), ("draft", [("approve", None)]))  # the next run needs the new version approved

    def test_the_current_version_is_where_the_next_run_starts(self):
        self.draft(carry=["skill"])  # numbers do not carry on by kind: only a pick brings one along
        notebook.approve(self.page(), "person")
        self.script([add("give a number"), add(done=True)], {"a": [{"answer": "5"}]})
        self.run_page()
        five = self.page().facts(1)["made"][0]["id"]
        self.script([add("give a number"), add(done=True)], {"a": [{"answer": "7"}]})
        self.run_page()
        notebook.pick(self.page(), five, "person")  # the person prefers run 1's answer
        _, dry = self.run_page(dry=True)
        self.assertIn(f"--seed-keep {five}", dry)
        self.assertIn(f"--seed-entry {self.page().runs()[0]['folder']} {five}", dry)  # made in run 1; run 3 goes on from run 2
        self.assertIn("The current versions", dry)
        self.assertIn(f"result: {five} (run 1)", dry)
        self.script([add("give a number"), add(done=True)], {"a": [{"answer": "9"}]})
        self.run_page()
        run3 = self.page().runs()[-1]
        self.assertEqual(first_line(os.path.join(run3["folder"], "engine.jsonl"))["seeded"]["picked"], [five])
        self.assertEqual(run3["picks"], [five])
        self.assertIn(five, [e["id"] for e in self.page().facts(3)["carried"]])
        self.cli("exclude", "p1", five, "--why", "not this one after all")
        self.assertNotIn("--seed-keep", self.run_page(dry=True)[1])  # an excluded pick stays out

    def test_a_new_task_brings_reference_material_but_not_as_its_results(self):
        self.draft()
        skill = "ARTIFACT: skill\n---\nname: count-up\n---\n1. one"
        notebook.attach(self.page(), self.engine_run("outside", answers=(skill, "5", "8")), "claude")
        made = {e["kind"]: e["id"] for e in self.page().facts(1)["made"][:2]}
        eight = self.page().facts(1)["made"][2]["id"]  # a result nobody picked: not a skill, not current, so not brought
        notebook.pick(self.page(), made["result"], "person")
        self.draft(id="p2", title="Second", **{"from": [{"page": "p1", "kinds": ["skill"], "current": True}]})
        notebook.approve(self.page("p2"), "person")
        self.script([add("give a number"), add(done=True)], {"a": [{"answer": "3"}]})
        out = io.StringIO()
        notebook.start_run(self.page("p2"), "tester", out=out)
        run = self.page("p2").runs()[-1]
        board = os.path.join(run["folder"], "board")
        for eid in (made["skill"], made["result"]):  # copied as files the members can read, as they were
            self.assertTrue(os.path.isfile(os.path.join(board, "reference", "p1", eid + ".txt")), eid)
        stored = next(e for e in self.page().facts(1)["made"] if e["id"] == made["skill"])["artifact"]
        self.assertEqual(read(os.path.join(board, "reference", "p1", made["skill"] + ".txt")),
                         read(os.path.join(self.page().runs()[0]["folder"], "kb", stored)))  # byte for byte what was judged
        index = read(os.path.join(board, "reference", "INDEX.md"))
        self.assertIn("Not this task's verified results", index)
        self.assertIn(f"reference/p1/{made['skill']}.txt", index)
        task = read(os.path.join(run["folder"], "task.md"))
        self.assertIn("Reference material from other tasks", task)
        self.assertIn(f"reference/p1/{made['result']}.txt", task)
        self.assertEqual(run["references"], [{"page": "p1", "run": 1, "entries": [made["skill"], made["result"]]}])
        self.assertFalse(os.path.exists(os.path.join(board, "reference", "p1", eight + ".txt")))
        facts = self.page("p2").facts(1)
        self.assertEqual((facts["carried"], facts["seeded"]), ([], None))  # not in its knowledge base: not its results
        self.assertIn(f"reference/p1/{made['skill']}.txt", read(os.path.join(self.log, "a-01.prompt")))  # the member is told
        self.cli("exclude", "p1", made["skill"], "--why", "wrong")
        dry = self.dry("p2")
        self.assertNotIn(made["skill"], dry)  # what its own page excluded is not brought
        self.assertIn(made["result"], dry)

    def test_what_a_request_asked_to_bring_becomes_the_pages_from(self):
        self.draft()
        self.draft(id="p2", title="Second")
        self.nb.ask("a third report", "person", bring=[{"page": "p1", "bring": ["skills", "current"]}, {"page": "p2", "bring": ["knowledge"]}])
        page = self.draft(id="p3", title="Third", request="r1")  # Claude wrote no "from": the request's choices are it
        self.assertEqual(page.d["from"], [{"page": "p1", "kinds": ["skill"], "current": True}, {"page": "p2", "kinds": ["*"]}])
        self.assertEqual(json.loads(read(os.path.join(page.dir, "page.json")))["from"], page.d["from"])  # what is approved
        self.nb.ask("a fourth", "person", bring=[{"page": "p1", "bring": ["skills"]}, {"page": "p2", "bring": ["current"]}])
        with self.assertRaisesRegex(NotebookError, r"asked to bring from p2 \(current\)"):  # a "from" that drops one is refused
            self.draft(id="p4", title="Fourth", request="r2", **{"from": [{"page": "p1", "kinds": ["skill"]}]})
        page = self.draft(id="p4", title="Fourth", request="r2", **{"from": [{"page": "p1", "entries": ["k1"]}, {"page": "p2", "current": True}]})
        self.assertEqual(page.d["from"], [{"page": "p1", "entries": ["k1"]}, {"page": "p2", "current": True}])  # written out: kept

    def test_every_verified_entry_of_a_page_can_be_brought(self):
        self.draft()
        notebook.attach(self.page(), self.engine_run("outside", answers=("ARTIFACT: skill\n---\nname: count-up\n---\n1. one", "5", "oops")), "claude")
        made = self.page().facts(1)["made"]
        self.assertEqual([e["status"] for e in made], ["valid", "valid", "invalid"])
        self.draft(id="p2", title="Second", **{"from": [{"page": "p1", "kinds": ["*"]}]})
        notebook.approve(self.page("p2"), "person")
        dry = self.dry("p2")
        for e in made[:2]:
            self.assertIn(f"reference/p1/{e['id']}.txt", dry)
        self.assertNotIn(made[2]["id"], dry)  # what failed its judge is not brought

    def dry(self, pid):
        out = io.StringIO()
        notebook.start_run(self.page(pid), "tester", dry=True, out=out)
        return out.getvalue()

    def test_what_a_page_brings_is_checked(self):
        d = self.definition
        for refs, problem in (([{"page": "p1", "kinds": ["skill"]}], "own runs carry on"), ([{"page": "p0"}], "say what to bring"),
                              ([{"page": "Bad Id", "current": True}], "page is the id"), ([{"page": "p0", "run": 0, "current": True}], "run is"),
                              ("p0", "from: a list")):
            got = notebook.check_definition(d(**{"from": refs}))
            self.assertTrue(any(problem in p for p in got), (refs, got))
        self.draft(**{"from": [{"page": "nowhere", "kinds": ["skill"]}]})
        notebook.approve(self.page(), "person")
        with self.assertRaisesRegex(NotebookError, "no page 'nowhere'"):
            self.dry("p1")

    def test_fresh_and_from_choose_where_a_run_starts(self):
        self.draft()
        notebook.approve(self.page(), "person")
        self.script([add("give a number"), add(done=True)], {"a": [{"answer": "5"}]})
        self.run_page()
        self.assertIn("from nothing", self.run_page(dry=True, fresh=True)[1])
        self.assertNotIn("--seed-from", self.run_page(dry=True, fresh=True)[1])
        self.assertIn("going on from run 1", self.run_page(dry=True, source_n=1)[1])
        with self.assertRaisesRegex(NotebookError, "no run 4"):
            self.run_page(dry=True, source_n=4)
        with self.assertRaisesRegex(NotebookError, "one or the other"):
            self.run_page(dry=True, source_n=1, fresh=True)

    def test_the_budget_and_the_judges_folders_reach_the_engine(self):
        self.draft(budget={"turns": 3, "planner_wakes": 4, "time_limit": 30, "max_open": 2, "turn_timeout": 60},
                   judge=[sys.executable, "judge.py", "--kb", "{kb}", "--run", "{run}"], carry=["skill"],
                   show={"answer_bytes": 100}, socket="~/x.sock", team={
                       "planner": "plan=claude", "members": ["a=claude", "b=codex:gpt-5"], "about": {"a": "looks things up"},
                       "access": "research"})
        notebook.approve(self.page(), "person")
        _, dry = self.run_page(dry=True)
        folder = os.path.join(self.folder, "pages", "p1", "runs", "1")
        for part in ("--turns 3", "--planner-wakes 4", "--time-limit 30 ", "--max-open 2", "--turn-timeout 60",
                     "--member a=claude", "--member b=codex:gpt-5", "--about 'a=looks things up'", "--member-access research",
                     "--answer-bytes 100", f"--socket {os.path.expanduser('~/x.sock')}", os.path.join(folder, "kb"),
                     f"--run {folder}", f"--out {folder}"):
            self.assertIn(part, dry)
        self.assertNotIn("--seed-from", dry)  # a first run starts from nothing
        self.assertNotIn("--carry", dry)
        self.assertFalse(os.path.exists(folder))  # a dry run makes nothing
        earlier = self.engine_run("outside")
        notebook.attach(self.page(), earlier, "claude")
        _, dry = self.run_page(dry=True)
        self.assertIn(f"--seed-from {earlier} --carry skill", dry)  # the next run carries only what the page carries

    def test_definitions_are_checked(self):
        d = self.definition
        cases = [(d(id="Bad Id"), "id:"), (d(day="10/10"), "day:"), (d(budget={"planner_wakes": 2}), "budget.turns"),
                 (d(budget={"turns": 0}), "budget.turns: a positive"), (d(budget={"turns": 1.5}), "whole"),
                 (d(budget={"turns": 1, "cost": 3}), "budget.cost"), (d(judge="python3 judge.py"), "judge:"),
                 (d(team={"planner": "a=claude", "members": ["a=claude"]}), "also a member"),
                 (d(team={"planner": "p=claude", "members": ["a=claude", "a=codex"]}), "two members"),
                 (d(team={"planner": "p=claude", "members": ["a=claude"], "about": {"z": "x"}}), "no member called z"),
                 (d(team={"planner": "p=nobody", "members": ["a=claude"]}), "team:"), (d(kind="cron"), "kind:"),
                 (d(carry="skill"), "carry:"), (d(links=[{"label": "x"}]), "links:"), (d(title=" "), "title:")]
        for definition, problem in cases:
            got = notebook.check_definition(definition)
            self.assertTrue(any(problem in p for p in got), (problem, got))
        self.assertEqual(notebook.check_definition(d()), [])
        self.assertEqual(notebook.check_definition({"id": "x", "title": "t", "goal": "g", "day": "2026-10-10", "kind": "dag"}), [])


class RecordsTest(Base):
    def test_attached_runs_picks_holds_and_what_waits(self):
        page = self.draft()
        folder = self.engine_run("outside", answers=("5", "oops"))
        e = notebook.attach(page, folder, "claude")
        self.assertTrue(e["imported"])  # recorded later than it happened
        with self.assertRaisesRegex(NotebookError, "already a run"):
            notebook.attach(self.page(), folder, "claude")
        with self.assertRaisesRegex(NotebookError, "not a run folder"):
            notebook.attach(self.page(), self.dir, "claude")
        page = self.page()
        self.assertEqual(page.state(), ("review", [("review", 1)]))  # ran before any approval: nothing to approve now
        made = page.facts(1)["made"]
        good = next(x["id"] for x in made if x["status"] == "valid")
        bad = next(x["id"] for x in made if x["status"] == "invalid")
        with self.assertRaisesRegex(NotebookError, "did not pass"):
            notebook.pick(page, bad, "person")
        with self.assertRaisesRegex(NotebookError, "no entry"):
            notebook.pick(page, "k000000000000", "person")
        notebook.pick(page, good, "person")
        other = self.engine_run("outside2", answers=("9",))
        notebook.attach(self.page(), other, "claude", source_n=1)
        later = self.page().facts(2)["made"][0]["id"]
        notebook.pick(self.page(), later, "person")  # the latest pick of a kind is the current one
        self.assertEqual(self.page().picks()["result"]["entry"], later)
        notebook.pick(self.page(), good, "person")
        page = self.page()
        self.assertEqual(page.picks()["result"]["entry"], good)
        self.assertEqual(page.state(), ("reviewed", []))
        self.cli("hold", "p1", "--why", "not now")
        self.assertEqual(self.page().state(), ("hold", []))
        self.cli("reopen", "p1")
        self.assertEqual(self.page().state(), ("reviewed", []))
        notebook.approve(self.page(), "person")
        self.assertEqual(self.page().state(), ("approved", []))

    def test_a_run_with_no_end_is_running_then_stuck(self):
        self.draft()
        folder = os.path.join(self.dir, "quiet")
        os.makedirs(folder)
        with open(os.path.join(folder, "engine.jsonl"), "w") as handle:
            handle.write(json.dumps({"t": time.time(), "kind": "start", "members": ["a"], "planner": "plan", "turns": 2}) + "\n")
        notebook.attach(self.page(), folder, "claude")
        self.assertEqual(self.page().state(), ("running", []))
        with self.assertRaisesRegex(NotebookError, "still going"):
            notebook.approve(self.page(), "person")
            self.run_page(dry=True)
        old = time.time() - notebook.QUIET - 5
        os.utime(os.path.join(folder, "engine.jsonl"), (old, old))
        state, needs = self.page().state()
        self.assertEqual((state, needs), ("stuck", [("stuck", 1)]))
        self.cli("hold", "p1", "--why", "it died")
        self.assertEqual(self.page().state(), ("hold", []))
        # the boundary: a minute short of quiet is still running
        recent = time.time() - notebook.QUIET + 60
        os.utime(os.path.join(folder, "engine.jsonl"), (recent, recent))
        self.assertEqual(self.page().state()[0], "running")

    def test_the_history_is_only_appended_and_imported_records_say_so(self):
        page = self.draft()
        path = os.path.join(page.dir, "history.jsonl")
        before = read(path, "rb")
        self.assertEqual(self.cli("note", "p1", "first", "--by", "person")[0], 0)
        self.assertEqual(self.cli("hold", "p1", "--by", "person", "--at", "2020-01-02 03:04:05", "--why", "later")[0], 0)
        after = read(path, "rb")
        self.assertTrue(after.startswith(before))
        rows = [json.loads(x) for x in after.decode().splitlines()]
        self.assertEqual([r["kind"] for r in rows], ["create", "note", "hold"])
        self.assertTrue(rows[2]["imported"] and rows[2]["recorded"] > rows[2]["t"])
        self.assertEqual(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(rows[2]["t"])), "2020-01-02 03:04:05")
        self.assertNotIn("imported", rows[1])
        self.assertEqual(self.cli("hold", "p1", "--at", "yesterday")[0], 2)
        self.assertEqual(self.cli("note", "p1")[0], 2)
        self.assertEqual(self.cli("note", "nope", "x")[0], 2)
        self.assertEqual(read(path, "rb"), after)  # refused commands write nothing


class ViewTest(Base):
    TABS = ("overview", "arch", "replay", "debug", "skills", "kb", "outputs", "notes")

    def test_the_view_shows_what_waits_every_version_and_is_complete_without_javascript(self):
        self.draft(title="Plans <script>alert(1)</script>", outputs=["page"])
        folder = self.engine_run("outside", answers=("ARTIFACT: page\n<!doctype html><p>v1</p>",))
        notebook.attach(self.page(), folder, "claude")
        self.engine_run("loose", answers=("4",))  # a run no page holds
        out = os.path.join(self.dir, "site")
        code, printed, err = self.cli("view", "--out", out, "--runs", self.dir)
        self.assertEqual(code, 0, err)
        home = read(os.path.join(out, "index.html"))
        self.assertNotIn("<script>alert", home)
        self.assertIn("Plans &lt;script&gt;", home)
        self.assertIn("review run 1", home)
        self.assertIn("1 of 2 team runs", home)
        self.assertIn("loose", home)
        page_html = read(os.path.join(out, "p", "p1", "index.html"))
        self.assertIn("no current version picked", page_html)
        for tab in self.TABS:  # every tab is a section of the page: without JavaScript they all show, one after another
            self.assertIn(f'<section class="panel tab" id="{tab}">', page_html)
            self.assertIn(f'href="#{tab}"', page_html)
        self.assertIn(".js .tab { display:none; }", page_html)  # only a page that runs its script hides a tab
        self.assertIn('<iframe class="replay" src="runs/1.html"', page_html)
        self.assertTrue(os.path.isfile(os.path.join(out, "p", "p1", "runs", "1.html")))
        made = self.page().facts(1)["made"][0]["id"]
        with open(os.path.join(out, "p", "p1", "files", made + ".html"), encoding="utf-8") as handle:
            self.assertEqual(handle.read().strip(), "<!doctype html><p>v1</p>")  # the kind line is taken off
        notebook.pick(self.page(), made, "person")
        self.cli("view", "--out", out)
        page_html = read(os.path.join(out, "p", "p1", "index.html"))
        self.assertIn('class="cur"', page_html)
        self.assertIn("picked by person", page_html)
        home = read(os.path.join(out, "index.html"))
        self.assertIn("Nothing waits for you.", home)

    def test_the_rail_lists_every_task_and_the_plus_opens_a_request(self):
        self.draft(title="26 circles")
        self.draft(id="p2", title="Weekly report", icon="R")
        self.assertEqual(self.cli("request", "a reading list for a rainy weekend", "--title", "Reading", "--by", "person")[0], 0)
        out = os.path.join(self.dir, "site")
        self.cli("view", "--out", out)
        home = read(os.path.join(out, "index.html"))
        self.assertIn('href="p/p1/"', home)
        self.assertIn('href="p/p2/"', home)
        self.assertIn('<span class="av">C<i class="dot', home)  # digits are skipped for the letter
        self.assertIn('<span class="av">R<i class="dot', home)  # a page's own icon, not the title's W
        self.assertIn('href="new.html"', home)
        new = read(os.path.join(out, "new.html"))
        self.assertIn('data-act="new"', new)
        self.assertIn("a reading list for a rainy weekend", new)  # waiting for Claude to draft it
        self.assertIn("a reading list for a rainy weekend", home)
        page = read(os.path.join(out, "p", "p2", "index.html"))
        self.assertIn('class="ri on" href="../../p/p2/"', page)  # the open task is marked on the rail
        definition = self.definition(id="p3", title="Reading")
        path = os.path.join(self.dir, "p3.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(definition, handle)
        self.assertEqual(self.cli("draft", path, "--request", "r9")[0], 2)  # no such request
        self.assertEqual(self.cli("draft", path, "--request", "r1")[0], 0)
        self.assertEqual([(r["id"], r["state"], r["page"]) for r in self.nb.requests()], [("r1", "drafted", "p3")])
        self.assertEqual(self.cli("request", " ")[0], 2)

    def test_debug_skills_and_the_knowledge_graph(self):
        self.draft(outputs=["page"])
        skill = "ARTIFACT: skill\n---\nname: count-up\n---\n1. one"
        folder = self.engine_run("outside", answers=(skill, "not a number", "7"))
        skill_id = next(e["id"] for e in notebook.run_facts(folder)["made"] if e["kind"] == "skill")
        os.makedirs(os.path.join(folder, "members", "claude"))
        with open(os.path.join(folder, "members", "claude", "events.jsonl"), "w") as handle:  # a member that opened the skill's file
            handle.write(json.dumps({"agent": "z", "t": time.time(), "event": {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Read", "input": {"file_path": f"/x/board/artifacts/{skill_id}.txt"}}]}}}) + "\n")
        notebook.attach(self.page(), folder, "claude")
        out = os.path.join(self.dir, "site")
        self.cli("view", "--out", out)
        page_html = read(os.path.join(out, "p", "p1", "index.html"))
        debug = page_html.split('id="debug"')[1].split("</section>")[0]
        self.assertIn("1 answer did not pass the judge", debug)
        self.assertIn("neither", debug)  # the judge's reason
        skills = page_html.split('id="skills"')[1].split("</section>")[0]
        self.assertIn("count-up", skills)
        self.assertIn("written in run 1 by a", skills)
        self.assertIn("used by z", skills)  # opening its file counts as using it
        graph = page_html.split('id="kb"')[1].split('<section class="panel tab"')[0]
        made = {e["id"]: e for e in self.page().facts(1)["made"]}
        for eid in made:
            self.assertIn(f'data-id="{eid}"', graph)
        self.assertEqual(graph.count('<circle class="n pass"'), 2)
        self.assertEqual(graph.count('<circle class="n fail"'), 1)

    def test_agents_show_what_the_records_say_and_open_their_details(self):
        self.draft(outputs=["page"], judge=[sys.executable, "-B", "page_judge.py", "--strict"])
        folder = self.engine_run("outside", answers=("ARTIFACT: page\n<!doctype html><p>v1</p>", "not a number"))
        notebook.attach(self.page(), folder, "claude")
        out = os.path.join(self.dir, "site")
        self.cli("view", "--out", out)
        page_html = read(os.path.join(out, "p", "p1", "index.html"))
        overview = page_html.split('id="overview"')[1].split('<section class="panel tab"')[0]
        self.assertIn('data-agent="a"', overview)
        self.assertIn('data-agent="plan"', overview)
        self.assertIn("did not pass", overview)  # a's last turn: its answer did not pass the judge
        self.assertIn("1/2 todos done", overview)  # the todos it took and the ones that passed, as recorded
        self.assertIn('<section class="agent-detail" id="agent-a">', overview)  # its details: in the page without JavaScript
        detail = overview.split('id="agent-a"')[1].split("</section>")[0]
        self.assertIn("run 1, turn 1: 100", detail)
        self.assertIn("Todos in run 1", detail)
        arch = page_html.split('id="arch"')[1].split('<section class="panel tab"')[0]
        self.assertIn('<a href="#agent-a" data-agent="a">', arch)  # every agent in the tree opens its panel
        self.assertIn("judge · page_judge.py", arch)
        self.assertNotIn("--strict", arch)  # the raw command is in Debug, not in the drawing
        self.assertIn("--strict", page_html.split('id="debug"')[1].split('<section class="panel tab"')[0])
        kb = page_html.split('id="kb"')[1].split('<section class="panel tab"')[0]
        self.assertIn('class="kb-search"', kb)
        self.assertIn('data-kind="page"', kb)

    def test_the_team_is_drawn_sideways_unless_the_notebook_asks_otherwise(self):
        self.draft(team={"planner": "plan=claude", "members": ["a=claude", "b=codex"]})
        out = os.path.join(self.dir, "site")
        shapes = {}
        for tree in (None, "tall", "both"):
            meta = {"title": "Test notebook", "lang": "en"}
            if tree:
                meta["tree"] = tree
            with open(os.path.join(self.folder, "notebook.json"), "w") as handle:
                json.dump(meta, handle)
            self.cli("view", "--out", out)
            arch = read(os.path.join(out, "p", "p1", "index.html")).split('id="arch"')[1].split('<section class="panel tab"')[0]
            shapes[tree] = re.findall(r'<div class="fig (tree-\w+(?: alt)?)">', arch)
        self.assertEqual(shapes, {None: ["tree-wide"], "tall": ["tree-tall"], "both": ["tree-wide", "tree-tall alt"]})

    def test_an_agents_state_comes_from_its_last_turn(self):
        from herdr_py.notebookview import agent_status

        def run(state, *turns, working=()):
            return {"state": state, "working": list(working), "turn_list": [dict(member="a", **x) for x in turns]}
        cases = [(run("running", working=["a"]), ("run", "working")), (run("running"), ("", "waiting")),
                 (run("ended"), ("", "no turn")), (run("ended", {"state": "error"}), ("bad", "broke")),
                 (run("ended", {"state": "idle", "kind": "failure"}), ("warn", "could not")),
                 (run("ended", {"state": "idle", "kind": "result", "status": "invalid"}), ("bad", "did not pass")),
                 (run("ended", {"state": "idle", "kind": "result", "status": "invalid"}, {"state": "idle", "kind": "result", "status": "valid"}),
                  ("ok", "passed")),  # the last turn decides
                 (run("ended", {"state": "idle"}), ("", "no answer")), (None, ("", "no turn"))]
        for facts, want in cases:
            self.assertEqual(agent_status(facts, "a", "en"), want, facts)

    def test_a_notebook_can_add_its_own_style_and_cannot_break_out_of_it(self):
        self.draft()
        with open(os.path.join(self.folder, "phone.css"), "w") as handle:
            handle.write(".rail { display:none; }</style><script>alert(1)</script>")
        with open(os.path.join(self.folder, "notebook.json"), "w") as handle:
            json.dump({"title": "Test notebook", "lang": "en", "style": "phone.css"}, handle)
        self.nb = Notebook(self.folder)
        out = os.path.join(self.dir, "site")
        self.cli("view", "--out", out)
        for page in ("index.html", "new.html", os.path.join("p", "p1", "index.html")):
            html_ = read(os.path.join(out, page))
            start = html_.index(".rail { display:none; }")
            self.assertIn(".rail { display:none; }<\\/style>", html_)  # its own style element, after the built-in one
            self.assertLess(html_.index("grid-template-columns:252px"), start)
            self.assertLess(html_.index("<script>alert", start), html_.index("</style>", start))  # still inside it: text, not a script
        with open(os.path.join(self.folder, "notebook.json"), "w") as handle:
            json.dump({"title": "Test notebook", "lang": "en", "style": "missing.css"}, handle)
        self.assertEqual(self.cli("view", "--out", out)[0], 0)  # a style that cannot be read adds nothing

    def test_no_step_of_how_a_page_works_is_cut_short(self):
        from herdr_py import notebookview
        for lang in ("en", "zh-TW"):
            svg = notebookview.life_svg(lang)
            self.assertNotIn("…", svg, lang)  # a label too wide for its box is cut with an ellipsis
            self.assertEqual(len(re.findall(r'<rect class="box', svg)), 6, lang)

    def test_a_text_file_opens_as_a_page_that_says_it_is_utf8(self):
        # sent as text/plain with no charset (python -m http.server does), a skill was read as Big5 on a phone set to
        # Traditional Chinese; the page it opens as says UTF-8, whatever the server sends
        from herdr_py import notebookview
        self.draft()
        skill = "ARTIFACT: skill\n---\nname: 長條圖做法\n---\n1. 每個月畫一根長條 <script>alert(1)</script>"
        made_page = '<!doctype html><html><head><meta charset="utf-8"></head><body><p>hi</p></body></html>'
        notebook.attach(self.page(), self.engine_run("outside", answers=(skill, made_page)), "claude")
        sid, pid = [e["id"] for e in self.page().facts(1)["made"]]
        out = os.path.join(self.dir, "site")
        self.cli("view", "--out", out)
        files = os.path.join(out, "p", "p1", "files")
        self.assertEqual(read(os.path.join(files, sid + ".txt")), skill.split("\n", 1)[1] + "\n")  # the file itself, kept
        with open(os.path.join(files, sid + ".html"), "rb") as handle:
            raw = handle.read()
        self.assertIn(b'<meta charset="utf-8">', raw[:1024])  # where a browser looks for it
        reader = raw.decode("utf-8")
        self.assertIn("每個月畫一根長條 &lt;script&gt;alert(1)&lt;/script&gt;", reader)  # shown, never run
        self.assertNotIn("<script>", reader)
        self.assertIn(f'href="{sid}.txt"', reader)
        task = read(os.path.join(out, "p", "p1", "index.html"))
        self.assertIn(f'files/{sid}.html"', task)
        self.assertNotIn(f'files/{sid}.txt"', task)  # every link opens the page, not the bare file
        self.assertEqual(read(os.path.join(files, pid + ".html")), made_page + "\n")  # a page the team made opens as itself
        for path, ctype, has in ((f"/p/p1/files/{sid}.html", "text/html; charset=utf-8", "&lt;script&gt;"),
                                 (f"/p/p1/files/{sid}.txt", "text/plain; charset=utf-8", "<script>"),
                                 (f"/p/p1/files/{pid}.html", "text/html; charset=utf-8", "<p>hi</p>")):
            code, body, got, _ = notebookview.route(self.nb.folder, path)
            self.assertEqual((code, got), (200, ctype), path)
            self.assertIn(has, body, path)
        live = notebookview.route(self.nb.folder, "/p/p1/")[1]
        self.assertIn(f'files/{sid}.html"', live)  # served live, the task's links open the page too
        self.assertNotIn(f'files/{sid}.txt"', live)

    def test_the_view_shows_what_a_run_brought_and_the_plus_asks_what_to_bring(self):
        self.draft()
        skill = "ARTIFACT: skill\n---\nname: count-up\n---\n1. one"
        notebook.attach(self.page(), self.engine_run("outside", answers=(skill,)), "claude")
        sid = self.page().facts(1)["made"][0]["id"]
        self.draft(id="p2", title="Second", **{"from": [{"page": "p1", "kinds": ["skill"]}]})
        notebook.approve(self.page("p2"), "person")
        self.script([add("give a number"), add(done=True)], {"a": [{"answer": "3"}]})
        notebook.start_run(self.page("p2"), "tester", out=io.StringIO())
        self.assertEqual(self.cli("request", "a third report", "--bring", "p1=skills,current", "--bring", "p2=knowledge")[0], 0)
        self.assertEqual(self.nb.requests()[0]["bring"], [{"page": "p1", "bring": ["skills", "current"]}, {"page": "p2", "bring": ["knowledge"]}])
        self.assertEqual(self.cli("request", "x", "--bring", "nowhere=skills")[0], 2)
        out = os.path.join(self.dir, "site")
        self.cli("view", "--out", out)
        p2 = read(os.path.join(out, "p", "p2", "index.html"))
        self.assertIn("reference material: A page, run 1: 1", p2)  # the run card
        self.assertIn(f'href="../p1/files/{sid}.html"', p2)  # the overview links each one to its own page
        self.assertIn("count-up", p2.split('id="overview"')[1].split('<section class="panel tab"')[0])
        new = read(os.path.join(out, "new.html"))
        self.assertIn('value="p1:skills"', new)
        self.assertIn('value="p2:current"', new)
        self.assertIn("A page: skills, current versions", new)  # the request lists what it asked to bring

    def test_a_dag_run_cut_off_shows_where_it_stopped(self):
        self.draft(kind="dag")
        folder = os.path.join(self.dir, "dag")
        os.makedirs(folder)
        node = {"member": {"name": "editor", "backend": "claude", "model": None, "command": None}, "needs": [], "task": "t",
                "judge": "true", "access": "read"}
        with open(os.path.join(folder, "plan.json"), "w") as handle:
            json.dump({"name": "words then design", "repo": self.dir, "base": "0" * 40, "order": ["words", "design"],
                       "nodes": {"words": node, "design": dict(node, needs=["words"])}}, handle)
        t = time.time() - notebook.QUIET - 60
        with open(os.path.join(folder, "events.jsonl"), "w") as handle:
            for e in ({"t": t, "seq": 1, "kind": "run.start"},
                      {"t": t, "seq": 2, "kind": "node.dispatch", "node": "words", "attempt": 1, "member": "editor"}):
                handle.write(json.dumps(e) + "\n")
        os.utime(os.path.join(folder, "events.jsonl"), (t, t))
        notebook.attach(self.page(), folder, "claude")
        self.assertEqual(self.page().state(), ("stuck", [("stuck", 1)]))
        self.cli("hold", "p1", "--why", "stopped it")
        out = os.path.join(self.dir, "site")
        self.assertEqual(self.cli("view", "--out", out)[0], 0)
        page_html = read(os.path.join(out, "p", "p1", "index.html"))
        self.assertIn("words (cut off: the run has no end) → design (waiting)", page_html)
        self.assertIn("words: cut off (the run has no end)", page_html.split('id="debug"')[1])
        self.assertIn("no end recorded", read(os.path.join(out, "index.html")))

    def test_words_for_time_and_counts(self):
        self.assertEqual((notebook.span_text(0.4, "en"), notebook.span_text(0.4, "zh-TW")), ("<1s", "不到 1 秒"))
        self.assertEqual((notebook.span_text(75, "en"), notebook.span_text(75, "zh-TW")), ("1m 15s", "1 分 15 秒"))
        self.assertEqual([notebook.say("en", "n_runs", n=n) for n in (1, 2)], ["1 run", "2 runs"])
        self.assertEqual(notebook.say("zh-TW", "n_runs", n=1), "1 次執行")

    def test_status_names_what_waits(self):
        self.draft()
        code, out, _ = self.cli("status")
        self.assertEqual(code, 0)
        self.assertIn("! p1: approve it before it runs", out)
        self.assertIn("p1 [draft] A page", out)


class ServeTest(Base):
    def setUp(self):
        super().setUp()
        import threading
        from herdr_py import notebookview
        self.draft(outputs=["page"])
        self.folder_run = self.engine_run("outside", answers=("ARTIFACT: page\n<!doctype html><p>v1</p>", "5"))
        notebook.attach(self.page(), self.folder_run, "claude")
        ready = threading.Event()
        self.url = None

        def up(url):
            self.url = url
            ready.set()
        self.thread = threading.Thread(target=notebookview.serve, args=(self.folder,), kwargs={"port": 0, "token": "tok", "ready": up},
                                       daemon=True)
        self.thread.start()
        self.assertTrue(ready.wait(10))
        self.base = self.url.split("#")[0].rstrip("/")

    def fetch(self, path, body=None, token=None):
        import urllib.error
        import urllib.request
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method="POST" if data is not None else "GET")
        if token:
            req.add_header("Authorization", "Bearer " + token)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read().decode("utf-8"), dict(r.headers)
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read().decode("utf-8"), dict(e.headers)

    def test_pages_are_drawn_live_and_commands_need_the_token(self):
        self.assertTrue(self.url.endswith("#token=tok"))
        code, body, _ = self.fetch("/")
        self.assertEqual(code, 200)
        self.assertIn('"live": true', body)
        page = self.page().facts(1)["made"][0]["id"]
        self.assertEqual(self.fetch("/api/act", {"command": "pick", "page": "p1", "entry": page})[0], 403)
        self.assertEqual(self.fetch("/api/act", {"command": "pick", "page": "p1", "entry": page}, token="nope")[0], 403)
        self.assertEqual(self.page().picks(), {})  # refused commands record nothing
        code, body, _ = self.fetch("/p/p1/api/act", {"command": "pick", "page": "p1", "entry": page}, token="tok")
        self.assertEqual(code, 200, body)
        self.assertEqual(self.page().picks()["page"]["by"], "person (web)")
        code, body, _ = self.fetch("/api/act", {"command": "approve", "page": "p1", "digest": "stale"}, token="tok")
        self.assertEqual(code, 409)  # the definition changed after the page was drawn (or was never shown)
        code, body, _ = self.fetch("/api/act", {"command": "approve", "page": "p1", "digest": self.page().digest()}, token="tok")
        self.assertEqual(code, 200, body)
        self.assertEqual(self.fetch("/api/act", {"command": "explode", "page": "p1"}, token="tok")[0], 409)
        self.assertEqual(self.fetch("/api/new", {"goal": "a list of five books", "bring": [{"page": "p1", "bring": ["skills", "nonsense"]}]},
                                    token="tok")[0], 200)
        self.assertEqual([(r["goal"], r["bring"]) for r in self.nb.requests()], [("a list of five books", [{"page": "p1", "bring": ["skills"]}])])
        self.assertEqual(self.fetch("/api/new", {"goal": "x", "bring": [{"page": "nowhere", "bring": ["skills"]}]}, token="tok")[0], 409)
        self.assertIn("a list of five books", self.fetch("/new.html")[1])

    def test_the_teams_files_run_in_a_sandbox_and_nothing_else_is_served(self):
        page = self.page().facts(1)["made"][0]["id"]
        code, body, headers = self.fetch(f"/p/p1/files/{page}.html")
        self.assertEqual((code, body.strip()), (200, "<!doctype html><p>v1</p>"))
        self.assertEqual(headers.get("Content-Security-Policy"), "sandbox allow-scripts")
        self.assertEqual(self.fetch("/p/p1/runs/1.html")[0], 200)
        for path in ("/p/p1/runs/2.html", "/p/../../etc/passwd", "/p/p1/files/k000000000000.html", f"/p/p1/files/{page}.txt",
                     "/p/nope/", "/notebook.json", "/pages/p1/history.jsonl"):
            self.assertEqual(self.fetch(path)[0], 404, path)


@unittest.skipUnless(shutil.which("git"), "needs git for the DAG task (RHEL 8 images may not have it; CI installs it)")
class DemoTest(unittest.TestCase):
    def test_the_demo_notebook_builds_without_models(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        out = os.path.join(folder, "demo")
        p = subprocess.run([sys.executable, "-B", os.path.join(ROOT, "examples", "notebook", "make_demo.py"), out],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=300)
        self.assertEqual(p.returncode, 0, p.stdout[-2000:])
        nb = Notebook(os.path.join(out, "notebook"))
        states = {pg.id: pg.state() for pg in nb.pages()}
        self.assertEqual(states, {"museum-report": ("review", [("review", 2)]), "circles": ("approved", []),
                                  "calculator": ("hold", []), "circles-bigger-team": ("draft", [("approve", None)]),
                                  "library-report": ("review", [("review", 1)]), "library-report-plain": ("review", [("review", 1)])})

        def turns_to_full_page(pid):  # member turns until a page scored 100, as the run recorded them
            for i, x in enumerate(nb.page(pid).facts(1)["turn_list"], 1):
                if x.get("kind") == "result" and x.get("status") == "valid" and x.get("score") == 100:
                    return i
            return None
        with_skills, plain = turns_to_full_page("library-report"), turns_to_full_page("library-report-plain")
        self.assertIsNotNone(plain)
        self.assertLess(with_skills, plain)  # P30, on made-up members: the borrowed skill saved a turn
        borrowed = nb.page("library-report").runs()[0]["references"]
        self.assertEqual([r["page"] for r in borrowed], ["museum-report"])
        self.assertIn("reference/museum-report/", next(e["summary"] for e in nb.page("library-report").facts(1)["made"] if e["kind"] == "skill"))
        self.assertEqual(nb.page("library-report").facts(1)["carried"], [])  # brought as reference material, not as its results
        museum = nb.page("museum-report")
        second = museum.facts(2)
        self.assertEqual(sorted(e["kind"] for e in second["carried"]), ["data", "data", "skill"])
        self.assertEqual(max(e["score"] for e in second["made"] if e["kind"] == "page"), 100)
        self.assertEqual(museum.runs()[1]["notes"], ["n1"])
        circles = nb.page("circles")  # whether run 2 beats run 1 depends on the platform's floats (the search is seeded), so not asserted
        first, second = circles.facts(1), circles.facts(2)
        self.assertEqual(sorted(e["id"] for e in second["carried"]), sorted(e["id"] for e in first["made"] if e["status"] == "valid"))
        self.assertGreaterEqual(len(second["used"]), 1)  # it built on what it carried
        self.assertEqual(circles.picks()["result"]["run"], 2)
        self.assertEqual([r["state"] for r in nb.requests()], ["open"])


if __name__ == "__main__":
    unittest.main()
