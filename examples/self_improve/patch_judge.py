"""Judge for self-improvement runs: does a member's patch make the scenarios pass without breaking anything?

    python3 patch_judge.py --repo . --base HEAD --scenarios examples/self_improve/scenarios \\
        --test "python3 -B -m unittest discover -s tests -q" PATCH_FILE
    python3 patch_judge.py --repo . --base HEAD --scenarios DIR --check            # are the scenarios good tasks?

The answer (PATCH_FILE, coop.py's last argument) is a unified diff with paths relative to the repo's root. The judge:
1. exports BASE from the repo into a new folder (git archive: the working tree and its changes are never used);
2. preflight, on that clean copy: every --test command must pass (if not, the environment is broken: a judge error)
   and every scenario must fail (if not, the scenario tests nothing: a judge error);
3. refuses patches that touch protected files: the scenarios folder, this judge and its task maker, and any test file
   that already exists in BASE (new test files are fine) - those patches are invalid;
4. applies the patch (git apply) to a second clean copy, runs every --test command (any failure: invalid, it broke
   something) and then every scenario on its own.
Everything that runs code from the copy (the --test commands and the scenarios, on the base and on the patched copy) runs
in a sandbox (herdr_py/sandbox.py): --sandbox docker:IMAGE (default docker:python:3-slim) or podman:IMAGE, with only
the copy mounted, no network, a read-only root and no privileges. A patch is code a member wrote, so it never runs on
this machine as it is, unless the person passes --sandbox none (for a --test command that starts its own container,
or a machine without docker); the detail then starts with "[no sandbox]". No sandbox to run in is a judge error.
It prints one JSON line {"status", "score", "detail"} and exits 0 (herdr_py.coop --judge-mode json). The score is how
many scenarios pass; 0 is a valid score (nothing improved, nothing broken). A judge that cannot do its job exits 2
with the reason on stderr, which coop records as a judge error, never as an invalid patch.

Scenarios live in a folder with scenario.json: {"scenarios": [{"name", "file", "status": "frozen" | "pending-review",
"sources": [paths a member needs to read], "from": "where the problem was seen"}]}. Only frozen scenarios are used
unless --allow-pending (for trying the pipeline; definitions §19: only the user freezes a scenario). Each scenario is
a unittest file run with the copy on PYTHONPATH. Standard library only; Python 3.6+.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))  # the repository's herdr_py
from herdr_py.sandbox import Sandbox, SandboxError  # noqa: E402

DEFAULT_SANDBOX = "docker:python:3-slim"
PROTECTED_DIRS = ("examples/self_improve/",)


class JudgeError(Exception):
    """The judge could not do its job (not the patch's fault)."""


def run(argv, cwd, timeout, env=None):
    try:
        p = subprocess.run(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
                           timeout=timeout, env=env)
        return p.returncode, p.stdout
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        return None, out


def tail(text, n=6):
    return " | ".join(line.strip() for line in (text or "").strip().splitlines()[-n:])


def load_scenarios(folder, allow_pending):
    with open(os.path.join(folder, "scenario.json"), encoding="utf-8") as handle:
        spec = json.load(handle)
    chosen = []
    for s in spec.get("scenarios", []):
        if s.get("status") == "frozen" or (allow_pending and s.get("status") == "pending-review"):
            if not os.path.isfile(os.path.join(folder, s["file"])):
                raise JudgeError(f"scenario {s['name']}: {s['file']} is missing")
            chosen.append(s)
    if not chosen:
        raise JudgeError("no scenario to run (only frozen ones count; --allow-pending to try pending ones)")
    return chosen


def export(repo, base, dest):
    """BASE as files in dest (no .git): what the patch is judged against, whatever the working tree holds."""
    os.makedirs(dest)
    archive = subprocess.Popen(["git", "-C", repo, "archive", "--format=tar", base], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    untar = subprocess.run(["tar", "-x", "-C", dest], stdin=archive.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    archive.stdout.close()
    err = archive.stderr.read().decode("utf-8", "replace")
    archive.wait()
    if archive.returncode or untar.returncode:
        raise JudgeError(f"could not export {base} from {repo}: {err.strip() or untar.stderr.decode('utf-8', 'replace')}")


def base_files(repo, base):
    p = subprocess.run(["git", "-C", repo, "ls-tree", "-r", "--name-only", base], stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, universal_newlines=True)
    if p.returncode:
        raise JudgeError(f"git ls-tree {base}: {p.stderr.strip()}")
    return set(p.stdout.splitlines())


def unquote(path):
    path = path.strip()
    if len(path) >= 2 and path[0] == path[-1] == '"':
        path = path[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape").encode("latin-1").decode("utf-8", "replace")
    return path


HEADER = re.compile(r'^(?:--- |\+\+\+ |rename from |rename to |copy from |copy to )(.+?)\s*$')
GIT_HEADER = re.compile(r'^diff --git ("?a/.+?"?) ("?b/.+?"?)\s*$')


def touched(patch):
    """Every path a unified diff names (old and new sides), without the a/ b/ prefixes."""
    paths = set()
    for line in patch.splitlines():
        found = []
        m = GIT_HEADER.match(line)
        if m:
            found = [m.group(1), m.group(2)]
        else:
            m = HEADER.match(line)
            if m:
                found = [m.group(1).split("\t")[0]]
        for raw in found:
            path = unquote(raw)
            if path == "/dev/null":
                continue
            if line.startswith(("--- ", "+++ ", "diff --git")) and path[:2] in ("a/", "b/"):
                path = path[2:]
            paths.add(path)
    return paths


def protected_problem(paths, existing, scenario_rel):
    """Why this patch may not be judged, or None."""
    for path in sorted(paths):
        parts = path.replace("\\", "/").split("/")
        if path.startswith("/") or ".." in parts:
            return f"{path}: paths must stay inside the repo"
        if scenario_rel and (path == scenario_rel or path.startswith(scenario_rel.rstrip("/") + "/")):
            return f"{path}: the scenarios are the task; a patch may not change them"
        if any(path.startswith(d) for d in PROTECTED_DIRS):
            return f"{path}: the judge and the task maker may not be changed by a patch"
        if path in existing and (path.startswith("tests/") or "/tests/" in path):
            return f"{path}: changes an existing test (add a new test file instead; changing a test needs a person)"
    return None


def run_tests(box, commands, copy, timeout):
    """None when every command passed, otherwise what failed. Each runs with sh -c in the copy, in the sandbox."""
    for command in commands:
        code, out = box.run(["sh", "-c", command], copy, timeout, env={"PYTHONDONTWRITEBYTECODE": "1"})
        if code is None:
            return f"`{command}` did not finish within {timeout} s: {tail(out)}"
        if code:
            return f"`{command}` failed (exit {code}): {tail(out)}"
    return None


NOISE = re.compile(r"^(Ran \d+ tests? in |-{10,}|={10,}|OK\b|FAILED \()")


def why_failed(output):
    """The lines of a unittest run that say why it failed (not its timing or its rulers)."""
    lines = [line.strip() for line in (output or "").splitlines() if line.strip() and not NOISE.match(line.strip())]
    errors = [line for line in lines if re.match(r"^\w*(Error|Exception)\b", line) or line.startswith(("FAIL:", "ERROR:"))]
    return " | ".join((errors or lines)[-3:])


def run_scenarios(box, scenarios, folder, copy, python, timeout, repeat=1):
    """{name: (passed, tail)}: each scenario file on its own, in the sandbox, with the copy importable, `repeat` times;
    it passes only when every run passes (a scenario that passes sometimes is flaky, and a flaky pass earns nothing)."""
    place = os.path.join(copy, "_scenarios")
    os.makedirs(place, exist_ok=True)
    results = {}
    for s in scenarios:
        shutil.copy(os.path.join(folder, s["file"]), os.path.join(place, os.path.basename(s["file"])))
        module = os.path.splitext(os.path.basename(s["file"]))[0]
        passes, why = 0, ""
        for _ in range(repeat):
            code, out = box.run([python, "-B", "-m", "unittest", "-q", module], copy, timeout, cwd="_scenarios",
                                env={"PYTHONDONTWRITEBYTECODE": "1"}, path_env={"PYTHONPATH": "."})
            if code == 0:
                passes += 1
            elif not why:
                why = "did not finish" if code is None else why_failed(out)
        if passes == repeat:
            results[s["name"]] = (True, "")
        elif passes:
            results[s["name"]] = (False, f"flaky: passed {passes} of {repeat} runs; {why}")
        else:
            results[s["name"]] = (False, why)
    return results


def mark(box, detail):
    """A verdict reached without a sandbox says so first."""
    return ("[no sandbox] " if box.on_host else "") + detail


def judge(a):
    repo = os.path.abspath(a.repo)
    folder = os.path.abspath(a.scenarios)
    scenarios = load_scenarios(folder, a.allow_pending)
    rel = os.path.relpath(folder, repo)
    scenario_rel = None if rel.startswith("..") else rel.replace(os.sep, "/")
    box = Sandbox.parse(a.sandbox)
    python = a.python or (sys.executable if box.on_host else "python3")
    work_root = a.work_root or (None if box.on_host else os.path.join(os.path.expanduser("~"), ".cache", "herdr-py", "judge"))
    if work_root:
        os.makedirs(work_root, exist_ok=True)
    work = tempfile.mkdtemp(prefix="patch-judge-", dir=work_root)
    try:
        if not a.no_preflight:
            clean = os.path.join(work, "base")
            export(repo, a.base, clean)
            box.check(clean)
            broken = run_tests(box, a.test, clean, a.timeout)
            if broken:
                raise JudgeError(f"the base itself does not pass its tests, so the environment is broken: {broken}")
            already = [n for n, (ok, why) in run_scenarios(box, scenarios, folder, clean, python, a.timeout, a.repeat).items()
                       if ok or why.startswith("flaky")]
            if already:
                raise JudgeError(f"scenarios that pass on the base, every time or sometimes, test nothing reliably: "
                                 f"{', '.join(already)}")
        if a.check:
            return {"status": "valid", "score": 0,
                    "detail": f"{len(scenarios)} scenario(s) fail on {a.base} and its tests pass: good tasks"}
        with open(a.patch, encoding="utf-8", errors="replace") as handle:
            patch = handle.read()
        if not patch.strip():
            return {"status": "invalid", "score": None, "detail": "the patch is empty"}
        paths = touched(patch)
        if not paths:
            return {"status": "invalid", "score": None, "detail": "not a unified diff (no file headers found)"}
        problem = protected_problem(paths, base_files(repo, a.base), scenario_rel)
        if problem:
            return {"status": "invalid", "score": None, "detail": problem}
        copy = os.path.join(work, "patched")
        export(repo, a.base, copy)
        patch_path = os.path.join(work, "answer.diff")
        with open(patch_path, "w", encoding="utf-8") as handle:
            handle.write(patch if patch.endswith("\n") else patch + "\n")
        code, out = run(["git", "apply", "--check", patch_path], copy, 60)
        if code != 0:
            return {"status": "invalid", "score": None, "detail": f"git apply --check failed: {tail(out, 4)}"}
        code, out = run(["git", "apply", patch_path], copy, 60)
        if code != 0:
            return {"status": "invalid", "score": None, "detail": f"git apply failed: {tail(out, 4)}"}
        box.check(copy)
        broken = run_tests(box, a.test, copy, a.timeout)
        if broken:
            return {"status": "invalid", "score": None, "detail": mark(box, f"breaks the existing tests: {broken}")}
        results = run_scenarios(box, scenarios, folder, copy, python, a.timeout, a.repeat)
        passed = [n for n, (ok, _) in results.items() if ok]
        failed = [f"{n} ({why})" for n, (ok, why) in results.items() if not ok]
        detail = f"{len(passed)} of {len(results)} scenario(s) pass" + (f": {', '.join(passed)}" if passed else "")
        if failed:
            detail += "; still failing: " + "; ".join(failed)
        return {"status": "valid", "score": len(passed), "detail": mark(box, detail)[:1000]}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("patch", nargs="?", help="the member's answer: a unified diff")
    ap.add_argument("--repo", required=True, help="a git repository holding BASE")
    ap.add_argument("--base", default="HEAD", help="the commit the patch is judged against (default HEAD)")
    ap.add_argument("--scenarios", required=True, metavar="DIR", help="folder with scenario.json and the scenario tests")
    ap.add_argument("--test", action="append", default=[], metavar="COMMAND",
                    help="an existing test suite that must still pass (repeat for each platform); run with sh -c in the copy")
    ap.add_argument("--python", help="the Python that runs the scenarios (default: python3 in the sandbox; this "
                                     "Python with --sandbox none)")
    ap.add_argument("--sandbox", default=DEFAULT_SANDBOX, metavar="ENGINE:IMAGE",
                    help=f"where the patched code runs: docker:IMAGE or podman:IMAGE (default {DEFAULT_SANDBOX}), or "
                         f"none to run it on this machine (unsafe: only for code you trust or a --test that starts its "
                         f"own container)")
    ap.add_argument("--timeout", type=int, default=900, metavar="S", help="for each test command and each scenario")
    ap.add_argument("--work-root", metavar="DIR", help="where the clean copies are made (default: ~/.cache/herdr-py/judge "
                    "with a sandbox, the system's temporary folder with --sandbox none). It has to be a folder the "
                    "container can see: colima shares only your home with its VM, so a copy under /var/folders looks "
                    "empty inside the container (the judge checks this before it runs anything)")
    ap.add_argument("--repeat", type=int, default=1, metavar="N",
                    help="run every scenario N times; it passes only when all N pass (use 3 or more when a scenario "
                         "involves threads or a daemon)")
    ap.add_argument("--allow-pending", action="store_true", help="also use scenarios still waiting for the user's review")
    ap.add_argument("--no-preflight", action="store_true", help="skip checking the base (faster; a broken environment "
                                                                "then looks like a patch that broke the tests)")
    ap.add_argument("--check", action="store_true", help="only check that the scenarios fail on the base and its tests pass")
    a = ap.parse_args(argv)
    if not a.check and not a.patch:
        ap.error("give the patch file (or --check)")
    if a.check and a.no_preflight:
        ap.error("--check is the preflight; do not combine it with --no-preflight")
    if a.repeat < 1:
        ap.error("--repeat: 1 or more")
    if not a.test:
        a.test = ["python3 -B -m unittest discover -s tests -q"]
    try:
        verdict = judge(a)
    except (JudgeError, SandboxError, OSError, ValueError, KeyError) as exc:
        print(f"patch_judge: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(verdict))
    return 0


if __name__ == "__main__":
    sys.exit(main())
