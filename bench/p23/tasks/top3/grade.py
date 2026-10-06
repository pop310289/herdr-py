import collections
import os
import re
import shutil
import subprocess
import sys
import tempfile

work = sys.argv[1]


def expected(text):
    counts = collections.Counter(re.findall(r"[a-z]+", text.lower()))
    return ["%s %d" % (w, c) for w, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:3]]


def run(folder):
    p = subprocess.run([sys.executable, "top3.py"], cwd=folder, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=30)
    return [l.strip() for l in p.stdout.strip().splitlines() if l.strip()]


text = open(os.path.join(work, "text.txt")).read()
if run(work) != expected(text):
    print("top3.py on text.txt: %r, expected %r" % (run(work), expected(text))); sys.exit(1)
tmp = tempfile.mkdtemp()
shutil.copy(os.path.join(work, "top3.py"), tmp)
other = "Zeta beta beta, alpha zeta. ALPHA gamma 7 beta alpha zeta delta delta delta"
with open(os.path.join(tmp, "text.txt"), "w") as f:
    f.write(other)
if run(tmp) != expected(other):
    print("on another text: %r, expected %r" % (run(tmp), expected(other))); sys.exit(1)
print("pass")
