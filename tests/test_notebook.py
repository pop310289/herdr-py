"""A notebook of pages (herdr_py/notebook.py): a draft never runs, an approval covers the definition and the task, a run
goes on from the last one with what passed and the notes nobody used yet and without what a person excluded, decisions
are only appended, and what waits for a person is worked out from the records. The teams are the stand-in planner,
members and judge of test_engine.py, run by the real engine in its own process."""
import contextlib
import io
import json
import os
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


# A judge for pages: an answer with a paragraph in it is valid with score 100; otherwise as JUDGE (a number is its score).
PAGE_JUDGE = r'''
import json, sys
text = open(sys.argv[-1]).read().strip()
if "<p>" in text:
    print(json.dumps({"status": "valid", "score": 100, "detail": "a page"}))
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

    def draft(self, **over):
        path = os.path.join(self.dir, "draft.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.definition(**over), handle, ensure_ascii=False)
        return notebook.draft(self.nb, path, "claude")

    def page(self, pid="p1"):
        return self.nb.page(pid)  # read again: what another process wrote is seen

    def run_page(self, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = notebook.start_run(self.page(), "tester", out=out, **kw)
        return code, out.getvalue()

    def engine_run(self, name, answers=("5",)):
        """A run made outside the notebook (as engine.main would make it), to attach."""
        self.script([add(*[{"text": f"answer {i}", "for": "a", "parents": []} for i in range(len(answers))]), add(done=True)],
                    {"a": [{"answer": x, "summary": f"answer {i}"} for i, x in enumerate(answers)]})
        out = os.path.join(self.dir, name)
        argv = ["--task", os.path.join(self.dir, "task.md"), "--judge", f"{sys.executable} -B page_judge.py",
                "--planner", f"plan=command:{sys.executable} -B planner.py", "--member", f"a=command:{sys.executable} -B member.py",
                "--turns", str(len(answers)), "--out", out]
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
    def test_the_view_shows_what_waits_every_version_and_is_complete_without_javascript(self):
        self.draft(title="Plans <script>alert(1)</script>", outputs=["page"])
        folder = self.engine_run("outside", answers=("ARTIFACT: page\n<!doctype html><p>v1</p>",))
        notebook.attach(self.page(), folder, "claude")
        self.engine_run("loose", answers=("4",))  # a run no page holds
        out = os.path.join(self.dir, "site")
        code, printed, err = self.cli("view", "--out", out, "--runs", self.dir)
        self.assertEqual(code, 0, err)
        home = read(os.path.join(out, "index.html"))
        self.assertNotIn("<script", home)  # the notebook's pages need no script
        self.assertNotIn("<script>alert", home)
        self.assertIn("Plans &lt;script&gt;", home)
        self.assertIn("review run 1", home)
        self.assertIn("1 of 2 team runs", home)
        self.assertIn("loose", home)
        page_html = read(os.path.join(out, "p", "p1", "index.html"))
        self.assertIn("no current version picked", page_html)
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
        home = read(os.path.join(out, "index.html"))
        self.assertIn("words (cut off: the run has no end) → design (waiting)", home)
        self.assertIn("no end recorded", home)

    def test_status_names_what_waits(self):
        self.draft()
        code, out, _ = self.cli("status")
        self.assertEqual(code, 0)
        self.assertIn("! p1: approve it before it runs", out)
        self.assertIn("p1 [draft] A page", out)


if __name__ == "__main__":
    unittest.main()
