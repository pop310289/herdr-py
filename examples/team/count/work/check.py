"""Visible check for the count task (the hidden grader checks more)."""
import os
import subprocess
import sys


def fail(msg):
    print("FAIL: " + msg)
    sys.exit(1)


if not os.path.exists("count/count.py"):
    fail("count/count.py does not exist")
p = subprocess.run([sys.executable, "count/count.py"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=30)
if p.returncode != 0:
    fail("count/count.py exited with code %d:\n%s" % (p.returncode, p.stdout[-800:]))
out = p.stdout.strip()
if not out.isdigit():
    fail("count/count.py should print only an integer, it printed: %r" % out[:200])
if not os.path.exists("count/answer.txt"):
    fail("count/answer.txt does not exist")
answer = open("count/answer.txt").read().strip()
if answer != out:
    fail("count/answer.txt contains %r but count/count.py prints %r" % (answer[:100], out))
print("PASS")
