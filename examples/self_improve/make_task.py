"""Build the task text for a self-improvement run: the rules, the scenarios (tests that fail today) and the source
files they name, all inside the text, because members may have no tools (Claude) or only read their own empty folder
(Codex). Files come from BASE in git, never from the working tree.

    python3 make_task.py --repo . --base HEAD --scenarios examples/self_improve/scenarios > task.md
    python3 -m herdr_py.coop --task task.md --answer-name answer.diff \\
        --judge "python3 examples/self_improve/patch_judge.py --repo . --base <BASE> --scenarios examples/self_improve/scenarios" ...

Fails loudly (exit 2) when a source is missing or the task would be longer than --max-bytes: a task that silently
drops the code it is about would ask members to guess. Standard library only; Python 3.6+.
"""
import argparse
import json
import os
import subprocess
import sys

INTRO = """You are improving herdr-py, a Python 3.6+ standard-library tool that runs teams of coding agents.
Below are {n} scenario test(s). Each one fails on the current code (commit {base}); each describes behaviour that
someone needed and did not get. Write ONE patch that makes as many of them pass as you can.

Rules (a program checks all of them; nothing you claim about your patch is believed):
- Answer with a unified diff (as from `git diff`), paths relative to the repository root with a/ and b/ prefixes,
  enough context lines for `git apply` to apply it to commit {base} exactly as shown below.
- The repository's existing tests must all still pass, on macOS and on RHEL 8 with Python 3.6: no f-string `=`,
  no walrus, no dataclasses, standard library only.
- Do not change the scenario tests, anything under examples/self_improve/, or any existing file under tests/.
  You may add new test files.
- Your score is the number of scenarios that pass after your patch. A patch that does not apply, touches what it may
  not, or breaks an existing test scores nothing.
"""


def show(repo, base, path):
    p = subprocess.run(["git", "-C", repo, "show", f"{base}:{path}"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode:
        raise ValueError(f"{path} is not in {base}: {p.stderr.decode('utf-8', 'replace').strip()}")
    return p.stdout.decode("utf-8")


def fence(text):
    mark = "~~~~" if "```" in text else "```"
    return f"{mark}\n{text.rstrip()}\n{mark}"


def build(repo, base, folder, allow_pending=False):
    with open(os.path.join(folder, "scenario.json"), encoding="utf-8") as handle:
        spec = json.load(handle)
    chosen = [s for s in spec.get("scenarios", [])
              if s.get("status") == "frozen" or (allow_pending and s.get("status") == "pending-review")]
    if not chosen:
        raise ValueError("no scenario to use (only frozen ones count; --allow-pending to try pending ones)")
    commit = subprocess.run(["git", "-C", repo, "rev-parse", "--short", base], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, universal_newlines=True)
    if commit.returncode:
        raise ValueError(f"{base}: {commit.stderr.strip()}")
    base_id = commit.stdout.strip()
    out = [INTRO.format(n=len(chosen), base=base_id)]
    sources = []
    for s in chosen:
        with open(os.path.join(folder, s["file"]), encoding="utf-8") as handle:
            test = handle.read()
        why = s.get("why", "").strip()
        out.append(f"## Scenario {s['name']}\n" + (why + "\n" if why else "") + "\n" + fence(test))
        sources += [p for p in s.get("sources", []) if p not in sources]
    out.append("## Source files at commit " + base_id)
    for path in sources:
        out.append(f"### {path}\n{fence(show(repo, base, path))}")
    return "\n\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--base", default="HEAD")
    ap.add_argument("--scenarios", required=True, metavar="DIR")
    ap.add_argument("--allow-pending", action="store_true")
    ap.add_argument("--max-bytes", type=int, default=200000, help="refuse to build a longer task (default 200000)")
    a = ap.parse_args(argv)
    try:
        text = build(os.path.abspath(a.repo), a.base, os.path.abspath(a.scenarios), a.allow_pending)
    except (ValueError, OSError, KeyError) as exc:
        print(f"make_task: {exc}", file=sys.stderr)
        return 2
    size = len(text.encode("utf-8"))
    if size > a.max_bytes:
        print(f"make_task: the task would be {size} bytes, over --max-bytes {a.max_bytes}; name fewer sources",
              file=sys.stderr)
        return 2
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
