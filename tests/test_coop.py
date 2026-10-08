"""herdr_py/coop.py: cooperative loops on top of the team knowledge base. Modes S, I and C, what members are shown and
may name as parents, and the faults from the 2026-10-08 controller research: members that time out, crash, ramble,
give up or claim scores; judges that crash, hang or print nonsense; nothing an idle member answered may be lost."""
import collections
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "examples", "coop"))

from herdr_py.coop import CoopRun, command_judge, exit_judge, main, parse_reply  # noqa: E402
from herdr_py.teamkb import TeamKB  # noqa: E402

JUDGE = """
import json, sys, time
mode = {mode!r}
if mode == "crash":
    raise SystemExit(3)
if mode == "garbage":
    print("looks fine to me")
    raise SystemExit(0)
if mode == "hang":
    time.sleep(30)
text = open(sys.argv[1]).read().strip()
try:
    x = int(text)
except ValueError:
    print(json.dumps({{"status": "invalid", "score": None, "detail": "not a whole number"}}))
    raise SystemExit(0)
ok = x <= 100
print(json.dumps({{"status": "valid" if ok else "invalid", "score": x if ok else None, "detail": "ok" if ok else "over 100"}}))
"""
BEST = re.compile(r"^- (k[0-9a-f]{12}) by \S+: score \S+: [^\n]*\n```\n(-?\d+)\n```", re.M)


def answer(x, summary, parents=()):
    return f"SUMMARY: {summary}\nPARENTS: {', '.join(parents) or 'none'}\n```\n{x}\n```"


def starter(prompt):
    return answer(5, "start at five")


def adder(prompt):
    best = BEST.search(prompt)
    return answer(int(best.group(2)) + 10, "add ten", [best.group(1)]) if best else answer(1, "start at one")


class Scripted:
    """Members whose replies come from functions: reply(prompt) -> text, or (text, state), or an exception."""

    def __init__(self, **behaviour):
        self.behaviour, self.prompts, self.used = behaviour, collections.defaultdict(list), collections.Counter()

    def run_turn(self, name, prompt, timeout=600):
        self.prompts[name].append(prompt)
        self.used[name] += 10
        out = self.behaviour[name](prompt)
        return out if isinstance(out, tuple) else (out, "idle")

    def tokens(self, name):
        return self.used[name]


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def judge(self, mode="ok", timeout=20):
        path = os.path.join(self.dir, f"judge_{mode}.py")
        with open(path, "w") as handle:
            handle.write(JUDGE.format(mode=mode))
        return command_judge([sys.executable, "-B", path], timeout=timeout)

    def run_team(self, members, names, mode="C", rounds=3, judge=None, **kw):
        out = os.path.join(self.dir, "run")
        run = CoopRun("Give a whole number up to 100; higher is better.", judge or self.judge(), members, names, out,
                      mode=mode, rounds=rounds, **kw)
        return run, run.run()

    def turns(self):
        with open(os.path.join(self.dir, "run", "run.jsonl")) as handle:
            return [json.loads(line) for line in handle]


class ParseTest(unittest.TestCase):
    def test_replies(self):
        r = parse_reply("**SUMMARY:** tried a grid\n> PARENTS: kaaaaaaaaaaaa, none, kbbbbbbbbbbbb, kaaaaaaaaaaaa\n"
                        "```json\n[1]\n```\nand then\n~~~~\n[2]\n~~~~\n")
        self.assertEqual(r, {"kind": "result", "summary": "tried a grid", "answer": "[2]\n",
                             "parents": ["kaaaaaaaaaaaa", "kbbbbbbbbbbbb"]})
        self.assertEqual(parse_reply("PARENTS: none\n```\n7\n```")["summary"], "(no summary given)")
        self.assertEqual(parse_reply("FAILED: the task names no format")["kind"], "failure")
        self.assertEqual(parse_reply("I think it is 7")["kind"], None)
        self.assertEqual(parse_reply("FAILED:   ")["kind"], None)
        self.assertEqual(parse_reply("```\n7\n``` not closed on its own line")["kind"], None)


