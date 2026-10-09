"""DAG dispatch (dag.py): plans are checked before anything runs; a step starts only after every step it needs has
passed, works in its own clone that holds the output commits of exactly those steps, and is judged by a program the
member cannot change; failures retry from the failed commit, block what needs them, or stop the run when the setup
broke; a run can be resumed without running passed steps again, and re-judged in fresh clones.
Members here are programs (command members), so no model is called."""
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
sys.path.insert(0, os.path.dirname(HERE))

from herdr_py import dag  # noqa: E402

HAVE_GIT = shutil.which("git") is not None

# A member program: what it does is written per step and attempt in DAG_TEST_SCRIPT; every prompt goes to DAG_TEST_LOG.
MEMBER = r'''
import json, os, re, subprocess, sys, time
prompt = sys.stdin.read()
node = re.search(r"doing step (\S+) of the plan", prompt).group(1)
log = os.environ["DAG_TEST_LOG"]
os.makedirs(log, exist_ok=True)
n = len([f for f in os.listdir(log) if f.startswith(node + "-")])
with open(os.path.join(log, "%s-%d.txt" % (node, n + 1)), "w") as handle:
    handle.write(prompt)
plan = json.load(open(os.environ["DAG_TEST_SCRIPT"])).get(node, [])
act = plan[min(n, len(plan) - 1)] if plan else {}
time.sleep(act.get("sleep", 0))
seen = []
for name, text in act.get("write", {}).items():
    with open(name, "w") as handle:
        handle.write(text)
for args in act.get("git", []):
    subprocess.run(["git"] + args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   env=dict(os.environ, GIT_AUTHOR_NAME="m", GIT_AUTHOR_EMAIL="m@x", GIT_COMMITTER_NAME="m", GIT_COMMITTER_EMAIL="m@x"))
if act.get("look"):
    refs = subprocess.run(["git", "for-each-ref", "--format=%(refname)"], stdout=subprocess.PIPE, universal_newlines=True).stdout.split()
    seen = ["refs: " + " ".join(sorted(refs)), "files: " + " ".join(sorted(f for f in os.listdir(".") if not f.startswith(".")))]
if act.get("append_to"):
    with open(act["append_to"], "a") as handle:
        handle.write("\n# changed by a member\n")
print(act.get("reply", "did " + node))
print("\n".join(seen))
sys.exit(act.get("exit", 0))
'''

# A judge: its rules per step come from DAG_TEST_JUDGE; the reply file is the last argument (coop.py's contract).
JUDGE = r'''
import json, os, sys
rules = json.loads(os.environ.get("DAG_TEST_JUDGE", "{}")).get(os.environ["HERDR_DAG_NODE"], {})
reply = open(sys.argv[-1]).read()
if rules.get("garbage"):
    print("not json")
    sys.exit(0)
if rules.get("status"):
    print(json.dumps({"status": rules["status"], "score": 1}))
    sys.exit(0)
problems = []
for name, text in rules.get("files", {}).items():
    if not os.path.isfile(name) or text not in open(name).read():
        problems.append("%s should contain %r" % (name, text))
for name in rules.get("absent", []):
    if os.path.exists(name):
        problems.append("%s should not be there" % name)
if rules.get("reply") and rules["reply"] not in reply:
    problems.append("the reply should say %r" % rules["reply"])
if rules.get("env") and not os.environ.get(rules["env"]):
    problems.append("%s is not set" % rules["env"])
print(json.dumps({"status": "invalid" if problems else "valid", "score": 1 if not problems else 0,
                  "detail": "; ".join(problems) or "ok"}))
'''


