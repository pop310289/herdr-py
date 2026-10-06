"""Visible check for fizzsum: only N = 15 is tested here."""
import os
import subprocess
import sys

if not os.path.exists("fizz.py"):
    print("FAIL: fizz.py does not exist"); sys.exit(1)
want = ["FizzBuzz" if i % 15 == 0 else "Fizz" if i % 3 == 0 else "Buzz" if i % 5 == 0 else str(i) for i in range(1, 16)] + ["Sum: 120"]
p = subprocess.run([sys.executable, "fizz.py", "15"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=30)
got = p.stdout.strip().splitlines()
for i, (w, g) in enumerate(zip(want, got)):
    if w != g.strip():
        print("FAIL: line %d should be %r, got %r" % (i + 1, w, g)); sys.exit(1)
if len(got) != len(want):
    print("FAIL: expected %d lines, got %d" % (len(want), len(got))); sys.exit(1)
print("PASS")