class ModesTest(Base):
    def test_cooperative_members_see_and_build_on_each_others_answers(self):
        members = Scripted(starter=starter, adder=adder)
        run, s = self.run_team(members, ["starter", "adder"], mode="C", rounds=3)
        self.assertEqual((s["best"], s["best_member"], s["turns"], s["valid"], s["repeats"]), (25, "adder", 6, 4, 2))
        self.assertEqual((s["adoption_rate"], s["improved_after_adoption"], s["tokens"]), (0.25, 1.0, 60))
        self.assertEqual([p["best"] for p in s["progress"]], [5, 15, 25])
        second = members.prompts["adder"][1]
        self.assertIn("This is round 2 of 3.", second)
        self.assertIn("```\n5\n```", second)  # the teammate's answer itself, so it can be built on
        built = [t for t in self.turns() if t["member"] == "adder" and t["round"] == 2][0]
        first = TeamKB(os.path.join(self.dir, "run", "kb")).entries()[0]
        self.assertEqual((first["member"], built["parents"]), ("starter", [first["id"]]))

    def test_independent_members_never_see_each_other(self):
        members = Scripted(starter=starter, adder=adder)
        run, s = self.run_team(members, ["starter", "adder"], mode="I", rounds=3)
        self.assertEqual((s["best"], s["best_member"], s["adoption_rate"]), (21, "adder", 0.0))
        for prompt in members.prompts["adder"]:
            self.assertNotIn("start at five", prompt)
            self.assertNotIn("one of 2 members", prompt)

    def test_a_member_cannot_claim_a_parent_it_was_not_shown(self):
        five = "k" + hashlib.sha256(("starter\0result\0" + hashlib.sha256(b"5\n").hexdigest() + "\0start at five")
                                    .encode()).hexdigest()[:12]
        members = Scripted(starter=starter, cheat=lambda p: answer(50, "built on the starter", [five]))
        run, s = self.run_team(members, ["starter", "cheat"], mode="I", rounds=2)
        cheats = [t for t in self.turns() if t["member"] == "cheat"]
        self.assertEqual([(t["parents"], t.get("parents_dropped")) for t in cheats], [([], [five]), ([], [five])])
        self.assertEqual(s["parents_dropped"], 2)
        self.assertEqual(s["adoption_rate"], 0.0)

    def test_single_mode_gives_every_turn_to_the_first_member(self):
        members = Scripted(adder=adder, starter=starter)
        run, s = self.run_team(members, ["adder", "starter"], mode="S", rounds=2)
        self.assertEqual((s["members"], s["turns"], s["best"]), (["adder"], 4, 31))
        self.assertEqual(len(members.prompts["starter"]), 0)
        self.assertIn("This is turn 4 of 4.", members.prompts["adder"][-1])
        self.assertIn("You work on this task alone.", members.prompts["adder"][0])

    def test_bad_settings_are_refused(self):
        for kwargs, words in ((dict(mode="X"), "mode"), (dict(names=[]), "no members"), (dict(rounds=0), "rounds")):
            args = dict(task="t", judge=None, members=None, names=["a"], out=self.dir)
            args.update(kwargs)
            with self.assertRaisesRegex(ValueError, words):
                CoopRun(**args)