def sh(cwd, *args):
    return subprocess.run(list(args), cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True, env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
                                                            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")).stdout.strip()


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.plans = os.path.join(self.dir, "plan")
        os.makedirs(os.path.join(self.plans, "tasks"))
        for name, body in (("member.py", MEMBER), ("judge.py", JUDGE)):
            with open(os.path.join(self.plans, name), "w") as handle:
                handle.write(textwrap.dedent(body))
        self.log = os.path.join(self.dir, "prompts")
        self.script_path = os.path.join(self.dir, "script.json")
        saved = {k: os.environ.get(k) for k in ("DAG_TEST_LOG", "DAG_TEST_SCRIPT", "DAG_TEST_JUDGE", "FLAKY")}
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v) for k, v in saved.items()])
        os.environ.update(DAG_TEST_LOG=self.log, DAG_TEST_SCRIPT=self.script_path)
        self.script({})
        self.judge_rules({})

    def script(self, steps):
        with open(self.script_path, "w") as handle:
            json.dump(steps, handle)

    def judge_rules(self, rules):
        os.environ["DAG_TEST_JUDGE"] = json.dumps(rules)

    def repo(self):
        repo = os.path.join(self.dir, "repo")
        os.makedirs(repo)
        sh(repo, "git", "init", "-q")
        sh(repo, "git", "symbolic-ref", "HEAD", "refs/heads/main")  # the same branch name whatever git's default is (CI's is master)
        with open(os.path.join(repo, "base.txt"), "w") as handle:
            handle.write("base\n")
        sh(repo, "git", "add", "-A")
        sh(repo, "git", "commit", "-q", "-m", "base")
        return repo

    def plan(self, nodes, repo=True, name="p", **extra):
        for n in nodes:
            n.setdefault("member", "m%s=command:%s -B member.py" % (n["id"], sys.executable))
            n.setdefault("judge", "%s -B judge.py" % sys.executable)
            n.setdefault("task", "tasks/%s.md" % n["id"])
            path = os.path.join(self.plans, n["task"])
            if not os.path.exists(path):
                with open(path, "w") as handle:
                    handle.write("Do step %s well.\n" % n["id"])
        body = dict(extra, name=name, nodes=nodes)
        if repo:
            body["repo"] = os.path.relpath(self.repo() if repo is True else repo, self.plans)
        path = os.path.join(self.plans, "plan.json")
        with open(path, "w") as handle:
            json.dump(body, handle)
        return path

    def run_main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = dag.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def prompts(self, node):
        return [open(os.path.join(self.log, f)).read() for f in sorted(os.listdir(self.log)) if f.startswith(node + "-")]

    def events(self, out):
        with open(os.path.join(out, "events.jsonl")) as handle:
            return [json.loads(line) for line in handle]

    def summary(self, out):
        with open(os.path.join(out, "summary.json")) as handle:
            return json.load(handle)


class PlanTest(Base):
    def test_every_mistake_in_a_plan_is_reported_at_once(self):
        path = os.path.join(self.plans, "bad.json")
        with open(path, "w") as handle:
            json.dump({"nodes": [
                {"id": "A", "member": "a=codex", "task": "tasks/none.md", "judge": "x", "retries": -1, "colour": "red"},
                {"id": "A", "member": "a=codex", "task": "t", "judge": "x"},
                {"id": "B", "member": "a=claude", "task": "t", "needs": ["B", "Z"], "access": "admin", "judge_mode": "loud"},
                {"id": "9C", "member": "c=codex", "task": "t", "judge": "x"},
                {"id": "D", "member": "nobody", "task": "t", "judge": ""},
                {"id": "E", "member": "e=codex", "task": "t", "judge": "x", "needs": ["D"], "start": "merge"},
                {"id": "F", "member": "f=codex", "task": "t", "judge": "x", "start": "Q"}]}, handle)
        with self.assertRaises(dag.PlanError) as caught:
            dag.load_plan(path)
        said = str(caught.exception)
        for words in ("task file tasks/none.md not found", "retries is a whole number", "unknown fields colour",
                      "two nodes have this id", "judge is required", "needs itself", "needs Z, which is not a node",
                      "access is write or read", "judge_mode is json or exit", "node 4: the id is letters",
                      "member a is not the same as in node A", "NAME=BACKEND", "node E: start merge needs two or more",
                      "node F: start is base, merge or one of its needs", "start only means something with a repo"):
            self.assertIn(words, said)

    def test_a_cycle_is_refused_and_named(self):
        path = self.plan([{"id": "A", "needs": ["C"]}, {"id": "B", "needs": ["A"]}, {"id": "C", "needs": ["B"]}], repo=False)
        with self.assertRaises(dag.PlanError) as caught:
            dag.load_plan(path)
        self.assertIn("the needs make a cycle: A -> C -> B -> A", str(caught.exception))

    def test_levels_and_the_check_command(self):
        path = self.plan([{"id": "A"}, {"id": "B", "needs": ["A"]}, {"id": "C", "needs": ["A"]}, {"id": "D", "needs": ["C", "B"]}],
                         repo=False)
        plan = dag.load_plan(path)
        self.assertEqual(dag.levels(plan), {"A": 0, "B": 1, "C": 1, "D": 2})
        code, out, _ = self.run_main(path, "--check")
        self.assertEqual(code, 0)
        self.assertIn("level 2: D (mD, after C, B)", out)

    def test_judge_and_member_files_resolve_against_the_plan_folder(self):
        plan = dag.load_plan(self.plan([{"id": "A", "judge": "python3 judge.py --strict"}], repo=False))
        node = plan["nodes"]["A"]
        self.assertEqual(node["judge_argv"], ["python3", os.path.join(self.plans, "judge.py"), "--strict"])
        self.assertIn(os.path.join(self.plans, "member.py"), node["member"]["command"])  # run in a clone, found anyway

    def test_a_repository_is_checked_and_opencode_members_can_use_one(self):
        with self.assertRaises(dag.PlanError) as caught:
            dag.load_plan(self.plan([{"id": "A"}], repo=os.path.join(self.dir, "plan")))
        # a machine without git (the RHEL 8 test image) is told so instead of crashing
        self.assertIn("is not a git repository" if HAVE_GIT else "git is not installed", str(caught.exception))
        if HAVE_GIT:
            plan = dag.load_plan(self.plan([{"id": "A", "member": "o=opencode"}]))
            self.assertEqual(plan["nodes"]["A"]["member"]["backend"], "opencode")


