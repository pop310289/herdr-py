"""teamrun.py and runreport.py with scripted members and a stub renderer (no model, no network, no Chrome).

The acceptance run repeats one spec three times; every number in report.md and aggregate.md is then recomputed here from
summary.json, chat.jsonl and the scripted members' own record of their turns. Also: the spec checks, a failed run, the
member router, OpenCode members through a daemon of the run, and --compare."""
import contextlib
import io
import json
import math
import os
import shutil
import stat
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "examples", "slide_team"))
import components as C  # noqa: E402
import imgcmp  # noqa: E402
import layout_team as L  # noqa: E402
import runreport  # noqa: E402
import teamrun  # noqa: E402
from fake_agents import Script  # noqa: E402
from fake_opencode import FakeOpenCode  # noqa: E402
from stub_render import RectRenderer  # noqa: E402

LAYOUT = os.path.join(os.path.dirname(HERE), "examples", "slide_team", "layout")
FAKE_TEAM = [{"name": "drawA", "role": "drawer", "backend": "fake"}, {"name": "drawB", "role": "drawer", "backend": "fake"},
             {"name": "art", "role": "art", "backend": "fake"}]
TASK = {"original": "original.png", "canvas": list(L.SIZE)}


def make_original(folder):
    """The 'original' of these runs: the hand-made reference layout, drawn by the stub renderer."""
    with open(os.path.join(LAYOUT, "base_portrait.json")) as a, open(os.path.join(LAYOUT, "reference_portrait.json")) as b:
        errors, _, comps = C.check(json.load(a) + json.load(b), size=L.SIZE)
    assert not errors, errors
    svg = os.path.join(folder, "original.svg")
    with open(svg, "w") as handle:
        handle.write(C.svg(comps, size=L.SIZE))
    RectRenderer().render(svg, os.path.join(folder, "original.png"), L.SIZE)


def write_spec(folder, name, **spec):
    path = os.path.join(folder, name)
    with open(path, "w") as handle:
        json.dump(spec, handle)
    return path


def tables(text):
    """The markdown tables in text, each a list of rows of cells (the header first, the |---| line left out)."""
    out, current = [], None
    for line in text.splitlines():
        if not line.startswith("|"):
            current = None
            continue
        if set(line.replace("|", "").strip()) <= {"-"}:
            continue
        if current is None:
            current = []
            out.append(current)
        current.append([cell.strip() for cell in line.strip().strip("|").split("|")])
    return out


def table_with(text, first):
    return next(t for t in tables(text) if t[0][0] == first)


def read_jsonl(path):
    with open(path) as handle:
        return [json.loads(line) for line in handle]


def quiet(fn, *args, **kw):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = fn(*args, **kw)
    return code, out.getvalue(), err.getvalue()