class FaultTest(Base):
    def test_turns_that_fail_leave_no_answer_and_nothing_answered_is_lost(self):
        def crash(prompt):
            raise RuntimeError("backend broke")
        members = Scripted(sleeper=lambda p: ("", "timeout"), crasher=crash, rambler=lambda p: "I think the answer is 7",
                           quitter=lambda p: "FAILED: the task does not say how to answer", liar=lambda p: answer(7, "this scores 1000"),
                           shaky=lambda p: (answer(9, "an answer from a turn that broke"), "error"))
        names = ["sleeper", "crasher", "rambler", "quitter", "liar", "shaky"]
        run, s = self.run_team(members, names, mode="C", rounds=1)
        turns = {t["member"]: t for t in self.turns()}
        self.assertEqual(turns["sleeper"]["problem"], "the turn ended timeout")
        self.assertEqual(turns["crasher"]["state"], "error")
        self.assertIn("RuntimeError: backend broke", turns["crasher"]["problem"])
        self.assertEqual(turns["rambler"]["problem"], "no fenced answer and no FAILED line")
        self.assertEqual(turns["quitter"]["kind"], "failure")
        self.assertEqual((turns["liar"]["status"], turns["liar"]["score"]), ("valid", 7))  # the judge's score, not 1000
        self.assertNotIn("entry", turns["shaky"])  # an answer from a turn that did not end normally is not taken
        entries = TeamKB(os.path.join(self.dir, "run", "kb")).entries()
        # nothing lost: the two idle turns that answered (or gave up) are exactly the two entries
        self.assertEqual(sorted((e["member"], e["id"]) for e in entries),
                         sorted((t["member"], t["entry"]) for t in self.turns() if "entry" in t))
        self.assertEqual(sorted(e["member"] for e in entries), ["liar", "quitter"])
        self.assertEqual((s["no_answer"], s["failures_reported"], s["answers"]), (1, 1, 1))
        self.assertEqual(s["turn_states"], {"timeout": 1, "error": 2, "idle": 3})

    def test_a_judge_that_breaks_is_not_an_invalid_answer(self):
        for mode, words in (("crash", "exited with code 3"), ("garbage", "Expecting value"), ("hang", "did not finish")):
            with self.subTest(mode=mode):
                shutil.rmtree(os.path.join(self.dir, "run"), True)
                run, s = self.run_team(Scripted(a=lambda p: answer(7, "seven")), ["a"], rounds=1,
                                       judge=self.judge(mode, timeout=2))
                self.assertEqual((s["judge_errors"], s["invalid"], s["valid"]), (1, 0, 0))
                self.assertEqual(self.turns()[0]["status"], "infra_error")
                self.assertIn(words, TeamKB(os.path.join(self.dir, "run", "kb")).entries()[0]["detail"])

    def test_a_broken_answer_is_invalid_and_shown_as_a_failure(self):
        members = Scripted(a=lambda p: answer(500, "aim high"), b=adder)
        run, s = self.run_team(members, ["a", "b"], rounds=2)
        self.assertEqual(s["invalid"], 1)
        self.assertIn("aim high (over 100)", members.prompts["b"][1])

    def test_a_repeated_answer_is_not_judged_again(self):
        calls = []
        judge = self.judge()

        def counting(path):
            calls.append(path)
            return judge(path)
        run, s = self.run_team(Scripted(a=starter), ["a"], rounds=3, judge=counting)
        self.assertEqual((len(calls), s["repeats"], s["valid"]), (1, 2, 1))


class ExitJudgeTest(Base):
    def test_pass_fail_checks(self):
        path = os.path.join(self.dir, "check.py")
        with open(path, "w") as handle:
            handle.write("import sys\nok = open(sys.argv[1]).read().strip() == 'yes'\nprint('looked at it')\nraise SystemExit(0 if ok else 1)\n")
        check = exit_judge([sys.executable, "-B", path])
        for text, expected in (("yes", ("valid", 1)), ("no", ("invalid", None))):
            answer_file = os.path.join(self.dir, "a.txt")
            with open(answer_file, "w") as handle:
                handle.write(text)
            status, score, detail = check(answer_file)
            self.assertEqual((status, score), expected)
            self.assertEqual(detail, "looked at it")
        self.assertEqual(check(None)[0], "invalid")