@unittest.skipUnless(HAVE_GIT, "git is not installed")
class RunTest(Base):
    def test_two_independent_steps_then_one_that_builds_on_both(self):
        self.script({"W1": [{"write": {"a.txt": "A\n"}, "look": True, "sleep": 1, "reply": "wrote a.txt"}],
                     "W2": [{"write": {"b.txt": "B\n"}, "look": True, "sleep": 1, "reply": "wrote b.txt"}],
                     "W3": [{"look": True, "git": [["merge", "-q", "--no-edit", "refs/dag/W1", "refs/dag/W2"]], "reply": "merged"}]})
        self.judge_rules({"W1": {"files": {"a.txt": "A"}, "absent": ["b.txt"]}, "W2": {"files": {"b.txt": "B"}, "absent": ["a.txt"]},
                          "W3": {"files": {"a.txt": "A", "b.txt": "B", "base.txt": "base"}}})
        path = self.plan([{"id": "W1"}, {"id": "W2"}, {"id": "W3", "needs": ["W1", "W2"]}])
        repo = os.path.join(self.dir, "repo")
        refs_before = sh(repo, "git", "for-each-ref")
        out = os.path.join(self.dir, "run")
        code, said, err = self.run_main(path, "--out", out, "--parallel", "2")
        self.assertEqual(code, 0, said + err)
        s = self.summary(out)
        self.assertEqual((s["passed"], s["failed"], s["blocked"], s["released_early"], s["dispatched_after_pass"]), (3, 0, 0, 0, 0))
        events = self.events(out)
        seq = {(e["kind"], e.get("node")): e["seq"] for e in events}
        # W1 and W2 ran at the same time; W3 started only after both had passed
        self.assertLess(seq[("node.dispatch", "W2")], seq[("node.return", "W1")])
        self.assertGreater(seq[("node.dispatch", "W3")], max(seq[("node.pass", "W1")], seq[("node.pass", "W2")]))
        self.assertGreater(s["parallelism"], 1.2)
        # isolation: W1's clone held no other step's branch and no file of W2; its prompt never named W2
        w1 = self.prompts("W1")[0]
        self.assertNotIn("W2", w1)
        w1_pass = [e for e in events if e["kind"] == "node.pass" and e["node"] == "W1"][0]
        with open(os.path.join(out, "replies", w1_pass["reply"] + ".txt")) as handle:
            w1_reply = handle.read()
        refs = [line for line in w1_reply.splitlines() if line.startswith("refs: ")][0]
        self.assertEqual(refs, "refs: refs/heads/dag-work refs/heads/main")  # its own branch and the base repository's
        self.assertIn("files: a.txt base.txt", w1_reply)  # its own file and the base's: nothing of W2
        # W3 saw exactly its two upstream commits, with their summaries and diffs
        w3 = self.prompts("W3")[0]
        for words in ("It builds on W1, W2.", "Step W1 (done by mW1) passed the judge with score 1", "wrote a.txt",
                      "refs/dag/W1", "+A", "refs/dag/W2", "+B"):
            self.assertIn(words, w3)
        sha3 = s["nodes"]["W3"]["sha"]
        tree = sh(os.path.join(out, "workspaces", "W3-1"), "git", "ls-tree", "--name-only", sha3).split()
        self.assertEqual(sorted(tree), ["a.txt", "b.txt", "base.txt"])
        self.assertEqual(sh(repo, "git", "for-each-ref"), refs_before)  # the user's repository got nothing
        page = open(os.path.join(out, "view.html")).read()
        for words in ("DAG p", ">W1<", ">W3<", "edge done", "3 passed"):
            self.assertIn(words, page)
        self.assertEqual(sh(os.path.join(out, "workspaces", "W1-1"), "git", "remote"), "")  # nothing to push to

    def test_where_a_steps_files_start(self):
        self.script({"A": [{"write": {"a.txt": "A\n"}, "look": True}], "B": [{"write": {"b.txt": "B\n"}, "look": True}],
                     "AB": [{"look": True}], "M": [{"look": True}], "PickA": [{"look": True}], "Next": [{"look": True}]})
        out = os.path.join(self.dir, "run")
        code, said, err = self.run_main(self.plan([
            {"id": "A"}, {"id": "B"},
            {"id": "AB", "needs": ["A", "B"]},                     # several needs: the base, to compare and combine
            {"id": "M", "needs": ["A", "B"], "start": "merge"},    # merged first by the program
            {"id": "PickA", "needs": ["A", "B"], "start": "A"},
            {"id": "Next", "needs": ["PickA"]}]), "--out", out)   # one need: continue from its output
        self.assertEqual(code, 0, said + err)
        looked = {}
        for e in self.events(out):
            if e["kind"] == "node.pass":
                with open(os.path.join(out, "replies", e["reply"] + ".txt")) as handle:
                    looked[e["node"]] = [line for line in handle.read().splitlines() if line.startswith("files: ")][0]
        self.assertEqual(looked, {"A": "files: a.txt base.txt", "B": "files: b.txt base.txt", "AB": "files: base.txt",
                                  "M": "files: a.txt b.txt base.txt", "PickA": "files: a.txt base.txt",
                                  "Next": "files: a.txt base.txt"})
        self.assertIn("(the outputs of A and B merged)", self.prompts("M")[0])
        self.assertIn("(the output of step PickA)", self.prompts("Next")[0])
        self.assertIn("(the base commit)", self.prompts("AB")[0])

    def test_outputs_that_do_not_merge_fail_the_step_and_name_the_files(self):
        self.script({"A": [{"write": {"base.txt": "from A\n"}}], "B": [{"write": {"base.txt": "from B\n"}}]})
        out = os.path.join(self.dir, "run")
        code, said, _ = self.run_main(self.plan([{"id": "A"}, {"id": "B"}, {"id": "M", "needs": ["A", "B"], "start": "merge"}]),
                                      "--out", out)
        self.assertEqual(code, 1)  # the plan's problem, not a broken setup
        s = self.summary(out)
        self.assertEqual((s["nodes"]["M"]["state"], s["stopped"]), ("failed", None))
        self.assertIn("do not merge cleanly (conflicts in base.txt)", s["nodes"]["M"]["detail"])
        self.assertEqual(self.prompts("M"), [])

    def test_a_failed_attempt_is_tried_again_from_its_commit_with_the_judges_words(self):
        self.script({"A": [{"write": {"a.txt": "wrong\n"}}, {"write": {"a.txt": "right\n"}, "look": True}]})
        self.judge_rules({"A": {"files": {"a.txt": "right"}}})
        out = os.path.join(self.dir, "run")
        code, said, err = self.run_main(self.plan([{"id": "A", "retries": 1}]), "--out", out)
        self.assertEqual(code, 0, said + err)
        first, second = self.prompts("A")
        self.assertNotIn("previous attempt", first)
        self.assertIn("Your previous attempt did not pass the judge: a.txt should contain 'right'", second)
        self.assertIn("starts from that attempt's commit", second)
        events = self.events(out)
        retry = [e for e in events if e["kind"] == "node.retry"][0]
        second_start = [e for e in events if e["kind"] == "node.dispatch" and e["attempt"] == 2][0]["start"]
        self.assertEqual(second_start, retry["prev"]["sha"])  # built on the failed attempt, not on the base
        self.assertEqual(self.summary(out)["nodes"]["A"]["attempts"], 2)

    def test_a_step_that_fails_blocks_what_needs_it_and_the_rest_goes_on(self):
        self.script({"A": [{"write": {"a.txt": "wrong\n"}}], "C": [{"write": {"c.txt": "C\n"}}]})
        self.judge_rules({"A": {"files": {"a.txt": "right"}}, "C": {"files": {"c.txt": "C"}}})
        out = os.path.join(self.dir, "run")
        code, said, _ = self.run_main(self.plan([{"id": "A"}, {"id": "B", "needs": ["A"]}, {"id": "D", "needs": ["B"]},
                                                 {"id": "C"}]), "--out", out, "--parallel", "1")
        self.assertEqual(code, 1)
        s = self.summary(out)
        self.assertEqual({n: v["state"] for n, v in s["nodes"].items()},
                         {"A": "failed", "B": "blocked", "D": "blocked", "C": "passed"})
        self.assertEqual(self.prompts("B"), [])  # never dispatched
        self.assertIn("did not pass after 1 attempt(s): a.txt should contain 'right'", said)

    def test_a_broken_backend_stops_new_steps_unless_keep_going(self):
        self.script({"A": [{"exit": 3}], "B": [{}]})
        path = self.plan([{"id": "A"}, {"id": "B"}])
        out = os.path.join(self.dir, "run")
        code, said, _ = self.run_main(path, "--out", out, "--parallel", "1")
        self.assertEqual(code, 3)
        s = self.summary(out)
        self.assertEqual((s["nodes"]["A"]["state"], s["nodes"]["B"]["state"]), ("failed", "waiting"))
        self.assertIn("the member's backend ended error", s["stopped"])
        code, _, _ = self.run_main(path, "--out", os.path.join(self.dir, "run2"), "--parallel", "1", "--keep-going")
        s = self.summary(os.path.join(self.dir, "run2"))
        self.assertEqual((code, s["nodes"]["B"]["state"], s["stopped"]), (1, "passed", None))

    def test_a_judge_that_breaks_is_a_judge_error_not_a_fail(self):
        self.judge_rules({"A": {"garbage": True}})
        out = os.path.join(self.dir, "run")
        code, _, _ = self.run_main(self.plan([{"id": "A", "retries": 2}]), "--out", out)
        self.assertEqual(code, 3)
        fail = [e for e in self.events(out) if e["kind"] == "node.fail"][0]
        self.assertTrue(fail["infra"])
        self.assertIn("the judge failed", fail["why"])
        self.assertEqual(len(self.prompts("A")), 1)  # a broken judge is not a reason to try the member again

    def test_a_judge_status_other_than_valid_or_invalid_is_a_judge_error(self):
        self.judge_rules({"A": {"status": "maybe"}})
        out = os.path.join(self.dir, "run")
        self.assertEqual(self.run_main(self.plan([{"id": "A"}]), "--out", out)[0], 3)
        self.assertIn("the judge said status 'maybe'", self.summary(out)["nodes"]["A"]["detail"])

    def test_a_member_over_its_time_limit_fails_its_attempt_not_the_run(self):
        self.script({"A": [{"sleep": 5}], "B": [{}]})
        out = os.path.join(self.dir, "run")
        code, said, _ = self.run_main(self.plan([{"id": "A", "timeout": 1}, {"id": "B"}]), "--out", out, "--parallel", "1")
        self.assertEqual(code, 1)  # the member's problem: the run went on (exit 3 would mean the setup broke)
        s = self.summary(out)
        self.assertEqual((s["nodes"]["A"]["state"], s["nodes"]["B"]["state"], s["stopped"]), ("failed", "passed", None))
        self.assertIn("the member's turn ended timeout (limit 1 s)", s["nodes"]["A"]["detail"])

    def test_a_step_sees_only_the_steps_it_needs(self):
        self.script({"A": [{"write": {"a.txt": "A\n"}, "reply": "made a"}], "B": [{"write": {"b.txt": "B\n"}, "reply": "made b"}],
                     "C": [{"look": True}]})
        out = os.path.join(self.dir, "run")
        code, said, err = self.run_main(self.plan([{"id": "A"}, {"id": "B"}, {"id": "C", "needs": ["A"]}]), "--out", out,
                                        "--parallel", "1")  # A and B have both passed when C starts
        self.assertEqual(code, 0, said + err)
        c = self.prompts("C")[0]
        self.assertIn("Step A (done by mA)", c)
        self.assertNotIn("made b", c)
        self.assertNotIn("Step B", c)
        c_pass = [e for e in self.events(out) if e["kind"] == "node.pass" and e["node"] == "C"][0]
        with open(os.path.join(out, "replies", c_pass["reply"] + ".txt")) as handle:
            looked = handle.read()
        self.assertIn("refs/dag/A", looked)
        self.assertNotIn("refs/dag/B", looked)
        self.assertIn("files: a.txt base.txt", looked)

    def test_a_judge_changed_during_the_run_is_caught(self):
        judge = os.path.join(self.plans, "judge.py")
        self.script({"A": [{"append_to": judge}]})  # a member that reaches the judge (a command member can)
        out = os.path.join(self.dir, "run")
        code, _, _ = self.run_main(self.plan([{"id": "A"}]), "--out", out)
        self.assertEqual(code, 3)
        self.assertIn("changed during the run", self.summary(out)["nodes"]["A"]["detail"])

    def test_resume_runs_again_only_what_did_not_pass(self):
        self.script({"A": [{"write": {"a.txt": "A\n"}}], "B": [{"exit": 3}], "C": [{}]})
        path = self.plan([{"id": "A"}, {"id": "B", "needs": ["A"]}, {"id": "C", "needs": ["B"]}])
        out = os.path.join(self.dir, "run")
        self.assertEqual(self.run_main(path, "--out", out)[0], 3)
        code, _, err = self.run_main(path, "--out", out)
        self.assertEqual(code, 2)  # a folder that holds a run is not reused without --resume
        self.assertIn("already holds a run", err)
        self.script({"A": [{"write": {"a.txt": "A\n"}}], "B": [{}], "C": [{}]})  # the backend works again
        code, said, err = self.run_main(path, "--out", out, "--resume")
        self.assertEqual(code, 0, said + err)
        s = self.summary(out)
        self.assertEqual((s["passed"], s["dispatched_after_pass"], s["released_early"]), (3, 0, 0))
        self.assertEqual(len(self.prompts("A")), 1)  # passed before the resume: not run again
        self.assertEqual([e["attempt"] for e in self.events(out) if e["kind"] == "node.dispatch" and e["node"] == "B"], [1, 2])
        with open(os.path.join(self.plans, "tasks", "C.md"), "a") as handle:
            handle.write("changed\n")
        code, _, err = self.run_main(path, "--out", out, "--resume")
        self.assertEqual(code, 2)
        self.assertIn("the plan or a task file changed", err)

    def test_recheck_finds_a_verdict_that_does_not_hold_in_a_fresh_clone(self):
        self.script({"A": [{"write": {"a.txt": "A\n"}}], "B": [{"write": {"b.txt": "B\n"}}]})
        self.judge_rules({"A": {"files": {"a.txt": "A"}}, "B": {"env": "FLAKY"}})  # B's verdict hangs on the environment
        os.environ["FLAKY"] = "1"
        out = os.path.join(self.dir, "run")
        self.assertEqual(self.run_main(self.plan([{"id": "A"}, {"id": "B", "needs": ["A"]}]), "--out", out)[0], 0)
        del os.environ["FLAKY"]
        rows = {r["node"]: r for r in dag.recheck(out)}
        self.assertEqual((rows["A"]["now"], rows["B"]["now"]), ("valid", "invalid"))
        code, said, _ = self.run_main("--recheck", out)
        self.assertEqual(code, 1)
        self.assertIn("2 passed steps judged again, 1 did not hold", said)

    def test_a_read_step_keeps_no_changes(self):
        self.script({"R": [{"write": {"scratch.txt": "x\n"}, "reply": "looked"}], "W": [{"look": True}]})
        self.judge_rules({"W": {"absent": ["scratch.txt"]}})
        out = os.path.join(self.dir, "run")
        code, said, err = self.run_main(self.plan([{"id": "R", "access": "read"}, {"id": "W", "needs": ["R"]}]), "--out", out)
        self.assertEqual(code, 0, said + err)
        s = self.summary(out)
        self.assertEqual(s["nodes"]["R"]["sha"], s["base"])  # its output is where it started
        self.assertIn("A judge program checks your reply", self.prompts("R")[0])


