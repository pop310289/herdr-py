"""A planner that is a program, not a model, for herdr_py.engine: it reads the planner's prompt on stdin (the team's
verified results, failures and todos, built by the engine from its records) and prints a todo list change as one
fenced JSON block. At the start it gives every member a todo to make a first answer; after that, whenever members
are free, it adds todos to improve the best verified result, naming that result as the parent.

    --planner 'plan=command:python3 examples/engine/planner.py'

This is how a rule-based scheduler you already have becomes the planner. Standard library only.
"""
import json
import re
import sys

RESULT = re.compile(r"^- (k[0-9a-f]{12}) by (\S+): score (\S+):", re.M)
TODO = re.compile(r"^- (t[0-9a-f]{12}) \[(open|taken by [^\]]+)\]", re.M)
MEMBER = re.compile(r"^- ([A-Za-z0-9_.-]+)(?::|$)", re.M)


def main():
    prompt = sys.stdin.read()
    members_part = prompt.split("Members:\n", 1)[1].split("\n\n", 1)[0] if "Members:\n" in prompt else ""
    members = MEMBER.findall(members_part)
    results = [(rid, who, float(score)) for rid, who, score in RESULT.findall(prompt)]
    busy = TODO.findall(prompt)  # open or taken: someone will do them
    left = re.search(r"Budget: (\d+) member turns left", prompt)
    left = int(left.group(1)) if left else 0
    add = []
    if not results and not busy:
        add = [{"text": "Make a first packing: start from the grid and improve it.", "for": m, "parents": []} for m in members]
    elif results:
        best = max(results, key=lambda r: r[2])
        free = max(0, min(len(members), left) - len(busy))
        add = [{"text": f"Improve the best packing so far ({best[0]}, score {best[2]:.6g}).", "for": None,
                "parents": [best[0]]} for _ in range(free)]
    reply = {"add": add, "drop": [], "done": left == 0,
             "why": f"{len(results)} verified results, {len(busy)} todos open or taken, {left} turns left"}
    print("```json\n" + json.dumps(reply) + "\n```")


if __name__ == "__main__":
    main()
