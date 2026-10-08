"""A team member that is a program, for trying a self-improvement run without spending on models: it reads the
prompt on stdin, ignores it, and answers with a patch file it was given (one per turn, cycling).

    --member 'a=command:python3 examples/self_improve/patch_member.py fix.diff'
    --member 'b=command:python3 examples/self_improve/patch_member.py broken.diff fix.diff'

The turn number comes from HERDR_TURN_FILE (a counter file it keeps next to the first patch) so a member can hand in
a different patch each turn. Standard library only.
"""
import os
import sys


def main(argv=None):
    patches = (argv if argv is not None else sys.argv[1:])
    if not patches:
        print("FAILED: patch_member.py was given no patch file")
        return 0
    sys.stdin.read()
    counter = os.environ.get("HERDR_TURN_FILE") or os.path.abspath(patches[0]) + f".{os.environ.get('HERDR_MEMBER', 'm')}.turn"
    try:
        with open(counter) as handle:
            turn = int(handle.read().strip() or 0)
    except (OSError, ValueError):
        turn = 0
    with open(counter, "w") as handle:
        handle.write(str(turn + 1))
    path = patches[turn % len(patches)]
    with open(path, encoding="utf-8") as handle:
        diff = handle.read()
    mark = "~~~~" if "```" in diff else "```"
    print(f"SUMMARY: hand in {os.path.basename(path)}")
    print("PARENTS: none")
    print(mark + "diff")
    print(diff.rstrip("\n"))
    print(mark)
    return 0


if __name__ == "__main__":
    sys.exit(main())
