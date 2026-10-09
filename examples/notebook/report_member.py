"""A team member that is a program, for the notebook demo (report_task.md); its name says its role.

    --member 'collector=command:python3 examples/notebook/report_member.py'

collector: the visitors of the made-up place the task names, the first half of the year; given a first half (a todo
that builds on it), the whole year. writer: a skill for drawing a bar chart as inline SVG; when another task's skill
is in its folder as reference material (reference/<page>/<entry>.txt), it adapts that one at once. builder: the
report page, built from the best data and the skill it was shown, following the skill's steps (the bars get a <title>
when the skill says so).
It reads the engine's prompt on stdin and prints a reply in the team's format. Standard library only.
"""
import html
import json
import os
import random
import re
import sys

SHOWN = re.compile(r"^(?:- |It builds on )(k[0-9a-f]{12}) by \S+[: ][^\n]*\n(```|~~~~)\n(.*?)\n\2", re.M | re.S)
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
VISITORS = [1180, 960, 1320, 1510, 1730, 2240, 2610, 2480, 1890, 1420, 1050, 1630]
SKILL_V1 = """---
name: svg-bar-chart
---
1. Scale the largest value to the chart's height and every bar by the same factor.
2. Draw one <rect class="bar"> per value, side by side, with a gap of a fifth of a bar.
3. Put the label of each bar under it and the axis at the bottom, starting from 0."""
SKILL_V2 = SKILL_V1 + """
4. Give every bar a <title> with its label and value, so a reader can hover it to read the number.
5. Keep the chart's width fixed and let the page scale it (viewBox), so it fits a phone.
6. Say in a sentence above the chart what it shows and the total, so the page reads without the chart.
7. Use one colour for every bar; colour says nothing here, so it should not vary.
8. Check the chart at phone width: no label may overlap its neighbour."""


def shown(prompt):
    """{kind: [(id, body)]} for every answer shown in the prompt (its todo's parents and the team's best results)."""
    out = {}
    for eid, _, text in SHOWN.findall(prompt):
        first, _, body = text.partition("\n")
        kind = first.split(":", 1)[1].strip().lower() if first.lower().startswith("artifact") else None
        if kind and (eid, body) not in out.get(kind, []):
            out.setdefault(kind, []).append((eid, body))
    return out


def reply(summary, parents, kind, body):
    return f"SUMMARY: {summary}\nPARENTS: {', '.join(parents) or 'none'}\n```\nARTIFACT: {kind}\n{body}\n```"


def place_of(prompt):
    m = re.search(r"visitors of a small made-up \w+, the ([^.\n]+)\.", prompt)
    return m.group(1).strip() if m else "Riverside Clock Museum"


def visitors_of(place):
    if place == "Riverside Clock Museum":
        return VISITORS
    rng = random.Random(place)  # made-up numbers, the same every time for the same place
    return [rng.randrange(600, 3000) for _ in MONTHS]


def reference_skills():
    """Skill files another task left in this member's folder as reference material."""
    found = []
    for root, _, files in os.walk("reference"):
        for name in sorted(files):
            if name.endswith(".txt"):
                with open(os.path.join(root, name), encoding="utf-8") as handle:
                    text = handle.read()
                if text.lstrip().lower().startswith("artifact: skill"):
                    found.append((os.path.join(root, name), text))
    return found


def collector(prompt, seen):
    data = [(eid, json.loads(body)) for eid, body in seen.get("data", [])]
    if data:
        eid, d = max(data, key=lambda x: len(x[1].get("months") or []))
        if len(d.get("months") or []) < 12:
            whole = {"title": d.get("title"), "months": MONTHS, "visitors": visitors_of(place_of(prompt))}
            return reply("the whole year: the second half added to the first", [eid], "data", json.dumps(whole))
    place = place_of(prompt)
    first = {"title": f"{place}: visitors by month", "months": MONTHS[:6], "visitors": visitors_of(place)[:6]}
    return reply("the first half of the year from the visitor book", [], "data", json.dumps(first))


def writer(prompt, seen):
    skills = seen.get("skill", [])
    if skills:
        return reply("the bar chart skill with titles on bars and a viewBox", [skills[0][0]], "skill", SKILL_V2)
    borrowed = [path for path, text in reference_skills() if "<title>" in text]
    if borrowed:  # another task already worked this out: adapt its skill instead of starting over
        return reply(f"the bar chart skill adapted from {borrowed[0]} (another task's reference material)", [], "skill", SKILL_V2)
    return reply("a bar chart skill: scale, bars, labels", [], "skill", SKILL_V1)


def builder(prompt, seen):
    data = [(eid, json.loads(body)) for eid, body in seen.get("data", [])]
    if not data:
        return "FAILED: no verified data was shown to me, so there is nothing to draw"
    eid, d = max(data, key=lambda x: len(x[1].get("months") or []))
    skills = seen.get("skill", [])
    skill = max(skills, key=lambda s: s[1].count("\n")) if skills else None
    titled = bool(skill and "<title>" in skill[1])
    w, h, top = 30, 160, max(d["visitors"])
    bars = []
    for i, (m, v) in enumerate(zip(d["months"], d["visitors"])):
        bh = round(v / top * h)
        tip = f"<title>{html.escape(m)}: {v}</title>" if titled else ""
        bars.append(f'<rect class="bar" x="{i * (w + 6)}" y="{h - bh}" width="{w}" height="{bh}">{tip}</rect>'
                    f'<text x="{i * (w + 6) + w / 2}" y="{h + 14}" text-anchor="middle" font-size="10">{html.escape(m)}</text>')
    page = (f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width\">"
            f"<title>{html.escape(d['title'])}</title></head><body style=\"font-family:sans-serif;max-width:640px;margin:2em auto\">"
            f"<h1>{html.escape(d['title'])}</h1><p>{len(bars)} months, {sum(d['visitors'])} visitors.</p>"
            f"<svg viewBox=\"-4 0 {len(bars) * (w + 6)} {h + 20}\" style=\"width:100%\" role=\"img\">{''.join(bars)}</svg></body></html>")
    parents = [eid] + ([skill[0]] if skill else [])
    return reply(f"the report page: {len(bars)} bars" + (" with titles, as the skill says" if titled else ""), parents, "page", page)


def main():
    prompt = sys.stdin.read()
    role = os.environ.get("HERDR_MEMBER", "")
    act = {"collector": collector, "writer": writer, "builder": builder}.get(role)
    print(act(prompt, shown(prompt)) if act else f"FAILED: no role called {role!r} in this demo")


if __name__ == "__main__":
    main()
