"""A team member that is a program, for trying examples/dag without spending on models: what it writes depends on
the step named in its prompt (dag.py says "doing step <id> of the plan"). Real members get the same prompt."""
import re
import sys

prompt = sys.stdin.read()
step = re.search(r"doing step (\S+) of the plan", prompt).group(1)
if step == "mul":
    with open("calc.py", "a") as handle:
        handle.write("\n\ndef mul(a, b):\n    return a * b\n")
    with open("test_calc.py", "a") as handle:
        handle.write("\n    def test_mul(self):\n        self.assertEqual(mul(2, 3), 6)\n")
    print("added mul() to calc.py, with a test")
elif step == "sub":
    with open("calc_sub.py", "w") as handle:
        handle.write("def sub(a, b):\n    return a - b\n")
    with open("test_sub.py", "w") as handle:
        handle.write("import unittest\nfrom calc_sub import sub\n\n\nclass Sub(unittest.TestCase):\n"
                     "    def test_sub(self):\n        self.assertEqual(sub(5, 3), 2)\n")
    print("added sub() in its own module, with a test")
elif step == "together":
    print("started from mul and sub merged; every test passes with both")
elif step == "div":
    with open("calc_div.py", "w") as handle:
        handle.write("def div(a, b):\n    return a // b\n")
    print("added div() (no test: the judge will say so)")
else:
    print("nothing to do for " + step)