class RepeatedRunTest(unittest.TestCase):
    """The acceptance run: the same spec three times, then every reported number against the run's own files."""
    REPS = 3

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        make_original(cls.dir)
        spec = write_spec(cls.dir, "spec.json", title="scripted team", task=TASK, members=FAKE_TEAM,
                          rounds={"revisions": 2, "fixes": 2}, output="runs")
        cls.out = os.path.join(cls.dir, "runs")
        cls.code, cls.stdout, cls.stderr = quiet(teamrun.main, [spec, "--repeat", str(cls.REPS)], renderer=RectRenderer())

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir)

    def rep(self, k):
        return os.path.join(self.out, f"rep-{k}")

    def files(self, k):
        """What run k left: summary.json, the scripted members' record of their turns, chat.jsonl, their tokens."""
        with open(os.path.join(self.rep(k), "summary.json")) as handle:
            summary = json.load(handle)
        with open(os.path.join(self.rep(k), "fake", "agents.json")) as handle:
            agents = json.load(handle)
        return (summary, read_jsonl(os.path.join(self.rep(k), "fake", "turns.jsonl")),
                read_jsonl(os.path.join(self.rep(k), "chat.jsonl")), agents)

    def test_every_run_finished_and_left_its_files(self):
        self.assertEqual(self.code, 0, self.stderr)
        for k in range(1, self.REPS + 1):
            for name in ("chat.jsonl", "summary.json", "run.json", "report.md", "slide.png"):
                self.assertTrue(os.path.exists(os.path.join(self.rep(k), name)), (k, name))
            self.assertFalse(os.path.exists(os.path.join(self.rep(k), "deck.json")))  # no open_slide in the spec: no PPTX
        again = os.path.join(self.dir, "slide-again.png")  # the final picture comes from the renderer the run was given
        RectRenderer().render(os.path.join(self.rep(1), "slide.svg"), again, L.SIZE)
        with open(again, "rb") as a, open(os.path.join(self.rep(1), "slide.png"), "rb") as b:
            self.assertEqual(a.read(), b.read())
        with open(os.path.join(self.out, "spec.json")) as handle:
            self.assertEqual(json.load(handle)["rounds"], {"revisions": 2, "fixes": 2, "score": "strict"})  # the default score that keeps a revision
        self.assertIn(f"{self.REPS} of {self.REPS} runs finished", self.stdout)

    def test_report_numbers_come_from_the_files(self):
        for k in range(1, self.REPS + 1):
            summary, turns, chat, agents = self.files(k)
            with open(os.path.join(self.rep(k), "report.md")) as handle:
                report = handle.read()
            rows = {r[0]: r[1:] for r in table_with(report, "Row")[1:]}
            for n in (1, 2, 3):
                recs = [t for t in summary["turns"] if t["row"] == n]
                revised = [t for t in recs if t["kind"] == "revise"]
                kept = [t for t in recs if t["accepted"]]
                expect = ["{:.3f}".format(recs[0]["match"]) if recs[0]["valid"] else "-",
                          "{:.3f}".format(kept[-1]["match"]) if kept else "-",
                          str(sum(1 for t in revised if t["accepted"])), str(sum(1 for t in revised if t["valid"] and not t["accepted"])),
                          str(sum(1 for t in revised if not t["valid"])),
                          str(sum(1 for t in turns if t["kind"] == "fix" and t["row"] == n)),
                          str(len(kept[-1]["missing"])) if kept else "-"]
                self.assertEqual(rows[str(n)], expect, (k, n))
            self.assertEqual(rows["all"][5], str(sum(1 for t in turns if t["kind"] == "fix")))
            final = summary["final"]
            self.assertIn("Match {:.3f} · strict {:.3f} · PSNR {:.2f} dB".format(final["match"], final["strict"], final["psnr"]), report)
            self.assertIn("(the strict score (match, borders and text colour together; scoring.py) minus", report)
            members = {r[0]: r for r in table_with(report, "Member")[1:]}
            for name in ("drawA", "drawB", "art"):
                self.assertEqual(members[name][5], str(sum(1 for t in turns if t["agent"] == name)), (k, name))
                self.assertEqual(members[name][7], "{:,}".format(agents[name]["tokens"]), (k, name))
            for lesson, n in summary["lessons"].items():
                self.assertIn(f"- {lesson} ({n} time", report)

    def test_aggregate_numbers_come_from_the_files(self):
        per = []
        for k in range(1, self.REPS + 1):
            summary, turns, chat, agents = self.files(k)
            per.append({"match": summary["final"]["match"], "strict": summary["final"]["strict"],
                        "accepted": sum(1 for t in summary["turns"] if t["kind"] == "revise" and t["accepted"]),
                        "fixups": sum(1 for t in turns if t["kind"] == "fix"),
                        "minutes": (chat[-1]["t"] - chat[0]["t"]) / 60,
                        "tokens": sum(a["tokens"] for a in agents.values())})
        with open(os.path.join(self.out, "aggregate.md")) as handle:
            text = handle.read()
        self.assertTrue(text.startswith(f"# scripted team: {self.REPS} of {self.REPS} runs"), text[:80])
        stats = {r[0]: r[1:] for r in table_with(text, "")[1:]}
        for label, key, form in (("Final match", "match", "{:.3f}"), ("Final strict", "strict", "{:.3f}"), ("Accepted revisions", "accepted", "{:.2f}"),
                                 ("Fix-ups", "fixups", "{:.2f}"), ("Minutes", "minutes", "{:.2f}"), ("Tokens", "tokens", "{:,.0f}")):
            values = [p[key] for p in per]
            self.assertEqual(stats[label], [form.format(sum(values) / len(values)), form.format(min(values)), form.format(max(values))],
                             label)
        runs = {r[0]: r for r in table_with(text, "Run")[1:]}
        self.assertEqual([runs[f"rep-{k}"][1] for k in range(1, self.REPS + 1)], ["{:.3f}".format(p["match"]) for p in per])
        self.assertEqual([runs[f"rep-{k}"][2] for k in range(1, self.REPS + 1)], ["{:.3f}".format(p["strict"]) for p in per])
        # the scripted runs differ (seed = run number), so a mix-up of runs would show
        self.assertGreater(len({(p["accepted"], p["fixups"]) for p in per}), 1)

    def test_the_scripted_members_went_through_every_path(self):
        turns = [t for k in range(1, self.REPS + 1) for t in self.files(k)[0]["turns"]]
        self.assertTrue(any(t["kind"] == "revise" and t["accepted"] for t in turns))
        self.assertTrue(any(t["kind"] == "revise" and t["valid"] and not t["accepted"] for t in turns))
        self.assertTrue(all(sum(1 for t in self.files(k)[1] if t["kind"] == "fix") for k in range(1, self.REPS + 1)))

    def test_compare_puts_two_aggregates_side_by_side(self):
        spec = write_spec(self.dir, "single.json", title="one drawer", task=TASK, rounds={"revisions": 1, "fixes": 1},
                          members=[{"name": "solo", "role": "drawer", "backend": "fake"}, {"name": "critic", "role": "art", "backend": "fake"}],
                          output="single")
        code, _, err = quiet(teamrun.main, [spec], renderer=RectRenderer())
        self.assertEqual(code, 0, err)
        single = os.path.join(self.dir, "single")
        self.assertTrue(os.path.exists(os.path.join(single, "report.md")))
        speakers = {m["from"] for m in read_jsonl(os.path.join(single, "chat.jsonl"))}
        self.assertTrue({"solo", "critic"} <= speakers, speakers)  # the spec's names, one drawer revising its own rows
        art_turns = [t for t in read_jsonl(os.path.join(single, "fake", "turns.jsonl")) if t["kind"] == "art"]
        self.assertEqual([t["agent"] for t in art_turns], ["critic"] * 3)  # one review before each row's revision
        with open(os.path.join(single, "summary.json")) as handle:
            one = "{:.3f}".format(json.load(handle)["final"]["match"])
        code, out, _ = quiet(teamrun.main, ["--compare", self.out, single])
        self.assertEqual(code, 0)
        self.assertIn("A: scripted team", out)
        self.assertIn("B: one drawer", out)
        rows = {r[0]: r[1:] for r in table_with(out, "")[1:]}
        self.assertEqual(rows["Runs counted"], [f"{self.REPS} of {self.REPS}", "1 of 1"])
        matches = [self.files(k)[0]["final"]["match"] for k in range(1, self.REPS + 1)]
        self.assertEqual(rows["Final match"], ["{:.3f} ({:.3f} to {:.3f})".format(sum(matches) / len(matches), min(matches), max(matches)),
                                               f"{one} ({one} to {one})"])