class TextPlanTest(Base):
    def test_without_a_repository_the_reply_is_the_output(self):
        self.script({"A": [{"reply": "the answer is 42"}], "B": [{"reply": "B read it"}]})
        self.judge_rules({"A": {"reply": "42"}, "B": {"reply": "read"}})
        out = os.path.join(self.dir, "run")
        code, said, err = self.run_main(self.plan([{"id": "A"}, {"id": "B", "needs": ["A"]}], repo=False), "--out", out)
        self.assertEqual(code, 0, said + err)
        b = self.prompts("B")[0]
        self.assertIn("the answer is 42", b)
        self.assertIn("Your reply is your result", b)
        self.assertIsNone(self.summary(out)["base"])


class InvariantsTest(unittest.TestCase):
    def test_both_invariants_are_counted_from_the_events_alone(self):
        nodes = {"A": {"needs": []}, "B": {"needs": ["A"]}}
        good = [{"kind": "node.dispatch", "node": "A"}, {"kind": "node.pass", "node": "A"}, {"kind": "node.dispatch", "node": "B"}]
        self.assertEqual(dag.invariants(good, nodes), {"released_early": 0, "dispatched_after_pass": 0})
        early = [{"kind": "node.dispatch", "node": "A"}, {"kind": "node.dispatch", "node": "B"}, {"kind": "node.pass", "node": "A"}]
        again = good + [{"kind": "node.dispatch", "node": "A"}]
        self.assertEqual(dag.invariants(early, nodes)["released_early"], 1)
        self.assertEqual(dag.invariants(again, nodes)["dispatched_after_pass"], 1)


