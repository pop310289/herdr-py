import json
import os
import shutil
import subprocess
import sys
import tempfile

work = sys.argv[1]
want = {"East": 0.0, "North": 85.0, "South": 1500.0, "West": 15.5}


def check(path, expected):
    try:
        raw = open(path).read()
        data = json.loads(raw)
    except Exception as exc:
        return "cannot read %s: %s" % (path, exc)
    if list(data) != sorted(data) or set(data) != set(expected):
        return "wrong or unsorted keys: %s" % list(data)
    for k, v in expected.items():
        if not isinstance(data[k], (int, float)) or abs(data[k] - v) > 0.005:
            return "%s = %r, expected %r" % (k, data[k], v)
    return None


problem = check(os.path.join(work, "totals.json"), want)
if problem:
    print(problem); sys.exit(1)
tmp = tempfile.mkdtemp()
shutil.copy(os.path.join(work, "totals.py"), tmp)
with open(os.path.join(tmp, "sales.csv"), "w") as f:
    f.write('region,amount\n"Alpha","2,000.10"\nBeta,1\n\nAlpha,-0.10\n')
subprocess.run([sys.executable, "totals.py"], cwd=tmp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
problem = check(os.path.join(tmp, "totals.json"), {"Alpha": 2000.0, "Beta": 1.0})
if problem:
    print("on another csv: " + problem); sys.exit(1)
print("pass")
