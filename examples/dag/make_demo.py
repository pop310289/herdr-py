"""Make a small repository and a DAG plan for it, to try herdr_py.dag without spending on models.

    python3 examples/dag/make_demo.py /tmp/dag-demo
    python3 -m herdr_py.dag /tmp/dag-demo/plan.json --check
    python3 -m herdr_py.dag /tmp/dag-demo/plan.json --out /tmp/dag-demo/run1      # then open run1/view.html

The plan: mul, sub and div start at once (sub and div share member w2, so those two take turns); "together" starts
from mul and sub merged and must pass every test; div adds no test, fails twice (one retry), and blocks "docs".
Swap a member for w1=codex or w2=claude to run the same plan with agents.
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main(folder):
    repo = os.path.join(folder, "repo")
    if os.path.exists(folder) and os.listdir(folder):
        sys.exit(f"{folder} is not empty")
    os.makedirs(repo)
    with open(os.path.join(repo, "calc.py"), "w") as handle:
        handle.write("def add(a, b):\n    return a + b\n")
    with open(os.path.join(repo, "test_calc.py"), "w") as handle:
        handle.write("import unittest\nfrom calc import *\n\n\nclass Calc(unittest.TestCase):\n"
                     "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n")
    env = dict(os.environ, GIT_AUTHOR_NAME="demo", GIT_AUTHOR_EMAIL="demo@example.invalid",
               GIT_COMMITTER_NAME="demo", GIT_COMMITTER_EMAIL="demo@example.invalid")
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "a calculator that only adds"]):
        subprocess.run(["git", "-C", repo] + args, check=True, env=env)
    member = f"command:{sys.executable} {os.path.join(HERE, 'member.py')}"
    judge = f"{sys.executable} {os.path.join(HERE, 'judge.py')}"
    task = lambda name: os.path.join(HERE, "tasks", name + ".md")  # noqa: E731
    plan = {"name": "calculator demo", "repo": "repo", "nodes": [
        {"id": "mul", "member": "w1=" + member, "task": task("mul"), "judge": judge},
        {"id": "sub", "member": "w2=" + member, "task": task("sub"), "judge": judge},
        {"id": "together", "member": "w3=" + member, "task": task("together"), "judge": judge,
         "needs": ["mul", "sub"], "start": "merge"},
        {"id": "div", "member": "w2=" + member, "task": task("div"), "judge": judge, "retries": 1},
        {"id": "docs", "member": "w1=" + member, "task": task("docs"), "judge": judge, "needs": ["together", "div"]}]}
    with open(os.path.join(folder, "plan.json"), "w") as handle:
        json.dump(plan, handle, indent=1)
    print(os.path.join(folder, "plan.json"))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(os.path.abspath(sys.argv[1]))
