"""Reports of layout-team runs, computed only from the files a run leaves: summary.json and chat.jsonl (layout_team.py)
and run.json (teamrun.py). Nothing is typed by hand or carried in memory, so every number can be traced and recomputed;
runs made by layout_team.py alone (no run.json) are read too, without the members' tokens and time.

    report_text(folder)      one run: members, rows (draft -> kept), whole slide, lessons
    aggregate_text(folder)   the runs in folder/rep-* (or the folder itself): mean, min and max
    compare_text(a, b)       two aggregates side by side
"""
import json
import os
import re
import time

ROUND = re.compile(r"round \d+: row (\d+) ")
STATS = (("final_match", "Final match", "{:.3f}"), ("final_strict", "Final strict", "{:.3f}"), ("accepted", "Accepted revisions", "{:.2f}"), ("fixups", "Fix-ups", "{:.2f}"),
         ("minutes", "Minutes", "{:.2f}"), ("tokens", "Tokens", "{:,.0f}"))


def load(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def chat_of(folder):
    out = []
    try:
        with open(os.path.join(folder, "chat.jsonl"), encoding="utf-8") as handle:
            for line in handle:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def fixups(chat):
    """{row: fix-up turns}. Before every FIX prompt the supervisor writes one "fix: ..." control line; its row is the one
    the manager's last "round N: row R ..." line named."""
    rows, row = {}, None
    for m in chat:
        if m.get("from") == "manager":
            found = ROUND.match(m.get("text", ""))
            row = int(found.group(1)) if found else row
        elif m.get("from") == "supervisor" and m.get("kind") == "control" and m.get("text", "").startswith("fix: "):
            rows[row] = rows.get(row, 0) + 1
    return rows


def rows_of(summary, chat):
    """Per row: draft match, kept match and its missing labels, revisions accepted / rejected / invalid, fix-ups."""
    rows = {}

    def entry(n):
        return rows.setdefault(n, {"draft": None, "kept": None, "kept_missing": None, "accepted": 0, "rejected": 0, "invalid": 0,
                                   "fixups": 0})
    for t in summary.get("turns", []):
        r = entry(t["row"])
        if t.get("kind") == "draft":
            if t.get("valid"):
                r["draft"] = r["kept"] = t.get("match")
                r["kept_missing"] = len(t.get("missing") or [])
        elif not t.get("valid"):
            r["invalid"] += 1  # no list the program could draw, even after the fix-ups
        elif t.get("accepted"):
            r["accepted"] += 1
            r["kept"], r["kept_missing"] = t.get("match"), len(t.get("missing") or [])
        else:
            r["rejected"] += 1
    for n, count in fixups(chat).items():
        if n is not None:
            entry(n)["fixups"] += count
    return rows


def team_minutes(chat):
    """The team's working time: first to last line of chat.jsonl."""
    times = [m["t"] for m in chat if isinstance(m.get("t"), (int, float))]
    return (max(times) - min(times)) / 60 if len(times) > 1 else None


def metrics(folder):
    """One run's numbers, or {"failed": why} when it has no summary."""
    summary, run, chat = load(os.path.join(folder, "summary.json")), load(os.path.join(folder, "run.json")), chat_of(folder)
    m = {"name": os.path.basename(os.path.normpath(folder)), "folder": folder}
    if not isinstance(summary, dict) or "final" not in summary:
        m["failed"] = (run or {}).get("error") or "no summary.json"
        return m
    rows = rows_of(summary, chat)
    tokens = [x.get("tokens") for x in (run or {}).get("members", [])]
    m.update(final_match=summary["final"]["match"], final_strict=summary["final"].get("strict"), psnr=summary["final"]["psnr"], missing=len(summary["final"]["missing"]),
             accepted=sum(r["accepted"] for r in rows.values()), rejected=sum(r["rejected"] for r in rows.values()),
             invalid=sum(r["invalid"] for r in rows.values()), fixups=sum(fixups(chat).values()), minutes=team_minutes(chat),
             tokens=sum(tokens) if tokens and all(isinstance(x, (int, float)) for x in tokens) else None)
    return m


def runs_in(folder):
    reps = [name for name in os.listdir(folder) if re.match(r"rep-\d+$", name) and os.path.isdir(os.path.join(folder, name))]
    return [os.path.join(folder, name) for name in sorted(reps, key=lambda n: int(n[4:]))] or [folder]


def title_of(folder):
    spec = load(os.path.join(folder, "spec.json")) or load(os.path.join(folder, "run.json")) or {}
    return spec.get("title") or os.path.basename(os.path.normpath(os.path.abspath(folder)))


def num(value, form):
    return "-" if value is None else form.format(value)  # an infinite PSNR (the same picture) prints as inf


def stats(runs, key):
    values = [r[key] for r in runs if r.get(key) is not None]
    if not values:
        return None
    return sum(values) / len(values), min(values), max(values), len(values)


def table(head, rows):
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    return out + ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]