class FailedRunTest(unittest.TestCase):
    def test_a_failed_run_is_reported_and_left_out_of_the_aggregate(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        make_original(d)
        spec = write_spec(d, "spec.json", task=TASK, members=FAKE_TEAM, rounds={"revisions": 0, "fixes": 2}, output="runs")

        class SecondRunFails(RectRenderer):
            def render(self, svg_path, png_path, size):
                return "rep-2" not in svg_path and RectRenderer.render(self, svg_path, png_path, size)
        code, _, _ = quiet(teamrun.main, [spec, "--repeat", "2"], renderer=SecondRunFails())
        self.assertEqual(code, 1)
        with open(os.path.join(d, "runs", "rep-2", "run.json")) as handle:
            self.assertEqual(json.load(handle)["error"], "RuntimeError: the renderer did not write the picture")
        with open(os.path.join(d, "runs", "rep-2", "report.md")) as handle:
            self.assertIn("**This run failed:** RuntimeError", handle.read())
        with open(os.path.join(d, "runs", "aggregate.md")) as handle:
            text = handle.read()
        self.assertTrue(text.startswith("# runs: 1 of 2 runs"), text[:60])
        self.assertIn("- rep-2: RuntimeError: the renderer did not write the picture", text)
        with open(os.path.join(d, "runs", "rep-1", "summary.json")) as handle:
            m = "{:.3f}".format(json.load(handle)["final"]["match"])
        self.assertEqual(table_with(text, "")[1], ["Final match", m, m, m])

    def test_a_run_that_exits_is_a_failed_run(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        make_original(d)
        spec = write_spec(d, "spec.json", task=TASK, members=FAKE_TEAM, rounds={"revisions": 0, "fixes": 2}, output="run")

        class Exits(RectRenderer):
            def render(self, svg_path, png_path, size):
                raise SystemExit("the renderer gave up")
        code, _, _ = quiet(teamrun.main, [spec], renderer=Exits())
        self.assertEqual(code, 1)
        with open(os.path.join(d, "run", "run.json")) as handle:
            self.assertEqual(json.load(handle)["error"], "SystemExit: the renderer gave up")

    def test_a_stop_by_hand_still_leaves_the_aggregate_of_the_runs_before(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        make_original(d)
        spec = write_spec(d, "spec.json", task=TASK, members=FAKE_TEAM, rounds={"revisions": 0, "fixes": 2}, output="runs")

        class CtrlCInSecondRun(RectRenderer):
            def render(self, svg_path, png_path, size):
                if "rep-2" in svg_path:
                    raise KeyboardInterrupt
                return RectRenderer.render(self, svg_path, png_path, size)
        code, _, _ = quiet(teamrun.main, [spec, "--repeat", "3"], renderer=CtrlCInSecondRun())
        self.assertEqual(code, 130)
        self.assertFalse(os.path.exists(os.path.join(d, "runs", "rep-3")))
        with open(os.path.join(d, "runs", "aggregate.md")) as handle:
            text = handle.read()
        self.assertTrue(text.startswith("# runs: 1 of 2 runs"), text[:60])
        self.assertIn("- rep-2: interrupted", text)


class TeamShapeTest(unittest.TestCase):
    def test_three_drawers_take_turns_and_each_revises_the_one_before(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        make_original(d)
        with open(os.path.join(LAYOUT, "SPEC_portrait.md")) as handle:
            checklist = handle.read().replace("## Row 1", "## Row 1\nMARKER: a checklist of this run's own", 1)
        with open(os.path.join(d, "mine.md"), "w") as handle:
            handle.write(checklist)
        members = [{"name": n, "role": "drawer", "backend": "fake"} for n in ("d1", "d2", "d3")] + [FAKE_TEAM[2]]
        spec = write_spec(d, "spec.json", task=dict(TASK, checklist="mine.md"), members=members, rounds={"revisions": 2, "fixes": 0},
                          output="run")
        code, _, err = quiet(teamrun.main, [spec], renderer=RectRenderer())
        self.assertEqual(code, 0, err)
        with open(os.path.join(d, "run", "SPEC.md")) as handle:
            self.assertIn("MARKER: a checklist of this run's own", handle.read())
        rounds = [m for m in read_jsonl(os.path.join(d, "run", "chat.jsonl")) if m["from"] == "manager"]
        self.assertEqual([m["to"] for m in rounds], ["d1", "d2", "d3", "d1", "d2", "d3", "d1", "d2", "d3"])
        for before, now in zip(rounds, rounds[1:]):
            if " revise " in now["text"]:
                self.assertIn(f"revise {before['to']}'s version", now["text"])

    def test_chrome_renderer_hands_the_files_to_headless_chrome(self):
        calls = []
        saved = L.slide_team.screenshot
        self.addCleanup(setattr, L.slide_team, "screenshot", saved)
        L.slide_team.screenshot = lambda *args, **kw: calls.append((args, kw)) or True
        self.assertTrue(L.ChromeRenderer("/apps/chrome", "/tmp/profile").render("a.svg", "a.png", (12, 34)))
        self.assertEqual(calls, [(("/apps/chrome", "a.svg", "a.png", "/tmp/profile"), {"size": (12, 34)})])

    def test_runs_are_read_in_number_order(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        for k in (1, 2, 10, 11):
            os.makedirs(os.path.join(d, f"rep-{k}"))
        os.makedirs(os.path.join(d, "rep-x"))
        self.assertEqual([os.path.basename(p) for p in runreport.runs_in(d)], ["rep-1", "rep-2", "rep-10", "rep-11"])


class SpecTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        make_original(self.dir)

    def test_every_problem_is_listed_before_anything_runs(self):
        spec = write_spec(self.dir, "bad.json", tittle="x", task={"original": "original.png", "canvas": [1920, 1080]},
                          members=[{"name": "drawA", "role": "drawer", "backend": "gpt"},
                                   {"name": "drawA", "role": "art", "backend": "opencode", "sessions": "forever", "model": "qwen3"},
                                   {"name": "check", "role": "art", "backend": "fake", "temperature": 0}],
                          rounds={"revisions": -1}, output="runs")
        code, _, err = quiet(teamrun.main, [spec], renderer=RectRenderer())
        self.assertEqual(code, 2)
        for part in ("unknown keys tittle", "task.canvas: the layout team knows one canvas, 1206 x 1441",
                     "members[0].backend: one of opencode, codex, claude, fake", "members[1].name: drawA is used twice",
                     "members[1].sessions: fresh or keep", "members[2].name: check is a role in the conversation log",
                     "members[2]: unknown keys temperature", "members: exactly one art director (role art), not 2",
                     "rounds.revisions: a whole number, 0 or more", "opencode: missing",
                     "members[1].model: OpenCode names a model provider/model"):
            self.assertIn(part, err)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "runs")))

    def test_the_picture_and_the_checklist_are_checked(self):
        imgcmp.write_png(os.path.join(self.dir, "small.png"), (10, 10, [(bytes(10), bytes(10), bytes(10))] * 10))
        with open(os.path.join(self.dir, "list.md"), "w") as handle:
            handle.write("## Row 1\n## Row 2\n")
        spec = write_spec(self.dir, "spec.json", task={"original": "small.png", "canvas": list(L.SIZE), "checklist": "list.md"},
                          members=FAKE_TEAM, output="runs")
        with self.assertRaises(teamrun.SpecError) as ctx:
            teamrun.load_spec(spec)
        self.assertIn("task.original: the picture is 10 x 10 pixels, the canvas 1206 x 1441", str(ctx.exception))
        self.assertIn('has no "## Row 3" section', str(ctx.exception))

    def test_compare_needs_two_run_folders(self):
        code, _, err = quiet(teamrun.main, ["--compare", self.dir, os.path.join(self.dir, "nothing")])
        self.assertEqual(code, 2)
        self.assertIn("no run folder at " + os.path.join(self.dir, "nothing"), err)

    def test_paths_are_relative_to_the_spec_and_defaults_are_filled(self):
        os.makedirs(os.path.join(self.dir, "specs"))
        spec = teamrun.load_spec(write_spec(os.path.join(self.dir, "specs"), "s.json", task=dict(TASK, original="../original.png"),
                                            members=FAKE_TEAM[:2] + [{"name": "art", "role": "art", "backend": "opencode"}],
                                            opencode={"url": "http://127.0.0.1:4096"}, output="out"))
        self.assertEqual(spec["task"]["original"], os.path.join(self.dir, "original.png"))
        self.assertEqual(spec["output"], os.path.join(self.dir, "specs", "out"))
        defaults = L.arguments().parse_args(["--workdir", ".", "--reference", ".", "--open-slide", ""])
        self.assertEqual(spec["rounds"], {"revisions": defaults.revisions, "fixes": defaults.fixes, "score": defaults.score})
        self.assertEqual(spec["timeouts"], {"turn": defaults.turn_timeout, "art": defaults.art_timeout})
        self.assertEqual({m["sessions"] for m in spec["members"]}, {"fresh"})
        self.assertEqual(spec["opencode"]["policy"], os.path.join(LAYOUT, "policy.json"))

    def test_the_score_that_keeps_a_revision_can_be_chosen(self):
        def spec_with(**rounds):
            return write_spec(self.dir, "s.json", task=TASK, members=FAKE_TEAM, rounds=rounds, output="out")
        spec = teamrun.load_spec(spec_with(score="match"))
        self.assertEqual(spec["rounds"]["score"], "match")
        self.assertEqual(teamrun.team_args(spec, self.dir).score, "match")
        self.assertEqual(teamrun.team_args(teamrun.load_spec(spec_with()), self.dir).score, "strict")
        code, _, err = quiet(teamrun.main, [spec_with(score="fuzzy")], renderer=RectRenderer())
        self.assertEqual(code, 2)
        self.assertIn("rounds.score: one of match, strict", err)

    def test_an_output_that_holds_files_is_never_overwritten(self):
        os.makedirs(os.path.join(self.dir, "runs"))
        with open(os.path.join(self.dir, "runs", "keep.txt"), "w") as handle:
            handle.write("earlier run")
        spec = write_spec(self.dir, "spec.json", task=TASK, members=FAKE_TEAM, output="runs")
        code, _, err = quiet(teamrun.main, [spec], renderer=RectRenderer())
        self.assertEqual(code, 2)
        self.assertIn("already holds files", err)
        self.assertEqual(os.listdir(os.path.join(self.dir, "runs")), ["keep.txt"])

    def test_chrome_must_be_there_unless_another_renderer_is_given(self):
        spec = write_spec(self.dir, "spec.json", task=TASK, members=FAKE_TEAM, chrome="no-such-chrome", output="runs")
        code, _, err = quiet(teamrun.main, [spec])
        self.assertEqual(code, 2)
        self.assertIn("Chrome is not at " + os.path.join(self.dir, "no-such-chrome"), err)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "runs")))

    def test_the_prompt_limit_covers_every_turn_of_the_busiest_member(self):
        two = {"members": FAKE_TEAM, "rounds": {"revisions": 2, "fixes": 2}}
        self.assertEqual(teamrun.prompt_limit(two), 15)  # 9 drawer turns: one drawer gets 5, each with up to 2 fix-ups
        one = {"members": FAKE_TEAM[:1] + FAKE_TEAM[2:], "rounds": {"revisions": 3, "fixes": 0}}
        self.assertEqual(teamrun.prompt_limit(one), 12)


class MembersTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        names = ("CLAUDE_BIN", "FAKE_CLAUDE_LOG", "FAKE_CLAUDE_STATE", "FAKE_CLAUDE_MODE", "CODEX_BIN", "FAKE_CODEX_LOG",
                 "FAKE_CODEX_MODE")
        saved = {k: os.environ.get(k) for k in names}
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v) for k, v in saved.items()])
        os.makedirs(os.path.join(self.dir, "bin"))
        for program, fake in (("claude", "fake_claude.py"), ("codex", "fake_codex.py")):
            path = os.path.join(self.dir, "bin", program)
            with open(path, "w") as handle:
                handle.write(f"#!/bin/sh\nexec {sys.executable} {os.path.join(HERE, fake)} \"$@\"\n")
            os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        os.environ.update({"CLAUDE_BIN": os.path.join(self.dir, "bin", "claude"), "FAKE_CLAUDE_LOG": os.path.join(self.dir, "calls.jsonl"),
                           "FAKE_CLAUDE_STATE": os.path.join(self.dir, "sessions.json"), "CODEX_BIN": os.path.join(self.dir, "bin", "codex"),
                           "FAKE_CODEX_LOG": os.path.join(self.dir, "codex-calls.jsonl")})
        os.environ.pop("FAKE_CLAUDE_MODE", None)
        os.environ.pop("FAKE_CODEX_MODE", None)

    def test_codex_members_share_one_instance_and_keep_or_drop_their_threads(self):
        members = teamrun.Members([{"name": "drawA", "role": "drawer", "backend": "codex", "model": "gpt-x", "sessions": "keep"},
                                   {"name": "drawB", "role": "drawer", "backend": "codex", "model": None, "sessions": "fresh"},
                                   {"name": "art", "role": "art", "backend": "fake", "model": None, "sessions": "fresh"}], self.dir)
        self.addCleanup(members.close)
        for name in ("drawA", "drawA", "drawB", "drawB"):
            self.assertEqual(members.run_turn(name, "You are " + name, model="team-default")[1], "idle")
        a1, a2, b1, b2 = read_jsonl(os.environ["FAKE_CODEX_LOG"])
        self.assertEqual(a2["args"][:2], ["resume", a1["thread"]])  # drawA keeps its conversation
        self.assertTrue(all("resume" not in c["args"] for c in (a1, b1, b2)))  # drawB starts a new one every turn
        self.assertEqual([c["args"][c["args"].index("-m") + 1] if "-m" in c["args"] else None for c in (a1, a2, b1, b2)],
                         ["gpt-x", "gpt-x", None, None])
        self.assertEqual({m["name"]: m["tokens"] for m in members.summary()}, {"drawA": 220, "drawB": 220, "art": 0})
        self.assertTrue(os.path.isdir(os.path.join(self.dir, "codex", "drawA")))

    def test_each_member_goes_to_its_backend_with_its_own_model_and_session_rule(self):
        members = teamrun.Members([{"name": "drawA", "role": "drawer", "backend": "claude", "model": "claude-x", "sessions": "fresh"},
                                   {"name": "drawB", "role": "drawer", "backend": "claude", "model": None, "sessions": "keep"},
                                   {"name": "art", "role": "art", "backend": "fake", "model": "m1", "sessions": "fresh"}], self.dir)
        self.addCleanup(members.close)
        for name in ("drawA", "drawA", "drawB", "drawB"):
            self.assertEqual(members.run_turn(name, "You are " + name, model="team-default")[1], "idle")
        pictures = [os.path.join(self.dir, p) for p in ("a.png", "b.png")]
        for path in pictures:
            imgcmp.write_png(path, (2, 2, [(bytes(2), bytes(2), bytes(2))] * 2))
        text, state = members.run_turn("art", "You are the art director. Image 1 is row 2 of the original", files=pictures)
        self.assertTrue(text.startswith("DIFF: row 2"), text)
        calls = read_jsonl(os.environ["FAKE_CLAUDE_LOG"])
        self.assertEqual([c["resumed"] for c in calls], [False, False, False, True])  # drawA fresh, drawB keeps its conversation
        self.assertEqual([c["args"][c["args"].index("--model") + 1] if "--model" in c["args"] else None for c in calls],
                         ["claude-x", "claude-x", None, None])  # the team's default model never reaches a member
        self.assertEqual(read_jsonl(os.path.join(self.dir, "fake", "turns.jsonl"))[0]["model"], "m1")
        summary = {m["name"]: m for m in members.summary()}
        self.assertEqual({n: (m["turns"], m["tokens"], m["states"]) for n, m in summary.items() if n != "art"},
                         {"drawA": (2, 134, {"idle": 2}), "drawB": (2, 134, {"idle": 2})})
        self.assertEqual(summary["art"]["turns"], 1)

    def test_a_turn_that_raises_counts_as_a_failed_turn(self):
        members = teamrun.Members([dict(m, model=None, sessions="fresh") for m in FAKE_TEAM], self.dir)
        self.addCleanup(members.close)

        def boom(*args, **kw):
            raise OSError("no such program")
        members.backends["fake"].run_turn = boom
        with self.assertRaises(OSError):
            members.run_turn("drawA", "You are drawA")
        self.assertEqual(members.summary()[0]["states"], {"error": 1})


