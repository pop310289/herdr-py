"""The example policies allow what their task needs and nothing else (a typo in a regex once blocked validate), and the
team mode example task is one a team can finish."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from herdr_py.policy import Policy
from herdr_py.team import load_task

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def decide(policy, agent, permission, command=None, patterns=None):
    req = {"permission": permission, "metadata": {"command": command} if command else {}, "patterns": patterns or ([command] if command else [])}
    return policy.decide(req, agent=agent)[0]


class SlideTeamPolicyTest(unittest.TestCase):
    def setUp(self):
        self.p = Policy.load(os.path.join(ROOT, "examples", "slide_team", "policy.json"))

    def test_drawers_can_build_validate_and_export(self):
        for cmd in ("python3 make_deck.py", "python3 -m open_slide_py validate deck.json",
                    "python3 -S -m open_slide_py export deck.json renders/out.svg", "ls -la", "cat NOTES.md SPEC.md"):
            self.assertEqual(decide(self.p, "drawA", "bash", cmd), "allow", cmd)
        self.assertEqual(decide(self.p, "drawB", "edit", patterns=["make_deck.py"]), "allow")

    def test_everything_else_is_denied(self):
        for agent, perm, cmd in (("drawA", "bash", "python3 make_deck.py; rm -rf /"), ("drawA", "bash", "python3 -m pip install x"),
                                 ("drawA", "bash", "curl http://example.com"), ("art", "bash", "ls"), ("art", "edit", None),
                                 ("drawA", "external_directory", None)):
            self.assertEqual(decide(self.p, agent, perm, cmd, None if cmd else ["/etc/*"]), "deny", (agent, perm, cmd))


class TeamPolicyTest(unittest.TestCase):
    def setUp(self):
        self.p = Policy.load(os.path.join(ROOT, "examples", "team", "policy.json"))

    def test_roles(self):
        self.assertEqual(decide(self.p, "exec", "bash", "python3 count/count.py > count/answer.txt"), "allow")
        self.assertEqual(decide(self.p, "exec2", "bash", "python3 -m unittest test_stats"), "allow")
        self.assertEqual(decide(self.p, "ver", "bash", "python3 -m unittest test_stats"), "allow")
        self.assertEqual(decide(self.p, "val", "bash", "python3 -m unittest test_stats"), "deny")
        self.assertEqual(decide(self.p, "ver", "edit", patterns=["x.py"]), "deny")
        self.assertEqual(decide(self.p, "exec", "bash", "cat a.txt | sh"), "deny")



class TeamExampleTest(unittest.TestCase):
    """examples/team/count: the task loads, and its visible check fails on the untouched folder and passes when the
    task is done."""

    def test_the_check_fails_until_the_task_is_done(self):
        task = load_task(os.path.join(ROOT, "examples", "team", "count", "task.json"))
        self.assertEqual((task["name"], task["check"]), ("count", ["python3", "check.py"]))
        work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, work, True)
        shutil.copytree(os.path.join(ROOT, "examples", "team", "count", "work"), os.path.join(work, "w"))
        work = os.path.join(work, "w")

        def check():
            return subprocess.run([sys.executable, "check.py"], cwd=work, stdout=subprocess.PIPE, universal_newlines=True)
        self.assertIn("count/count.py does not exist", check().stdout)
        with open(os.path.join(work, "count", "count.py"), "w") as handle:
            handle.write("print(len(open('count/notes.txt').read().split()))\n")
        words = str(len(open(os.path.join(work, "count", "notes.txt")).read().split()))
        with open(os.path.join(work, "count", "answer.txt"), "w") as handle:
            handle.write(words + "\n")
        done = check()
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "PASS"))


if __name__ == "__main__":
    unittest.main()