class OutOfBoundsTest(unittest.TestCase):
    def test_member_events_naming_another_steps_workspace_are_counted(self):
        out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, out, True)
        events = [{"kind": "node.dispatch", "node": "W1", "attempt": 1, "member": "a", "workspace": "workspaces/W1-1", "t": 10},
                  {"kind": "node.dispatch", "node": "W2", "attempt": 1, "member": "b", "workspace": "workspaces/W2-1", "t": 10},
                  {"kind": "node.return", "node": "W1", "attempt": 1, "t": 20},
                  {"kind": "node.return", "node": "W2", "attempt": 1, "t": 20}]
        os.makedirs(os.path.join(out, "members", "codex"))
        w2 = os.path.join(out, "workspaces", "W2-1")
        rows = [{"t": 12, "agent": "a", "event": {"item": {"type": "command_execution", "command": "cat " + w2 + "/b.txt"}}},
                {"t": 13, "agent": "a", "event": {"item": {"command": "ls " + os.path.join(out, "workspaces", "W1-1")}}},
                {"t": 30, "agent": "a", "event": {"item": {"command": "cat " + w2 + "/b.txt"}}},  # after its step ended
                {"t": 14, "agent": "a", "argv": ["codex", "-C", w2]}]
        with open(os.path.join(out, "members", "codex", "events.jsonl"), "w") as handle:
            handle.write("\n".join(json.dumps(r) for r in rows) + "\n")
        found = dag.out_of_bounds(out, events)
        self.assertEqual((found["count"], found["measured"]), (1, ["codex"]))
        self.assertEqual(found["examples"], ["a touched W2-1"])


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HAVE_GIT, "git is not installed")
class CloneContentsTest(Base):
    def test_no_file_in_a_clone_names_another_steps_clone(self):
        # git writes the folder it fetched from into FETCH_HEAD; a member that reads it would seem to touch that clone
        self.script({"A": [{"write": {"a.txt": "A\n"}}], "B": [{"write": {"b.txt": "B\n"}}]})
        out = os.path.join(self.dir, "run")
        code, said, err = self.run_main(self.plan([{"id": "A"}, {"id": "B", "needs": ["A"]}]), "--out", out)
        self.assertEqual(code, 0, said + err)
        other = os.path.join(out, "workspaces", "A-1")
        named = []
        for root, _, files in os.walk(os.path.join(out, "workspaces", "B-1")):
            for name in files:
                with open(os.path.join(root, name), "rb") as handle:
                    if other.encode() in handle.read():
                        named.append(os.path.relpath(os.path.join(root, name), out))
        self.assertEqual(named, [])
        self.assertIn("Your folder, %s, is a git clone" % os.path.join(out, "workspaces", "B-1"), self.prompts("B")[0])
        self.assertEqual(sh(os.path.join(out, "workspaces", "B-1"), "git", "rev-parse", "refs/dag/A"),
                         self.summary(out)["nodes"]["A"]["sha"])  # the upstream output is still there


