"""examples/self_improve: the patch judge and the task maker on a small repository built here, and a whole cooperative
run whose members hand in known patches. Covers what a patch may not do (touch the scenarios, the judge or an
existing test, leave the repo, break a test, fail to apply) and what makes the judge itself fail (a broken base,
a scenario that already passes), which must never look like an invalid patch."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXAMPLE = os.path.join(ROOT, "examples", "self_improve")
sys.path.insert(0, ROOT)
sys.path.insert(0, EXAMPLE)

import make_task  # noqa: E402
import patch_judge  # noqa: E402
from herdr_py.coop import CoopRun, command_judge  # noqa: E402
from herdr_py.members import Members, parse_member  # noqa: E402

CALC = '''def clamp(x, lo, hi):
    if x < lo:
        return lo
    return x
'''
TEST = '''import unittest
from calc import clamp


class T(unittest.TestCase):
    def test_low(self):
        self.assertEqual(clamp(-5, 0, 10), 0)

    def test_inside(self):
        self.assertEqual(clamp(5, 0, 10), 5)
'''
SCENARIO_HIGH = '''import unittest
from calc import clamp


class High(unittest.TestCase):
    def test_high(self):
        self.assertEqual(clamp(50, 0, 10), 10)
'''
SCENARIO_ORDER = '''import unittest
from calc import clamp


class Order(unittest.TestCase):
    def test_swapped_limits_are_refused(self):
        with self.assertRaises(ValueError):
            clamp(5, 10, 0)
'''
FIX_HIGH = '''--- a/calc.py
+++ b/calc.py
@@ -1,4 +1,6 @@
 def clamp(x, lo, hi):
     if x < lo:
         return lo
+    if x > hi:
+        return hi
     return x
'''
FIX_BOTH = '''diff --git a/calc.py b/calc.py
--- a/calc.py
+++ b/calc.py
@@ -1,4 +1,8 @@
 def clamp(x, lo, hi):
+    if lo > hi:
+        raise ValueError("lo > hi")
     if x < lo:
         return lo
+    if x > hi:
+        return hi
     return x
'''
BREAKS = '''--- a/calc.py
+++ b/calc.py
@@ -1,4 +1,4 @@
 def clamp(x, lo, hi):
     if x < lo:
-        return lo
+        return hi
     return x
'''
NOTHING = '''--- a/README
+++ b/README
@@ -1 +1,2 @@
 calc
+(a harmless note)
'''


def git(repo, *args):
    subprocess.run(["git", "-C", repo] + list(args), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


@unittest.skipUnless(shutil.which("git"), "the patch judge and these tests need git (the RHEL 8 test image has none)")
class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.repo = os.path.join(self.dir, "repo")
        files = {"calc.py": CALC, "tests/test_calc.py": TEST, "README": "calc\n",
                 "scen/scenario.json": json.dumps({"scenarios": [
                     {"name": "high", "file": "test_high.py", "status": "frozen", "sources": ["calc.py"],
                      "why": "values above hi must come back as hi"},
                     {"name": "order", "file": "test_order.py", "status": "frozen", "sources": ["calc.py", "README"]},
                     {"name": "later", "file": "test_later.py", "status": "pending-review", "sources": ["calc.py"]}]}),
                 "scen/test_high.py": SCENARIO_HIGH, "scen/test_order.py": SCENARIO_ORDER,
                 "scen/test_later.py": SCENARIO_HIGH.replace("High", "Later")}
        for path, text in files.items():
            full = os.path.join(self.repo, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as handle:
                handle.write(text)
        git(self.repo, "init", "-q")
        git(self.repo, "add", ".")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "base")
        with open(os.path.join(self.repo, "calc.py"), "w") as handle:  # an uncommitted change: never judged
            handle.write(CALC.replace("return x", "return x  # working tree only"))
        self.test_cmd = f"{sys.executable} -B -m unittest discover -s tests -q"

    def args(self, *extra):
        return ["--repo", self.repo, "--base", "HEAD", "--scenarios", os.path.join(self.repo, "scen"),
                "--test", self.test_cmd, "--timeout", "60"] + list(extra)

    def patch(self, text, name="answer.diff"):
        path = os.path.join(self.dir, name)
        with open(path, "w") as handle:
            handle.write(text)
        return path

    def judge(self, text, *extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = patch_judge.main(self.args(*extra) + [self.patch(text)])
        return code, (json.loads(out.getvalue()) if code == 0 else None), err.getvalue()


class JudgeTest(Base):
    def test_scores_count_the_scenarios_a_patch_makes_pass(self):
        code, v, _ = self.judge(FIX_HIGH)
        self.assertEqual((code, v["status"], v["score"]), (0, "valid", 1))
        self.assertIn("1 of 2 scenario(s) pass: high; still failing: order", v["detail"])
        self.assertEqual(self.judge(FIX_BOTH)[1]["score"], 2)
        v = self.judge(NOTHING)[1]
        self.assertEqual((v["status"], v["score"]), ("valid", 0))
        self.assertRegex(v["detail"], r"^0 of 2 scenario\(s\) pass; still failing: "  # names differ across Pythons
                         r"high \(FAIL: test_high \(test_high\.High[.\w]*\) \| AssertionError: 50 != 10\); "
                         r"order \(FAIL: test_swapped_limits_are_refused \([.\w]+\) \| AssertionError: ValueError not raised\)$")

    def test_pending_scenarios_are_used_only_when_asked(self):
        self.assertEqual(self.judge(FIX_BOTH, "--allow-pending")[1]["score"], 3)

    def test_a_patch_that_breaks_an_existing_test_is_invalid(self):
        code, v, _ = self.judge(BREAKS)
        self.assertEqual((code, v["status"], v["score"]), (0, "invalid", None))
        self.assertIn("breaks the existing tests", v["detail"])

    def test_protected_files(self):
        cases = {
            "the scenarios": FIX_HIGH.replace("calc.py", "scen/test_high.py"),
            "the judge": FIX_HIGH.replace("calc.py", "examples/self_improve/patch_judge.py"),
            "changes an existing test": "--- a/tests/test_calc.py\n+++ b/tests/test_calc.py\n@@ -1 +1 @@\n-import unittest\n+import unittest  # \n",
            "deletes an existing test": "diff --git a/tests/test_calc.py b/tests/test_calc.py\ndeleted file mode 100644\n--- a/tests/test_calc.py\n+++ /dev/null\n",
            "renames into the scenarios": "diff --git a/README b/scen/README\nsimilarity index 100%\nrename from README\nrename to scen/README\n",
            "leaves the repo": FIX_HIGH.replace("a/calc.py", "a/../outside.py"),
            "an absolute path": FIX_HIGH.replace("+++ b/calc.py", "+++ /etc/calc.py"),
        }
        words = {"the scenarios": "scenarios are the task", "the judge": "judge and the task maker",
                 "changes an existing test": "changes an existing test", "deletes an existing test": "changes an existing test",
                 "renames into the scenarios": "scenarios are the task", "leaves the repo": "inside the repo",
                 "an absolute path": "inside the repo"}
        for name, text in cases.items():
            with self.subTest(name):
                code, v, _ = self.judge(text)
                self.assertEqual((code, v["status"]), (0, "invalid"), v)
                self.assertIn(words[name], v["detail"])

    def test_a_new_test_file_is_fine(self):
        new = ("diff --git a/tests/test_more.py b/tests/test_more.py\nnew file mode 100644\n--- /dev/null\n+++ b/tests/test_more.py\n"
               "@@ -0,0 +1,2 @@\n+import unittest\n+from calc import clamp\n")
        self.assertEqual(self.judge(FIX_HIGH + new)[1]["score"], 1)

    def test_patches_that_do_not_apply(self):
        for name, text, words in (("empty", "  \n", "empty"), ("prose", "I changed calc.py to clamp the top.", "not a unified diff"),
                                  ("stale", FIX_HIGH.replace("    if x < lo:", "    if x <= lo:"), "git apply --check failed")):
            with self.subTest(name):
                v = self.judge(text)[1]
                self.assertEqual(v["status"], "invalid")
                self.assertIn(words, v["detail"])

    def test_the_working_tree_is_never_judged(self):  # setUp left an uncommitted edit on calc.py that FIX_HIGH would not fit
        work = FIX_HIGH.replace("     return x\n", "     return x  # working tree only\n")
        self.assertIn("git apply --check failed", self.judge(work)[1]["detail"])

    def test_a_broken_base_or_a_scenario_that_already_passes_is_the_judge_failing(self):
        code, v, err = self.judge(FIX_HIGH, "--test", f"{sys.executable} -c 'raise SystemExit(1)'")
        self.assertEqual((code, v), (2, None))
        self.assertIn("the base itself does not pass its tests", err)
        with open(os.path.join(self.repo, "scen", "test_order.py"), "w") as handle:
            handle.write(SCENARIO_HIGH.replace("50, 0, 10), 10", "5, 0, 10), 5"))  # passes today
        git(self.repo, "add", "scen")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "weak")
        code, v, err = self.judge(FIX_HIGH)
        self.assertEqual(code, 2)
        self.assertIn("every time or sometimes, test nothing reliably: order", err)
        code, v, err = self.judge(FIX_HIGH, "--no-preflight")  # without the preflight, nothing checks the scenarios
        self.assertEqual((code, v["score"]), (0, 2))

    def test_copies_are_made_under_the_work_root_and_removed(self):
        root = os.path.join(self.dir, "copies")
        seen = os.path.join(self.dir, "seen.txt")
        code, v, _ = self.judge(FIX_HIGH, "--work-root", root, "--test", f"pwd >> {seen} && {self.test_cmd}")
        self.assertEqual((code, v["score"]), (0, 1))
        with open(seen) as handle:
            places = handle.read().split()
        self.assertEqual(len(places), 2)  # the preflight's copy and the patched one
        self.assertTrue(all(os.path.realpath(p).startswith(os.path.realpath(root) + os.sep) for p in places), places)
        self.assertEqual(os.listdir(root), [])  # nothing left behind

    def test_a_scenario_that_passes_only_sometimes_earns_nothing(self):
        flip = os.path.join(self.dir, "flip")
        with open(os.path.join(self.repo, "scen", "test_flaky.py"), "w") as handle:  # passes on every other run
            handle.write(SCENARIO_ORDER.replace("    def test_swapped_limits_are_refused(self):\n",
                         "    def test_swapped_limits_are_refused(self):\n"
                         f"        import os\n        f = {flip!r}\n        n = int(open(f).read()) if os.path.exists(f) else 0\n"
                         "        open(f, 'w').write(str(n + 1))\n        if n % 2:\n            return\n"))
        spec = os.path.join(self.repo, "scen", "scenario.json")
        with open(spec) as handle:
            data = json.load(handle)
        data["scenarios"].append({"name": "flaky", "file": "test_flaky.py", "status": "frozen", "sources": ["calc.py"]})
        with open(spec, "w") as handle:
            json.dump(data, handle)
        git(self.repo, "add", "scen")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "flaky")
        code, v, err = self.judge(NOTHING, "--repeat", "2")  # on the base it passes once in two runs: not a task
        self.assertEqual(code, 2)
        self.assertIn("every time or sometimes, test nothing reliably: flaky", err)
        code, v, _ = self.judge(FIX_HIGH, "--no-preflight", "--repeat", "2")
        self.assertEqual(v["score"], 1)  # high passes both runs; flaky (unfixed) passes one of two: no credit
        self.assertIn("flaky (flaky: passed 1 of 2 runs;", v["detail"])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):  # 0 runs would let every scenario pass
            patch_judge.main(self.args("--repeat", "0") + [self.patch(NOTHING)])
        code, v, _ = self.judge(FIX_HIGH, "--no-preflight", "--repeat", "1")
        self.assertIn(v["score"], (1, 2))  # one run: a flaky scenario can look fixed

    def test_check_and_missing_scenarios(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(patch_judge.main(self.args("--check")), 0)
        self.assertIn("2 scenario(s) fail on HEAD", out.getvalue())
        os.remove(os.path.join(self.repo, "scen", "test_high.py"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(patch_judge.main(self.args("--check")), 2)
        self.assertIn("test_high.py is missing", err.getvalue())

    def test_touched_paths(self):
        self.assertEqual(patch_judge.touched(FIX_BOTH), {"calc.py"})
        self.assertEqual(patch_judge.touched('diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"\n'), {"café.py"})
        self.assertEqual(patch_judge.touched("--- a/x.py\t2026-10-08\n+++ b/y.py\t2026-10-08\n"), {"x.py", "y.py"})


class TaskTest(Base):
    def test_the_task_carries_the_rules_the_scenarios_and_the_sources_from_git(self):
        text = make_task.build(self.repo, "HEAD", os.path.join(self.repo, "scen"))
        self.assertIn("2 scenario test(s)", text)
        self.assertIn("## Scenario high\nvalues above hi must come back as hi", text)
        self.assertIn("## Scenario order\n\n```\n" + SCENARIO_ORDER.rstrip() + "\n```", text)
        self.assertIn("### calc.py\n```\n" + CALC.rstrip() + "\n```", text)  # from git: not the working tree's edit
        self.assertNotIn("working tree only", text)
        self.assertEqual(text.count("### calc.py"), 1)  # a source named by two scenarios is shown once
        self.assertNotIn("Later", text)

    def test_failing_loudly(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(make_task.main(["--repo", self.repo, "--scenarios", os.path.join(self.repo, "scen"),
                                             "--max-bytes", "100"]), 2)
        self.assertIn("over --max-bytes 100", err.getvalue())
        spec = os.path.join(self.repo, "scen", "scenario.json")
        with open(spec) as handle:
            data = json.load(handle)
        data["scenarios"][0]["sources"].append("missing.py")
        with open(spec, "w") as handle:
            json.dump(data, handle)
        with self.assertRaisesRegex(ValueError, "missing.py is not in HEAD"):
            make_task.build(self.repo, "HEAD", os.path.join(self.repo, "scen"))


class LoopTest(Base):
    def test_a_cooperative_run_of_members_handing_in_known_patches(self):
        fix, breaks, both = self.patch(FIX_HIGH, "fix.diff"), self.patch(BREAKS, "breaks.diff"), self.patch(FIX_BOTH, "both.diff")
        member = os.path.join(EXAMPLE, "patch_member.py")
        specs = [parse_member(f"a=command:{sys.executable} -B {member} {breaks} {fix}"),
                 parse_member(f"b=command:{sys.executable} -B {member} {both}")]
        team = Members(specs, os.path.join(self.dir, "work"), cwd=self.dir)
        judge_cmd = [sys.executable, "-B", os.path.join(EXAMPLE, "patch_judge.py")] + self.args()
        out = os.path.join(self.dir, "run")
        task = make_task.build(self.repo, "HEAD", os.path.join(self.repo, "scen"))
        s = CoopRun(task, command_judge(judge_cmd, timeout=120), team, team.names(), out, mode="I", rounds=2,
                    answer_name="answer.diff", stop_on_infra_error=True).run()
        self.assertIsNone(s["stopped"])
        self.assertEqual(s["best"], 2)
        self.assertEqual(s["best_member"], "b")
        self.assertEqual((s["valid"], s["invalid"]), (2, 1))  # a: breaks (invalid) then fix (1); b: both (2), resent
        self.assertEqual(s["repeats"], 1)
        self.assertTrue(os.path.exists(os.path.join(out, "view.html")))


if __name__ == "__main__":
    unittest.main()
