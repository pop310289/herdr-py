"""Judge for examples/dag: every test in the step's clone must pass, and a step that adds a function must add its
test. It follows coop.py's contract: the reply file is the last argument; one JSON line; exit code 0."""
import json
import os
import subprocess
import sys

needs_file = {"div": "test_div.py"}.get(os.environ["HERDR_DAG_NODE"])
if needs_file and not os.path.exists(needs_file):
    print(json.dumps({"status": "invalid", "score": 0, "detail": needs_file + " is missing: a new function needs a test"}))
    sys.exit(0)
p = subprocess.run([sys.executable, "-m", "unittest", "-q"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                   universal_newlines=True)
ran = [line for line in p.stdout.splitlines() if line.startswith("Ran ")]
print(json.dumps({"status": "valid" if p.returncode == 0 else "invalid", "score": int(ran[0].split()[1]) if ran else 0,
                  "detail": (ran[0] if ran else "no tests ran") + ("" if p.returncode == 0 else ": " + p.stdout[-300:])}))
