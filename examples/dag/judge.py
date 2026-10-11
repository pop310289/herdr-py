"""Judge for examples/dag: every test in the step's clone must pass, and a step that adds a function must add its
test. It follows coop.py's contract: the reply file is the last argument; one JSON line; exit code 0.

The tests are code a member wrote, so they run in a sandbox (herdr_py/sandbox.py) with only the step's folder mounted:
$HERDR_JUDGE_SANDBOX, default docker:python:3-slim (podman:IMAGE works too). HERDR_JUDGE_SANDBOX=none runs them on this
machine as they are (unsafe: only for code you trust); the detail then starts with "[no sandbox]". No sandbox to run in
exits 2, which dag.py records as a broken judge, never as a failed step."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))  # the repository's herdr_py
from herdr_py.sandbox import Sandbox, SandboxError  # noqa: E402

needs_file = {"div": "test_div.py"}.get(os.environ["HERDR_DAG_NODE"])
if needs_file and not os.path.exists(needs_file):
    print(json.dumps({"status": "invalid", "score": 0, "detail": needs_file + " is missing: a new function needs a test"}))
    sys.exit(0)
try:
    box = Sandbox.parse(os.environ.get("HERDR_JUDGE_SANDBOX", "docker:python:3-slim"))
    box.check(os.getcwd())
except (SandboxError, OSError) as exc:
    print(f"dag judge: {exc}", file=sys.stderr)
    sys.exit(2)
limit = max(1, int(os.environ.get("HERDR_DAG_JUDGE_TIMEOUT", "600")) - 15)  # stop the container before dag.py stops us
code, out = box.run([sys.executable if box.on_host else "python3", "-m", "unittest", "-q"], os.getcwd(), limit,
                    env={"PYTHONDONTWRITEBYTECODE": "1"})
ran = [line for line in out.splitlines() if line.startswith("Ran ")]
detail = ("[no sandbox] " if box.on_host else "") + (ran[0] if ran else "no tests ran") + (
    "" if code == 0 else (": did not finish in %d s" % limit if code is None else ": " + out[-300:]))
print(json.dumps({"status": "valid" if code == 0 else "invalid", "score": int(ran[0].split()[1]) if ran else 0,
                  "detail": detail}))
