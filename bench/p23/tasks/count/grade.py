"""Hidden grader: python3 grade.py WORKDIR. Exit 0 = pass."""
import os
import shutil
import subprocess
import sys
import tempfile

work = sys.argv[1]
truth = len(open(os.path.join(work, "count", "notes.txt")).read().split())
try:
    answer = open(os.path.join(work, "count", "answer.txt")).read().strip()
except OSError:
    print("no answer.txt"); sys.exit(1)
if answer != str(truth):
    print("answer.txt is %r, expected %d" % (answer[:60], truth)); sys.exit(1)
tmp = tempfile.mkdtemp()
shutil.copytree(os.path.join(work, "count"), os.path.join(tmp, "count"))
with open(os.path.join(tmp, "count", "notes.txt"), "w") as f:
    f.write("one two  three\nfour\tfive six.\n\nseven\n")
p = subprocess.run([sys.executable, "count/count.py"], cwd=tmp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=30)
if p.stdout.strip() != "7":
    print("count.py on another text printed %r, expected 7" % p.stdout.strip()[:60]); sys.exit(1)
print("pass"); sys.exit(0)