@unittest.skipUnless(HAVE_GIT, "git is not installed")
class OpenCodeStepsTest(Base):
    """opencode members through a real daemon and a fake OpenCode that behaves as 1.18 with folders: each step's
    session works in the step's clone, its events come on /global/event, and the scripted agent acts in that folder."""

    def setUp(self):
        super().setUp()
        from fake_opencode import FakeOpenCode
        from herdr_py.opencode import OpenCode
        from herdr_py.policy import Policy
        from herdr_py.server import Daemon
        self.fake = FakeOpenCode(global_events=True).start()
        self.addCleanup(self.fake.stop)
        self.sock_dir = tempfile.mkdtemp(dir="/tmp")  # AF_UNIX paths are limited to ~104 bytes on macOS
        self.addCleanup(shutil.rmtree, self.sock_dir, True)
        policy = Policy([{"permission": "edit", "action": "allow"}, {"permission": "external_directory", "action": "deny"}],
                        default="deny")
        self.daemon = Daemon(OpenCode(self.fake.url), policy, self.sock_dir)
        self.daemon.unasked = []  # what `serve` reads from an OpenCode that asks about everything
        self.daemon.start()
        self.addCleanup(self.daemon.stop)
        self.fake.on_prompt = self.agent
        self.peek = None  # a folder B's read tool names: its own, or another step's clone
        self.stall = False

    def agent(self, session_id, text):
        """A: writes a.txt in its folder. B: tries to edit, is refused, reads a.txt (where self.peek says) and reports.
        With self.stall, the model provider never answers: OpenCode reports a retry every 0.2 s."""
        import time
        folder = self.fake.folder_of(session_id)
        if self.stall:
            self.fake.emit("session.status", sessionID=session_id, status={"type": "busy"})
            for n in range(40):
                time.sleep(0.2)
                self.fake.emit("session.status", sessionID=session_id,
                               status={"type": "retry", "attempt": n + 1, "message": "Rate limit exceeded"})
            return
        if "doing step A " in text:
            with open(os.path.join(folder, "a.txt"), "w") as handle:
                handle.write("from A\n")
            self.fake.turn(session_id, "wrote a.txt", tools=[("write", "completed", {"filePath": os.path.join(folder, "a.txt")}, "")])
        else:
            rid = self.fake.ask_permission(session_id, "edit", patterns=["a.txt"])
            deadline = time.time() + 5
            while rid in self.fake.pending and time.time() < deadline:
                time.sleep(0.02)
            read = os.path.join(self.peek or folder, "a.txt")
            with open(os.path.join(folder, "a.txt")) as handle:
                self.fake.turn(session_id, "a.txt says: " + handle.read().strip(),
                               tools=[("read", "completed", {"filePath": read}, "from A")])

    def plan_ab(self):
        self.judge_rules({"A": {"files": {"a.txt": "from A"}}, "B": {"reply": "says: from A"}})
        return self.plan([{"id": "A", "member": "o1=opencode:opencode/big-pickle", "timeout": 30},
                          {"id": "B", "member": "o2=opencode", "needs": ["A"], "access": "read", "timeout": 30}])

    def test_each_step_is_a_session_in_its_own_clone_and_a_read_step_cannot_edit(self):
        out = os.path.join(self.dir, "run")
        code, said, err = self.run_main(self.plan_ab(), "--out", out, "--socket", self.daemon.socket_path)
        self.assertEqual(code, 0, said + err)
        folders = sorted(s.get("directory") or "" for s in self.fake.sessions.values())
        self.assertEqual(folders, [os.path.join(out, "workspaces", "A-1"), os.path.join(out, "workspaces", "B-1")])
        self.assertEqual(sh(os.path.join(out, "workspaces", "A-1"), "git", "show", "HEAD:a.txt"), "from A")
        self.assertEqual([r[1] for r in self.fake.replies], ["reject"])  # B's edit, refused before the policy (which allows edits)
        self.assertIn("only reads", self.fake.replies[0][2])
        self.assertEqual(self.summary(out)["out_of_bounds"], {"count": 0, "examples": [], "measured": ["opencode"]})
        b = self.daemon.hub.agent("o2")
        self.assertEqual((b.directory, sorted(b.deny)), (os.path.join(out, "workspaces", "B-1"), ["bash", "edit", "external_directory"]))
        self.assertEqual(self.daemon.hub.agent("o1").model, "opencode/big-pickle")

    def test_the_run_stops_when_opencode_does_not_ask_or_cannot_see_the_clone(self):
        self.daemon.unasked = ["external_directory=unset"]
        out, plan = os.path.join(self.dir, "run1"), self.plan_ab()
        code, said, err = self.run_main(plan, "--out", out, "--socket", self.daemon.socket_path)
        self.assertEqual(code, 3, said + err)  # stopped: the setup broke
        stops = [e for e in self.events(out) if e["kind"] == "node.fail"]
        self.assertIn("does not ask before external_directory", stops[0]["why"])
        self.assertFalse(self.fake.sessions)  # nothing started

        self.daemon.unasked, self.fake.unseen = [], True
        out = os.path.join(self.dir, "run2")
        code, said, err = self.run_main(plan, "--out", out, "--socket", self.daemon.socket_path)
        self.assertEqual(code, 3, said + err)
        stops = [e for e in self.events(out) if e["kind"] == "node.fail"]
        self.assertIn("OpenCode cannot see", stops[0]["why"])
        self.assertFalse(self.fake.sessions)

    def test_a_tool_call_naming_another_steps_clone_is_counted(self):
        out = os.path.join(self.dir, "run")
        self.peek = os.path.join(out, "workspaces", "A-1")  # B's read names A's clone, not its own
        code, said, err = self.run_main(self.plan_ab(), "--out", out, "--socket", self.daemon.socket_path)
        self.assertEqual(code, 0, said + err)
        self.assertEqual(self.summary(out)["out_of_bounds"], {"count": 1, "examples": ["o2 touched A-1"], "measured": ["opencode"]})

    def test_a_provider_that_never_answers_stops_the_run_instead_of_failing_the_member(self):
        self.stall = True
        self.judge_rules({"A": {"files": {"a.txt": "from A"}}})
        out = os.path.join(self.dir, "run")
        plan = self.plan([{"id": "A", "member": "o1=opencode", "timeout": 1, "retries": 2}])
        code, said, err = self.run_main(plan, "--out", out, "--socket", self.daemon.socket_path)
        self.assertEqual(code, 3, said + err)  # the setup broke: no retry is spent on it
        events = self.events(out)
        self.assertEqual([e["state"] for e in events if e["kind"] == "node.return"], ["provider_stall"])
        fail = [e for e in events if e["kind"] == "node.fail"][0]
        self.assertTrue(fail["infra"])
        self.assertIn("the model provider stalled", fail["why"])
        self.assertFalse([e for e in events if e["kind"] in ("node.verdict", "node.retry")])