class OpenCodeMembersTest(unittest.TestCase):
    """OpenCode members: teamrun starts a herdr-py daemon of its own against the server in the spec (here the fake)."""

    def setUp(self):
        self.fake = FakeOpenCode(password="pw").start()
        self.addCleanup(self.fake.stop)
        self.dir = tempfile.mkdtemp(dir="/tmp")
        self.addCleanup(shutil.rmtree, self.dir, True)
        pw = os.path.join(self.dir, "pw")
        with open(pw, "w") as handle:
            handle.write("pw")
        self.spec = {"opencode": {"url": self.fake.url, "password_file": pw, "username": None,
                                  "policy": os.path.join(LAYOUT, "policy.json")},
                     "members": [{"name": "drawA", "role": "drawer", "backend": "opencode", "model": "ollama/x", "sessions": "fresh"},
                                 {"name": "art", "role": "art", "backend": "opencode", "model": None, "sessions": "keep"}],
                     "rounds": {"revisions": 1, "fixes": 1}}

        def answer(sid, text):
            self.fake.emit("message.part.updated", sessionID=sid, part={"id": "p" + sid, "sessionID": sid, "type": "step-finish",
                                                                         "tokens": {"total": 42}})
            self.fake.turn(sid, f"{self.fake.name_of(sid)} read: {text}")
        self.fake.on_prompt = answer

    def test_turns_go_through_a_daemon_of_the_run(self):
        proc, sock = teamrun.start_daemon(self.spec, os.path.join(self.dir, "daemon"))
        try:
            members = teamrun.Members(self.spec["members"], self.dir, socket=sock)
            replies = [members.run_turn(name, text) for name, text in (("drawA", "one"), ("drawA", "two"), ("art", "look"),
                                                                        ("art", "again"))]
            summary = {m["name"]: m for m in members.summary()}
        finally:
            teamrun.stop_daemon(proc, sock)
        self.assertEqual(replies, [("drawA read: one", "idle"), ("drawA read: two", "idle"), ("art read: look", "idle"),
                                   ("art read: again", "idle")])
        titles = [s.get("title") for s in self.fake.sessions.values()]
        self.assertEqual((titles.count("herdr-py: drawA"), titles.count("herdr-py: art")), (2, 1))  # fresh vs keep
        self.assertEqual(self.fake.prompts[0][1]["model"], {"providerID": "ollama", "modelID": "x"})
        self.assertEqual((summary["drawA"]["tokens"], summary["art"]["tokens"]), (84, 84))
        self.assertIsNotNone(proc.returncode)
        self.assertFalse(os.path.exists(os.path.dirname(sock)))

    def test_a_daemon_that_does_not_start_fails_the_run_with_its_reason(self):
        make_original(self.dir)
        with open(os.path.join(self.dir, "policy.json"), "w") as handle:
            handle.write('{"default": "maybe"}')  # the daemon refuses it and exits
        spec = write_spec(self.dir, "spec.json", task=TASK, output="run",
                          members=[{"name": "drawA", "role": "drawer", "backend": "opencode"}, FAKE_TEAM[2]],
                          opencode={"url": self.fake.url, "password_file": "pw", "policy": "policy.json"})
        code, _, _ = quiet(teamrun.main, [spec], renderer=RectRenderer())
        self.assertEqual(code, 1)
        with open(os.path.join(self.dir, "run", "run.json")) as handle:
            run = json.load(handle)
        self.assertTrue(run["error"].startswith("RuntimeError: the herdr-py daemon did not start: herdr-py serve: policy:"),
                        run["error"])
        self.assertEqual(self.fake.prompts, [])
        with open(os.path.join(self.dir, "run", "report.md")) as handle:
            self.assertIn("(the run stopped before its members were set up)", handle.read())

    def test_a_whole_run_with_an_opencode_drawer(self):
        make_original(self.dir)
        script = Script(1)

        def answer(sid, text):
            self.fake.emit("message.part.updated", sessionID=sid, part={"id": "p" + sid, "sessionID": sid, "type": "step-finish",
                                                                         "tokens": {"total": 42}})
            self.fake.turn(sid, script.reply(text)[2])
        self.fake.on_prompt = answer
        spec = write_spec(self.dir, "spec.json", task=TASK, rounds={"revisions": 0, "fixes": 1}, output="run",
                          members=[{"name": "drawA", "role": "drawer", "backend": "opencode", "model": "ollama/x"}, FAKE_TEAM[2]],
                          opencode={"url": self.fake.url, "password_file": "pw"})
        code, _, err = quiet(teamrun.main, [spec], renderer=RectRenderer())
        self.assertEqual(code, 0, err)
        with open(os.path.join(self.dir, "run", "run.json")) as handle:
            run = json.load(handle)
        drawer = run["members"][0]
        self.assertEqual(drawer["turns"], len(self.fake.prompts))  # every prompt the server got was one of drawA's turns
        self.assertEqual(drawer["tokens"], 42 * len(self.fake.prompts))  # read from the daemon before it was stopped
        self.assertGreater(len(self.fake.prompts), 3)  # three drafts and at least one fix-up
        self.assertIsNotNone(run["daemon"]["exit"])  # the run's daemon is gone, and so is its socket folder
        self.assertFalse(os.path.exists(os.path.dirname(run["daemon"]["socket"])))
        with open(run["daemon"]["log"]) as handle:
            self.assertIn("socket:", handle.read())


