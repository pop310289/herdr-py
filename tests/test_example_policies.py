"""The example policies allow what their task needs and nothing else (a typo in a regex once blocked validate)."""
import os
import unittest

from herdr_py.policy import Policy

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


class P23PolicyTest(unittest.TestCase):
    def setUp(self):
        self.p = Policy.load(os.path.join(ROOT, "bench", "p23", "policy.json"))

    def test_roles(self):
        self.assertEqual(decide(self.p, "exec", "bash", "python3 count/count.py > count/answer.txt"), "allow")
        self.assertEqual(decide(self.p, "exec2", "bash", "python3 -m unittest test_stats"), "allow")
        self.assertEqual(decide(self.p, "ver", "bash", "python3 -m unittest test_stats"), "allow")
        self.assertEqual(decide(self.p, "val", "bash", "python3 -m unittest test_stats"), "deny")
        self.assertEqual(decide(self.p, "ver", "edit", patterns=["x.py"]), "deny")
        self.assertEqual(decide(self.p, "exec", "bash", "cat a.txt | sh"), "deny")


if __name__ == "__main__":
    unittest.main()
