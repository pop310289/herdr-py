"""The organizer (organize.py): it proposes a plan, the program checks it against dag.py's rules and the team's limits
(listed members and judges only, steps, member turns, retries), a refused plan goes back with every reason, and an
accepted plan runs as it is. The organizer here is a program (a command member), so no model is called."""
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

from herdr_py import dag, organize  # noqa: E402

# An organizer program: its replies, one per call, come from ORG_REPLIES; every prompt goes to ORG_LOG.
ORGANIZER = r'''
import json, os, sys
prompt = sys.stdin.read()
log = os.environ["ORG_LOG"]
os.makedirs(log, exist_ok=True)
n = len(os.listdir(log))
open(os.path.join(log, "%d.txt" % (n + 1)), "w").write(prompt)
replies = json.load(open(os.environ["ORG_REPLIES"]))
reply = replies[min(n, len(replies) - 1)]
if reply == "CRASH":
    sys.exit(3)
print(reply)
'''
WORKER = r'''
import sys
print("did: " + sys.stdin.read().split("Task:")[1].strip().splitlines()[0])
'''
JUDGE = r'''
import json, sys
print(json.dumps({"status": "valid" if "did" in open(sys.argv[-1]).read() else "invalid", "score": 1}))
'''


def fenced(plan, why="two writers then a reader"):
    return why + "\n```json\n" + json.dumps(plan) + "\n```"


class OrganizeTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        for name, body in (("organizer.py", ORGANIZER), ("worker.py", WORKER), ("judge.py", JUDGE)):
            with open(os.path.join(self.dir, name), "w") as handle:
                handle.write(textwrap.dedent(body))
        with open(os.path.join(self.dir, "goal.md"), "w") as handle:
            handle.write("Write two short notes and one summary of both.\n")
        self.team_path = os.path.join(self.dir, "team.json")
        self.team(members=[{"name": "w", "member": "w=command:%s -B worker.py" % sys.executable, "about": "writes notes"}],
                  judges=[{"name": "said", "command": "%s -B judge.py" % sys.executable, "about": "the reply says what was done"}],
                  limits={"max_steps": 3, "max_turns": 4, "max_retries": 1})
        saved = {k: os.environ.get(k) for k in ("ORG_LOG", "ORG_REPLIES")}
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v) for k, v in saved.items()])
        os.environ["ORG_LOG"] = os.path.join(self.dir, "prompts")
        os.environ["ORG_REPLIES"] = os.path.join(self.dir, "replies.json")
        self.out = os.path.join(self.dir, "plan")

    def team(self, **body):
        body.setdefault("goal", "goal.md")
        with open(self.team_path, "w") as handle:
            json.dump(body, handle)

    def replies(self, *replies):
        with open(os.environ["ORG_REPLIES"], "w") as handle:
            json.dump(list(replies), handle)

    def organize(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = organize.main([self.team_path, "--organizer", "org=command:%s -B %s" % (sys.executable,
                                  os.path.join(self.dir, "organizer.py")), "--out", self.out] + list(extra))
        return code, out.getvalue(), err.getvalue()

    def prompts(self):
        folder = os.environ["ORG_LOG"]
        return [open(os.path.join(folder, f)).read() for f in sorted(os.listdir(folder))]

    def log(self):
        with open(os.path.join(self.out, "organize.jsonl")) as handle:
            return [json.loads(line) for line in handle]

    GOOD = {"name": "notes", "why": "parallel notes, then a summary", "steps": [
        {"id": "a", "member": "w", "judge": "said", "task": "Write note A about apples, two lines."},
        {"id": "b", "member": "w", "judge": "said", "task": "Write note B about bananas, two lines."},
        {"id": "sum", "member": "w", "judge": "said", "task": "Summarise notes A and B in one line.", "needs": ["a", "b"]}]}

    def test_a_refused_plan_goes_back_with_every_reason_and_the_fixed_one_runs(self):
        bad = {"name": "too big", "steps": [
            {"id": "a", "member": "ghost", "judge": "said", "task": "Write note A about apples, two lines.", "retries": 2},
            {"id": "b", "member": "w", "judge": "mine", "task": "short", "needs": ["zzz"], "colour": "red"},
            {"id": "c", "member": "w", "judge": "said", "task": "Write note C about cherries, two lines."},
            {"id": "d", "member": "w", "judge": "said", "task": "Write note D about dates, two lines."}]}
        self.replies(fenced(bad), fenced(self.GOOD))
        code, said, err = self.organize()
        self.assertEqual(code, 0, said + err)
        first, second = self.prompts()
        for words in ("Write two short notes", "- w (command): writes notes", "- said: the reply says what was done",
                      "at most 3 steps; at most 4 member turns"):
            self.assertIn(words, first)
        self.assertNotIn("previous plan was refused", first)
        for words in ("4 steps: at most 3", "member 'ghost' is not one of w", "judge 'mine' is not one of said",
                      "2 retries, at most 1", "unknown fields colour", "the task is the full text",
                      "needs zzz, which is not a node", "6 member turns counting retries: at most 4"):
            self.assertIn(words, second)
        kinds = [e["kind"] for e in self.log()]
        self.assertEqual(kinds, ["organize.start", "organize.reply", "organize.refused", "organize.reply", "organize.accept"])
        accept = self.log()[-1]
        with open(os.path.join(self.out, "plan.json"), "rb") as handle:
            self.assertEqual(accept["plan"], __import__("hashlib").sha256(handle.read()).hexdigest())
        self.assertEqual(accept["why"], "parallel notes, then a summary")
        plan = dag.load_plan(os.path.join(self.out, "plan.json"))
        self.assertEqual(plan["nodes"]["sum"]["needs"], ["a", "b"])
        self.assertEqual(plan["nodes"]["a"]["judge_argv"][2], os.path.join(self.dir, "judge.py"))  # the team's judge, found
        self.assertIn(os.path.join(self.dir, "worker.py"), plan["nodes"]["a"]["member"]["command"])
        with open(os.path.join(self.out, "tasks", "b.md")) as handle:
            self.assertEqual(handle.read(), "Write note B about bananas, two lines.\n")
        self.assertEqual(sorted(f for f in os.listdir(self.out) if f.startswith("proposal-")), ["proposal-1"])  # kept for review
        # the accepted plan runs as it is
        run = io.StringIO()
        with contextlib.redirect_stdout(run):
            self.assertEqual(dag.main([os.path.join(self.out, "plan.json"), "--out", os.path.join(self.dir, "run")]), 0)

    def test_an_organizer_that_never_sends_a_plan_is_refused_every_time(self):
        self.replies("I would split the work in three.", "```json\n{not json\n```", fenced({"steps": []}))
        code, _, err = self.organize("--tries", "3")
        self.assertEqual(code, 1)
        problems = [e["problems"][0] for e in self.log() if e["kind"] == "organize.refused"]
        self.assertEqual(problems[0], "no fenced JSON block in the reply")
        self.assertIn("the JSON block does not parse", problems[1])
        self.assertEqual(problems[2], "the JSON block is an object with a non-empty \"steps\" list")
        self.assertFalse(os.path.exists(os.path.join(self.out, "plan.json")))
        self.assertIn("no plan was accepted", err)

    def test_a_broken_organizer_backend_is_not_asked_again(self):
        self.replies("CRASH", fenced(self.GOOD))
        code, _, _ = self.organize("--tries", "3")
        self.assertEqual(code, 1)
        self.assertEqual(len(self.prompts()), 1)
        self.assertIn("the organizer's turn ended error", self.log()[-1]["problems"][0])

    def test_the_team_file_is_checked_before_anything_runs(self):
        self.team(goal="", members=[], judges=[])
        self.assertEqual(self.organize()[0], 2)
        self.team(members=[{"name": "a", "member": "b=codex"}], judges=[{"name": "j"}], limits={"max_steps": -1})
        code, _, err = self.organize()
        self.assertEqual(code, 2)
        for words in ("with the same name", "judges: {'name': 'j'}: give name and command",
                      "limits: max_steps is max_steps, max_turns or max_retries"):
            self.assertIn(words, err)
        self.replies(fenced(self.GOOD))
        self.team(members=[{"name": "w", "member": "w=command:%s -B worker.py" % sys.executable}],
                  judges=[{"name": "said", "command": "%s -B judge.py" % sys.executable}])
        self.assertEqual(self.organize()[0], 0)
        self.assertEqual(self.organize()[0], 2)  # the folder holds an organizer's work now

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_with_a_repository_the_prompt_says_how_clones_start_and_the_plan_keeps_it(self):
        repo = os.path.join(self.dir, "repo")
        os.makedirs(repo)
        subprocess.run(["git", "init", "-q", repo], check=True)
        subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-q", "--allow-empty", "-m", "base"],
                       check=True)
        self.team(repo="repo", members=[{"name": "w", "member": "w=command:%s -B worker.py" % sys.executable}],
                  judges=[{"name": "said", "command": "%s -B judge.py" % sys.executable}])
        merge = dict(self.GOOD, steps=self.GOOD["steps"][:2] + [dict(self.GOOD["steps"][2], start="merge")])
        self.replies(fenced(merge))
        code, said, err = self.organize()
        self.assertEqual(code, 0, said + err)
        self.assertIn('unless its "start" is "merge"', self.prompts()[0])
        plan = dag.load_plan(os.path.join(self.out, "plan.json"))
        self.assertEqual((plan["repo"], plan["nodes"]["sum"]["start"]), (repo, "merge"))


if __name__ == "__main__":
    unittest.main()