class CommandLineTest(Base):
    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_a_run_from_the_command_line(self):
        task = os.path.join(self.dir, "task.md")
        with open(task, "w") as handle:
            handle.write("Give a whole number up to 100.")
        member = os.path.join(self.dir, "member.py")
        with open(member, "w") as handle:
            handle.write(textwrap.dedent("""
                import sys
                prompt = sys.stdin.read()
                print("SUMMARY: always 42" if "Task:" in prompt else "SUMMARY: no task")
                print("PARENTS: none")
                print("```")
                print(42)
                print("```")
                """))
        judge = os.path.join(self.dir, "judge.py")
        with open(judge, "w") as handle:
            handle.write(JUDGE.format(mode="ok"))
        out = os.path.join(self.dir, "cli")
        args = ["--task", task, "--judge", f"{sys.executable} -B {judge}", "--out", out, "--rounds", "2",
                "--member", f"a=command:{sys.executable} -B {member}"]
        code, stdout, _ = self.cli(*args)
        self.assertEqual(code, 0, stdout)
        self.assertIn("best 42 (", stdout)
        self.assertIn("1 repeats", stdout)
        for name in ("run.jsonl", "summary.json", os.path.join("kb", "events.jsonl")):
            self.assertTrue(os.path.exists(os.path.join(out, name)), name)
        code, _, err = self.cli(*args)  # the same folder again: refused, so two runs never mix
        self.assertEqual(code, 2)
        self.assertIn("already holds a run", err)
        code, _, err = self.cli(*(args[:-2] + ["--member", "a=gpt"]))
        self.assertEqual(code, 2)
        self.assertIn("one of", err)
        code, _, err = self.cli("--task", os.path.join(self.dir, "missing.md"), "--judge", "x", "--out",
                                os.path.join(self.dir, "other"), "--member", "a=codex")
        self.assertEqual(code, 2)


class PackingExampleTest(Base):
    def test_the_judge_checks_exactly(self):
        from packing_judge import judge
        grid = [[0.1 + 0.2 * i, 0.1 + 0.2 * j, 0.1] for i in range(5) for j in range(5)]
        self.assertEqual(judge(json.dumps(grid), n=25)[:2], ("invalid", None))  # 0.1 + 0.2 * 4 + 0.1 > 1 in floats
        fits = [[x, y, 0.0999999] for x, y, _ in grid]
        self.assertEqual(judge(json.dumps(fits), n=25)[0], "valid")
        for bad, words in ((fits[:24], "a list of 25"), ([[0.5, 0.5, 0.1]] * 25, "overlap"),
                           ([[0.05, 0.5, 0.1]] + fits[1:], "not inside"), ([[0.5, 0.5, True]] + fits[1:], "finite numbers"),
                           ([[0.5, 0.5, -0.1]] + fits[1:], "positive")):
            self.assertIn(words, judge(json.dumps(bad), n=25)[2])
        self.assertIn("finite", judge(json.dumps(fits).replace("0.0999999", "NaN", 1), n=25)[2])
        self.assertIn("not JSON", judge("[[0.5, 0.5", n=25)[2])

    def test_program_members_improve_on_each_other(self):
        from herdr_py.members import Members, parse_member
        prog = os.path.join(ROOT, "examples", "coop", "packing_member.py")
        specs = [parse_member(f"m{s}=command:{sys.executable} -B {prog} --seed {s} --steps 800") for s in (1, 2)]
        members = Members(specs, os.path.join(self.dir, "members"))
        with open(os.path.join(ROOT, "examples", "coop", "packing_task.md")) as handle:
            task = handle.read()
        judge = command_judge([sys.executable, "-B", os.path.join(ROOT, "examples", "coop", "packing_judge.py")])
        s = CoopRun(task, judge, members, members.names(), os.path.join(self.dir, "run"), mode="C", rounds=2).run()
        self.assertEqual((s["valid"], s["invalid"], s["judge_errors"], s["no_answer"]), (4, 0, 0, 0))
        self.assertGreater(s["best"], 2.5414)  # better than the grid it starts from
        self.assertGreater(s["adoption_rate"], 0)


if __name__ == "__main__":
    unittest.main()
