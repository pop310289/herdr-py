#!/usr/bin/env python3
"""Check the checkers, inside the RHEL 8 bench image (Python 3.6). For every task:
  template  (untouched)                 visible check FAIL, hidden grader FAIL
  partial   (a typical half-done answer) visible check PASS, hidden grader FAIL  <- what the validator role should catch
  reference (a correct answer)          visible check PASS, hidden grader PASS
Run this after changing any task.

usage: validate.py [--image herdr-py/p23:rhel8]
"""
import argparse
import os
import shutil
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TASKS = os.path.join(HERE, "tasks")
POST = {"count": "python3 count/count.py > count/answer.txt", "csvtotals": "python3 totals.py"}  # the "run it" step


def in_image(image, work, command):
    p = subprocess.run(["docker", "run", "--rm", "--network", "none", "--user", "%d:%d" % (os.getuid(), os.getgid()),
                        "-v", work + ":/work", "-v", TASKS + ":/tasks:ro", "-w", "/work", image, "sh", "-c", command],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=180)
    return p.returncode, p.stdout.strip().splitlines()[-1] if p.stdout.strip() else ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default="herdr-py/p23:rhel8")
    a = ap.parse_args()
    base = tempfile.mkdtemp(dir=os.path.join(os.path.expanduser("~"), ".cache"))  # colima only shares $HOME
    bad = 0
    try:
        for name in sorted(os.listdir(TASKS)):
            for variant in ("template", "partial", "reference"):
                work = os.path.join(base, name + "-" + variant)
                shutil.copytree(os.path.join(TASKS, name, "work"), work)
                if variant != "template":
                    ref = os.path.join(HERE, variant, name)
                    for root, _, files in os.walk(ref):
                        for f in files:
                            dest = os.path.join(work, os.path.relpath(os.path.join(root, f), ref))
                            os.makedirs(os.path.dirname(dest), exist_ok=True)
                            shutil.copy(os.path.join(root, f), dest)
                    if name in POST:
                        in_image(a.image, work, POST[name])
                check = in_image(a.image, work, "python3 check.py")
                grade = in_image(a.image, work, "python3 /tasks/%s/grade.py /work" % name)
                want = {"template": (False, False), "partial": (True, False), "reference": (True, True)}[variant]
                ok = (check[0] == 0, grade[0] == 0) == want
                bad += not ok
                print("%-10s %-9s check=%s grade=%s %s  | %s | %s" % (name, variant, check[0], grade[0], "OK" if ok else "WRONG",
                                                                       check[1][:60], grade[1][:60]))
    finally:
        shutil.rmtree(base, ignore_errors=True)
    print("all checkers behave" if not bad else "%d checker problem(s)" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
