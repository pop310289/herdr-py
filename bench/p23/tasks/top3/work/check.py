"""Visible check for top3: three lines in the form `word count` (the content is not checked)."""
import os
import re
import subprocess
import sys

if not os.path.exists("top3.py"):
    print("FAIL: top3.py does not exist"); sys.exit(1)
p = subprocess.run([sys.executable, "top3.py"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=30)
lines = [l for l in p.stdout.strip().splitlines() if l.strip()]
if p.returncode != 0 or len(lines) != 3:
    print("FAIL: expected 3 lines, got %d (exit %d):\n%s" % (len(lines), p.returncode, p.stdout[-600:])); sys.exit(1)
for l in lines:
    if not re.fullmatch(r"[a-z]+ \d+", l.strip()):
        print("FAIL: line %r is not `word count` in lower case" % l); sys.exit(1)
print("PASS")