def report_text(folder):
    summary, run, chat = load(os.path.join(folder, "summary.json")), load(os.path.join(folder, "run.json")) or {}, chat_of(folder)
    lines = [f"# Layout team run: {run.get('title') or os.path.basename(os.path.normpath(os.path.abspath(folder)))}", ""]
    facts = [f"run {run['rep']}"] if run.get("rep") else []
    if run.get("started"):
        facts.append("started " + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(run["started"])))
    minutes = team_minutes(chat)
    facts.append(f"team time {num(minutes, '{:.2f}')} min (first to last line of chat.jsonl)")
    if run.get("seconds") is not None:
        facts.append(f"wall time {run['seconds'] / 60:.2f} min")
    if run.get("renderer"):
        facts.append(f"renderer {run['renderer']}")
    lines += [" · ".join(facts), ""]
    if run.get("error"):
        lines += [f"**This run failed:** {run['error']}", ""]
    if run.get("members"):
        lines += table(["Member", "Role", "Backend", "Model", "Sessions", "Turns", "Failed turns", "Tokens", "Time in turns (s)"],
                       [[m["name"], m["role"], m["backend"], m.get("model") or "(default)", m["sessions"], m["turns"],
                         sum(v for k, v in m.get("states", {}).items() if k != "idle"), num(m.get("tokens"), "{:,}"),
                         f"{m['seconds']:.1f}"] for m in run["members"]]) + [""]
    else:
        lines += ["(the run stopped before its members were set up)" if run else
                  "(no run.json: members, tokens and time are unknown)", ""]
    if not isinstance(summary, dict) or "final" not in summary:
        lines += ["No summary.json: the team did not finish. Last lines of chat.jsonl:", ""]
        lines += [f"- {m.get('from')} -> {m.get('to')}: {str(m.get('text'))[:160]}" for m in chat[-5:]] or ["- (none)"]
        return "\n".join(lines) + "\n"
    rows = rows_of(summary, chat)
    rule = ("the strict score (match, borders and text colour together; scoring.py)" if summary["final"].get("score_mode") == "strict"
            else "the match")
    lines += ["## Rows", "", "Match: share of colour squares that agree with the original (1 = the same picture). Draft: the "
              "first list the program could draw; kept: the version kept at the end. A revision is kept only when its score "
              f"({rule} minus a penalty for every missing label) is higher.", ""]
    body = [[n, num(r["draft"], "{:.3f}"), num(r["kept"], "{:.3f}"), r["accepted"], r["rejected"], r["invalid"], r["fixups"],
             num(r["kept_missing"], "{}")] for n, r in sorted(rows.items())]
    body.append(["all", "", "", sum(r["accepted"] for r in rows.values()), sum(r["rejected"] for r in rows.values()),
                 sum(r["invalid"] for r in rows.values()), sum(fixups(chat).values()), ""])
    lines += table(["Row", "Draft", "Kept", "Revisions accepted", "Rejected", "Invalid", "Fix-ups", "Labels missing (kept)"], body)
    final = summary["final"]
    strict = f" · strict {final['strict']:.3f}" if final.get("strict") is not None else ""  # runs before scoring.py have none
    lines += ["", "## Whole slide", "", f"Match {final['match']:.3f}{strict} · PSNR {num(final['psnr'], '{:.2f}')} dB · required labels "
              f"missing: {len(final['missing'])}" + (f" ({', '.join(final['missing'])})" if final["missing"] else ""), ""]
    lessons = summary.get("lessons") or {}
    lines += [f"## Lessons ({len(lessons)})", ""] + [f"- {text} ({n} time{'s' if n > 1 else ''})" for text, n in lessons.items()]
    lines += ([] if lessons else ["(none)"]) + ["", "Computed by runreport.py from summary.json, chat.jsonl and run.json in this folder."]
    return "\n".join(lines) + "\n"


def write_report(folder):
    path = os.path.join(folder, "report.md")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(report_text(folder))
    return path


def aggregate_text(folder):
    runs = [metrics(f) for f in runs_in(folder)]
    good = [r for r in runs if "failed" not in r]
    lines = [f"# {title_of(folder)}: {len(good)} of {len(runs)} runs", "",
             "Computed by runreport.py from each run's summary.json, chat.jsonl and run.json.", ""]
    body = []
    for key, label, form in STATS:
        s = stats(good, key)
        body.append([label] + (["-", "-", "-"] if s is None else [form.format(s[0]), num(s[1], form), num(s[2], form)]))
    lines += table(["", "mean", "min", "max"], body) + [""]
    lines += table(["Run", "Final match", "Final strict", "PSNR (dB)", "Accepted", "Rejected", "Invalid", "Fix-ups", "Minutes", "Tokens"],
                   [[r["name"], f"{r['final_match']:.3f}", num(r["final_strict"], "{:.3f}"), num(r["psnr"], "{:.2f}"), r["accepted"], r["rejected"], r["invalid"],
                     r["fixups"], num(r["minutes"], "{:.2f}"), num(r["tokens"], "{:,}")] for r in good])
    failed = [r for r in runs if "failed" in r]
    if failed:
        lines += ["", "Not counted (no summary.json):"] + [f"- {r['name']}: {r['failed']}" for r in failed]
    return "\n".join(lines) + "\n"


def write_aggregate(folder):
    path = os.path.join(folder, "aggregate.md")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(aggregate_text(folder))
    return path


def compare_text(a, b):
    sides = []
    for folder in (a, b):
        runs = [metrics(f) for f in runs_in(folder)]
        sides.append((title_of(folder), [r for r in runs if "failed" not in r], len(runs)))
    rows = [["Runs counted"] + [f"{len(good)} of {total}" for _, good, total in sides]]
    for key, label, form in STATS:
        row = [label]
        for _, good, _ in sides:
            s = stats(good, key)
            row.append("-" if s is None else f"{form.format(s[0])} ({num(s[1], form)} to {num(s[2], form)})")
        rows.append(row)
    lines = [f"A: {sides[0][0]} ({a})", f"B: {sides[1][0]} ({b})", "", "mean (min to max) over the runs that finished", ""]
    return "\n".join(lines + table(["", "A", "B"], rows))