class RunReportTest(unittest.TestCase):
    """runreport on hand-made files: an invalid draft, a revision that is kept, a picture equal to the original."""

    def test_rows_fixups_and_an_unlimited_psnr(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        summary = {"turns": [{"turn": 1, "row": 1, "drawer": "drawA", "kind": "draft", "valid": False, "match": None, "missing": None,
                              "accepted": False},
                             {"turn": 2, "row": 1, "drawer": "drawB", "kind": "revise", "valid": True, "accepted": True, "match": 0.5,
                              "missing": ["New"]},
                             {"turn": 3, "row": 1, "drawer": "drawA", "kind": "revise", "valid": False, "accepted": False},
                             {"turn": 4, "row": 2, "drawer": "drawB", "kind": "draft", "valid": True, "match": 0.7, "missing": [],
                              "accepted": True},
                             {"turn": 5, "row": 2, "drawer": "drawA", "kind": "revise", "valid": True, "accepted": False, "match": 0.6,
                              "missing": []},
                             {"turn": 6, "row": 3, "drawer": "drawB", "kind": "draft", "valid": False, "match": None, "missing": None,
                              "accepted": False},
                             {"turn": 7, "row": 3, "drawer": "drawA", "kind": "revise", "valid": False, "accepted": False}],
                   "final": {"match": 1.0, "psnr": math.inf, "missing": []}, "lessons": {}}
        with open(os.path.join(d, "summary.json"), "w") as handle:
            json.dump(summary, handle)
        chat = [("manager", "drawA", "round 1: row 1 draft from the checklist", "message"),
                ("supervisor", "drawA", "fix: r1.x: missing \"y\"", "control"),
                ("supervisor", "drawA", "fix: again", "control"),
                ("manager", "drawB", "round 2: row 1 revise drawA's version: 0 art notes, 1 program notes", "message"),
                ("drawA", "supervisor", "fix: this is a drawer talking", "message"),
                ("supervisor", "drawB", "fix: only control lines come before a FIX prompt", "message"),
                ("manager", "drawB", "round 4: row 2 draft from the checklist", "message"),
                ("supervisor", "drawB", "fix: one", "control"),
                ("supervisor", "team", "finished", "end")]
        with open(os.path.join(d, "chat.jsonl"), "w") as handle:
            for i, (frm, to, text, kind) in enumerate(chat):
                handle.write(json.dumps({"t": 100 + 30 * i, "from": frm, "to": to, "kind": kind, "text": text}) + "\n")
        report = runreport.report_text(d)
        rows = {r[0]: r[1:] for r in table_with(report, "Row")[1:]}
        self.assertEqual(rows["1"], ["-", "0.500", "1", "0", "1", "2", "1"])
        self.assertEqual(rows["2"], ["0.700", "0.700", "0", "1", "0", "1", "0"])
        self.assertEqual(rows["3"], ["-", "-", "0", "0", "1", "0", "-"])  # no version the program could draw
        self.assertIn("Match 1.000 · PSNR inf dB", report)
        self.assertIn("(no run.json: members, tokens and time are unknown)", report)
        self.assertIn("team time 4.00 min", report)  # nine lines 30 s apart
        m = runreport.metrics(d)
        self.assertEqual((m["accepted"], m["rejected"], m["invalid"], m["fixups"], m["tokens"]), (1, 1, 2, 3, None))


if __name__ == "__main__":
    unittest.main()
