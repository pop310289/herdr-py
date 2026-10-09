"""The planner of the notebook demo (report_task.md), a program: it reads the planner's prompt on stdin and prints the
todo list change as one fenced JSON block. The roles are the members' names: the collector's results are data, the
writer's skills, the builder's pages.

    --planner 'plan=command:python3 examples/notebook/report_planner.py'

First data and a skill; then the whole year (when the best data scores under 60) and a better skill (when the best
scores under 20); then a page built on the best data and the best skill; done when a page scores 100.
Standard library only.
"""
import json
import re
import sys

RESULT = re.compile(r"^- (k[0-9a-f]{12}) by (\S+): score (\S+):", re.M)
OPEN = re.compile(r"^- t[0-9a-f]{12} \[(?:open|taken by [^\]]+)\] (?:for )?(\S+)?", re.M)


def main():
    prompt = sys.stdin.read()
    results = [(rid, who, float(score)) for rid, who, score in RESULT.findall(prompt)]
    best = {}
    for rid, who, score in results:
        if who not in best or score > best[who][1]:
            best[who] = (rid, score)
    busy = len(OPEN.findall(prompt))
    add, done = [], False
    if busy:
        pass  # wait for the todos given out
    elif "builder" in best and best["builder"][1] >= 100:
        done = True
    else:
        if "collector" not in best:
            add.append({"text": "Collect the visitors of the first half of the year.", "for": "collector", "parents": []})
        elif best["collector"][1] < 60:
            add.append({"text": "Add the second half of the year to the data.", "for": "collector", "parents": [best["collector"][0]]})
        if "writer" not in best:
            add.append({"text": "Write a skill for drawing a bar chart as inline SVG.", "for": "writer", "parents": []})
        elif best["writer"][1] < 20:
            add.append({"text": "Improve the bar chart skill: a reader should be able to read every bar.", "for": "writer",
                        "parents": [best["writer"][0]]})
        if not add and "collector" in best and "writer" in best:
            add.append({"text": "Build the report page from the best data, following the best skill.", "for": "builder",
                        "parents": [best["collector"][0], best["writer"][0]]})
    print("```json\n" + json.dumps({"add": add, "drop": [], "done": done,
                                    "why": f"{len(results)} verified results, {busy} todos given out"}) + "\n```")


if __name__ == "__main__":
    main()
