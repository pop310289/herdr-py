"""One HTML page for a cooperative run (coop.py): every member's every turn at a glance, to see what went wrong.

    python3 -m herdr_py.coopview RUN_DIR                 # writes RUN_DIR/view.html
    python3 -m herdr_py.coopview RUN_DIR --html page.html

coop.py writes RUN_DIR/view.html itself after every round, so the page can be opened while the run goes on (reload it).
The page is one file with its style inside and no script: it opens from disk anywhere. It only reads the run folder:
  run.jsonl              every turn: state, seconds, tokens, entry, verdict, parents, problems
  kb/events.jsonl        the entries (summaries, the judge's reasons, who built on whom)
  summary.json           the totals, once the run has ended (or stopped)
  members/codex/events.jsonl, members/claude/events.jsonl
                         the backends' logs: how many commands a Codex member ran in a turn, and how a turn that did
                         not end normally ended (exit code, the last lines of stderr)
Rows are rounds and columns are members, so a single-member run (mode S) is one column. Every text that came from a
member or a judge is escaped. Standard library only; Python 3.6+.
"""
import argparse
import collections
import html
import json
import os
import sys

from .viewstyle import TOKENS

SCORE_DIGITS = 13  # scores that differ in the 10th digit must not look equal

STYLE = TOKENS + """
:root { font-family: system-ui, -apple-system, "Segoe UI", "PingFang TC", "Noto Sans CJK TC", sans-serif; }
* { box-sizing: border-box; }
html, body { margin: 0; background: var(--bg); color: var(--text); }
body { font-size: 15px; line-height: 1.45; }
main { max-width: 1400px; margin: 0 auto; padding: 12px 16px 48px; }
h1 { font-size: 20px; margin: 4px 0; }
h2 { font-size: 14px; color: var(--muted); margin: 22px 0 8px; text-transform: uppercase; letter-spacing: .04em; }
.sub { color: var(--muted); font-size: 14px; overflow-wrap: anywhere; }
.stopped { margin: 10px 0; padding: 10px 14px; border: 2px solid var(--bad); border-radius: 10px; font-weight: 600; }
.tiles { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 8px; }
.tile { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 8px 12px;
  box-shadow: inset 0 1px 0 var(--hi); }
.tile b { display: block; font-size: 20px; font-variant-numeric: tabular-nums; }
.tile span { color: var(--muted); font-size: 13px; }
.tile.bad b { color: var(--bad); }
.wrap { overflow-x: auto; }
table { border-collapse: separate; border-spacing: 6px; }
th { color: var(--muted); font-size: 13px; font-weight: 600; text-align: left; padding: 0 4px; white-space: nowrap; }
td.cell { vertical-align: top; min-width: 220px; max-width: 340px; background: var(--panel); border: 1px solid var(--line);
  border-left: 5px solid var(--idle); border-radius: 8px; padding: 6px 10px; }
td.cell.valid { border-left-color: var(--ok); }
td.cell.invalid, td.cell.failure, td.cell.noanswer, td.cell.timeout { border-left-color: var(--warn); }
td.cell.error, td.cell.aborted, td.cell.infra { border-left-color: var(--bad); }
td.cell.best { box-shadow: inset 0 0 0 2px var(--ok); }
.badge { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 13px; font-weight: 600;
  background: var(--idle); color: var(--chip-ink); }
.valid .badge { background: var(--ok); } .error .badge, .aborted .badge, .infra .badge { background: var(--bad); }
.invalid .badge, .failure .badge, .noanswer .badge, .timeout .badge { background: var(--warn); }
.score { font-weight: 700; font-variant-numeric: tabular-nums; margin-left: 6px; }
.meta { color: var(--muted); font-size: 13px; font-variant-numeric: tabular-nums; }
.from { font-size: 13px; margin-top: 2px; }
.why { font-size: 13px; margin-top: 4px; overflow-wrap: anywhere; }
details { margin-top: 4px; font-size: 13px; }
summary { cursor: pointer; color: var(--muted); }
pre { white-space: pre-wrap; overflow-wrap: anywhere; margin: 4px 0; padding: 6px; background: var(--bg);
  border-radius: 6px; font-size: 12px; max-height: 240px; overflow-y: auto; }
code { font-size: 12px; }
ul.plain { margin: 0; padding-left: 18px; }
ul.plain li { margin: 2px 0; overflow-wrap: anywhere; }
table.list { border-spacing: 0; width: 100%; background: var(--panel); border: 1px solid var(--line); border-radius: 10px; }
table.list td.nw { white-space: nowrap; }
table.list td.id { white-space: nowrap; font-family: ui-monospace, Menlo, monospace; font-size: 12px; }
table.list th, table.list td { padding: 5px 8px; border-bottom: 1px solid var(--line); font-size: 13px; text-align: left;
  vertical-align: top; overflow-wrap: anywhere; }
svg text { fill: var(--muted); font-size: 12px; }
.legend { color: var(--muted); font-size: 13px; margin: 4px 0 0; }
"""


def esc(value):
    return html.escape("" if value is None else str(value), quote=True)


def read_jsonl(path):
    rows, bad = [], 0
    if os.path.exists(path):
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    bad += 1  # a line still being written
    return rows, bad


def entries_of(run_dir):
    proposals, verdicts = collections.OrderedDict(), {}
    for e in read_jsonl(os.path.join(run_dir, "kb", "events.jsonl"))[0]:
        if e.get("type") == "propose" and e.get("id") not in proposals:
            proposals[e["id"]] = e
        elif e.get("type") == "verdict" and e.get("id") in proposals:
            verdicts[e["id"]] = e
    out = collections.OrderedDict()
    for eid, p in proposals.items():
        v = verdicts.get(eid, {})
        out[eid] = dict(p, status=v.get("status", "unjudged"), score=v.get("score"), detail=v.get("detail", ""))
    first = {}
    for e in out.values():
        e["adopted_by"] = [c["id"] for c in out.values() if e["id"] in (c.get("parents") or [])]
        e["duplicate_of"] = first.get(e.get("sha")) if e.get("sha") else None
        if e.get("sha") and e["sha"] not in first:
            first[e["sha"]] = e["id"]
    return out


def backend_turns(run_dir):
    """member -> [one dict per turn the backend started]: commands run, exit row (non-normal ends), from the logs of
    the Codex and Claude backends. The n-th turn a backend started for a member is that member's n-th turn."""
    found = collections.defaultdict(list)
    for kind in ("codex", "claude"):
        rows = read_jsonl(os.path.join(run_dir, "members", kind, "events.jsonl"))[0]
        for row in rows:
            name = row.get("agent")
            if name is None:
                continue
            if "argv" in row:
                found[name].append({"backend": kind, "commands": 0, "exit": None, "timeout": None, "stderr": ""})
                continue
            if not found[name]:
                continue
            turn = found[name][-1]
            event = row.get("event") or {}
            if event.get("type") == "item.completed" and (event.get("item") or {}).get("type") == "command_execution":
                turn["commands"] += 1
            if "exit" in row:
                turn.update(exit=row.get("exit"), timeout=row.get("timeout"), stderr=row.get("stderr") or "")
    return found


def outcome(turn, entries):
    """(css class, badge text, one line saying why) for a turn."""
    state = turn.get("state")
    if state != "idle":
        return state if state in ("timeout", "error", "aborted") else "error", state, turn.get("problem", "")
    if "problem" in turn:
        return "noanswer", "no answer", turn["problem"]
    entry = entries.get(turn.get("entry"), {})
    if turn.get("kind") == "failure":
        return "failure", "FAILED", entry.get("summary", "")
    status = turn.get("status")
    if status == "valid":
        return "valid", "valid", ""
    if status == "invalid":
        return "invalid", "invalid", entry.get("detail", "")
    if status == "infra_error":
        return "infra", "judge error", entry.get("detail", "")
    return "noanswer", status or "?", ""


def fmt_num(value, digits=6):
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}g}"
    return str(value)


def fmt_tokens(value):
    if value is None:
        return "tokens -"
    return f"{value / 1000:.1f}k tok" if value >= 1000 else f"{value} tok"


def cell(turn, entries, backend, best_id, unit="round"):
    css, badge, why = outcome(turn, entries)
    entry = entries.get(turn.get("entry"), {})
    classes = ["cell", css] + (["best"] if best_id and turn.get("entry") == best_id else [])
    head = f'<span class="badge">{esc(badge)}</span>'
    if turn.get("score") is not None:
        head += f'<span class="score">{esc(fmt_num(turn["score"], SCORE_DIGITS))}</span>'
    if turn.get("repeat"):
        head += ' <span class="meta" title="the same entry again: recorded once, not judged again">resent</span>'
    meta = [f"{turn.get('seconds', 0):.0f} s", fmt_tokens(turn.get("tokens"))]
    if backend is not None:
        meta.append(f"{backend['commands']} commands")
    parts = [head, f'<div class="meta">{esc(" · ".join(meta))}</div>']
    if turn.get("entry"):
        parts.append(f'<div class="meta"><code>{esc(turn["entry"])}</code></div>')
    for parent in turn.get("parents") or []:
        p = entries.get(parent, {})
        mine = p.get("member") == turn.get("member")
        parts.append(f'<div class="from">{"↳ own" if mine else "↳ built on"} <code>{esc(parent)}</code> by {esc(p.get("member"))} '
                     f'({unit} {esc(p.get("round"))}, {esc(fmt_num(p.get("score"), SCORE_DIGITS))})</div>')
    if turn.get("parents_dropped"):
        parts.append(f'<div class="why">named entries it was never shown (ignored): {esc(", ".join(turn["parents_dropped"]))}</div>')
    if why:
        parts.append(f'<div class="why">{esc(why[:400])}</div>')
    more = []
    if entry.get("summary") and turn.get("kind") == "result":
        more.append(f"<div>summary: {esc(entry['summary'])}</div>")
    if turn.get("reply_tail"):
        more.append(f"<div>end of the reply:</div><pre>{esc(turn['reply_tail'])}</pre>")
    if backend is not None and (backend["exit"] is not None or backend["timeout"]):
        line = f"exit {backend['exit']}" + (f", stopped at the {backend['timeout']} s limit" if backend["timeout"] else "")
        more.append(f"<div>{esc(backend['backend'])}: {esc(line)}</div>")
        if backend["stderr"].strip():
            more.append(f"<pre>{esc(backend['stderr'][-500:])}</pre>")
    if more:
        parts.append("<details><summary>details</summary>" + "".join(more) + "</details>")
    return f'<td class="{" ".join(classes)}">' + "".join(parts) + "</td>"


def progress_svg(progress):
    """Best verified score after each round, as a small line chart (or nothing to draw)."""
    points = [(i + 1, p) for i, p in enumerate(progress) if p is not None]
    if not points:
        return '<p class="sub">No verified answer yet.</p>'
    w, h, pad = 640, 160, 36
    lo, hi = min(p for _, p in points), max(p for _, p in points)
    span = (hi - lo) or abs(hi) or 1.0
    lo, hi = lo - span * 0.1, hi + span * 0.1
    n = max(len(progress), 2)
    x = lambda i: pad + (w - 2 * pad) * (i - 1) / (n - 1)  # noqa: E731
    y = lambda v: h - pad + (pad * 2 - h) * (v - lo) / (hi - lo)  # noqa: E731
    line = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in points)
    dots = "".join(f'<circle cx="{x(i):.1f}" cy="{y(v):.1f}" r="3.5" fill="var(--accent)"><title>round {i}: {v:.10g}</title></circle>'
                   for i, v in points)
    top, bottom = max(p for _, p in points), min(p for _, p in points)
    labels = (f'<text x="{pad}" y="{h - 8}">round 1</text><text x="{w - pad}" y="{h - 8}" text-anchor="end">round {len(progress)}</text>'
              f'<text x="4" y="{y(top) - 6:.1f}">{top:.{SCORE_DIGITS}g}</text>'
              + (f'<text x="4" y="{y(bottom) + 16:.1f}">{bottom:.{SCORE_DIGITS}g}</text>' if bottom != top else ""))
    scale = (f'<p class="legend">The axis does not start at 0: from the first to the best point is {top - bottom:+.3g}.</p>'
             if bottom != top else "")
    return (f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" aria-label="best verified score after each round">'
            f'<polyline points="{line}" fill="none" stroke="var(--accent)" stroke-width="2"/>{dots}{labels}</svg>' + scale)


def build(run_dir):
    """The page as a string."""
    turns, bad_lines = read_jsonl(os.path.join(run_dir, "run.jsonl"))
    entries = entries_of(run_dir)
    summary = None
    if os.path.exists(os.path.join(run_dir, "summary.json")):
        with open(os.path.join(run_dir, "summary.json"), encoding="utf-8") as handle:
            summary = json.load(handle)
    backends = backend_turns(run_dir)
    members = list(collections.OrderedDict.fromkeys(t["member"] for t in turns))
    if summary and summary.get("members"):
        members = list(collections.OrderedDict.fromkeys(list(summary["members"]) + members))
    by_cell, seen = {}, collections.Counter()
    for t in turns:
        index = seen[t["member"]]
        seen[t["member"]] += 1
        found = backends.get(t["member"]) or []
        by_cell[(t["round"], t["member"])] = (t, found[index] if index < len(found) else None)
    rounds = sorted({t["round"] for t in turns})
    valid = [e for e in entries.values() if e["status"] == "valid"]
    best = max(valid, key=lambda e: e["score"], default=None)
    mode = (summary or {}).get("mode", "?")
    counts = collections.Counter(outcome(t, entries)[0] for t in turns)
    tokens = [t["tokens"] for t in turns if t.get("tokens") is not None]
    tiles = [("best verified score", fmt_num(best["score"], SCORE_DIGITS) if best else "-", ""),
             ("turns", len(turns), ""), ("valid answers", counts["valid"], ""),
             ("invalid", counts["invalid"], "bad" if counts["invalid"] else ""),
             ("no answer / FAILED", counts["noanswer"] + counts["failure"], "bad" if counts["noanswer"] + counts["failure"] else ""),
             ("timeouts", counts["timeout"], "bad" if counts["timeout"] else ""),
             ("backend or judge errors", counts["error"] + counts["aborted"] + counts["infra"],
              "bad" if counts["error"] + counts["aborted"] + counts["infra"] else ""),
             ("tokens", f"{sum(tokens):,}" if tokens else "-", ""),
             ("seconds in turns", f"{sum(t.get('seconds', 0) for t in turns):,.0f}", "")]
    if summary:
        rate = lambda v: "-" if v is None else f"{v:.2f}"  # noqa: E731
        tiles += [("adoption rate", rate(summary.get("adoption_rate")), ""),
                  ("improved after adoption", rate(summary.get("improved_after_adoption")), ""),
                  ("duplicate rate", rate(summary.get("duplicate_rate")), "")]
    out = ["<!doctype html>", '<html lang="en"><head><meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width, initial-scale=1">',
           f"<title>coop {esc(mode)} · {esc(os.path.basename(os.path.abspath(run_dir)))}</title>",
           f"<style>{STYLE}</style></head><body><main>",
           f"<h1>Cooperative run · mode {esc(mode)}</h1>",
           f'<div class="sub">{esc(os.path.abspath(run_dir))} · {len(members)} member(s) · {len(rounds)} round(s)'
           + ("" if summary else " · <b>still running</b> (reload for news)") + "</div>"]
    stopped = (summary or {}).get("stopped")
    if stopped:
        out.append(f'<div class="stopped">Stopped after round {esc(stopped.get("round"))} of {esc(stopped.get("of"))}: '
                   + esc("; ".join(stopped.get("why") or [])) + "</div>")
    if bad_lines:
        out.append(f'<div class="sub">{bad_lines} line(s) of run.jsonl could not be read (still being written?)</div>')
    out.append('<div class="tiles">' + "".join(
        f'<div class="tile {css}"><b>{esc(v)}</b><span>{esc(k)}</span></div>' for k, v, css in tiles) + "</div>")
    out.append("<h2>Every turn</h2>")
    out.append('<p class="legend">green: valid · amber: invalid, FAILED, no answer or timeout · red: the backend or the '
               'judge broke · outlined: the best answer · “built on” names a teammate\'s entry; “own” its own earlier one</p>')
    out.append('<div class="wrap"><table><tr><th></th>' + "".join(f"<th>{esc(m)}</th>" for m in members) + "</tr>")
    for r in rounds:
        row = [f"<th>{'turn' if mode == 'S' else 'round'} {esc(r)}</th>"]
        for m in members:
            if (r, m) in by_cell:
                t, b = by_cell[(r, m)]
                row.append(cell(t, entries, b, best["id"] if best else None, "turn" if mode == "S" else "round"))
            else:
                row.append("<td></td>")
        out.append("<tr>" + "".join(row) + "</tr>")
    out.append("</table></div>")
    out.append("<h2>Best after each round</h2>")
    progress = [p.get("best") for p in (summary or {}).get("progress", [])]
    if not progress:  # still running: from the turns so far
        best_so_far, progress = None, []
        for r in rounds:
            for (rr, _), (t, _) in by_cell.items():
                if rr == r and t.get("status") == "valid" and t.get("score") is not None:
                    best_so_far = t["score"] if best_so_far is None else max(best_so_far, t["score"])
            progress.append(best_so_far)
    out.append(progress_svg(progress))
    edges = []
    for e in entries.values():
        for parent in e.get("parents") or []:
            p = entries.get(parent)
            if p and p.get("member") != e.get("member"):
                gain = (e["score"] - p["score"]) if e.get("score") is not None and p.get("score") is not None else None
                edges.append(f"<li>{esc(e['member'])} (round {esc(e.get('round'))}, {esc(e['status'])}) built on "
                             f"{esc(p['member'])}'s <code>{esc(parent)}</code> (round {esc(p.get('round'))})"
                             + ("" if gain is None else f": {gain:+.6g}") + "</li>")
    out.append("<h2>Who built on whom</h2>")
    out.append('<ul class="plain">' + ("".join(edges) or "<li>No member built on a teammate's entry.</li>") + "</ul>")
    out.append("<h2>Entries in the knowledge base</h2>")
    out.append('<div class="wrap"><table class="list"><tr><th>id</th><th>member</th><th>round</th><th>kind</th><th>verdict</th>'
               "<th>parents</th><th>used by</th><th>repeats</th><th>summary / judge</th></tr>")
    for e in entries.values():
        verdict = e["status"] + (f" {fmt_num(e['score'], SCORE_DIGITS)}" if e.get("score") is not None else "")
        text = e.get("summary", "") + (f" — {e['detail']}" if e.get("detail") and e["status"] != "valid" else "")
        ids = lambda values: "<br>".join(esc(v) for v in values)  # noqa: E731
        out.append(f'<tr><td class="id">{esc(e["id"])}</td><td class="nw">{esc(e.get("member"))}</td><td class="nw">{esc(e.get("round"))}</td>'
                   f'<td class="nw">{esc(e.get("kind"))}</td><td>{esc(verdict)}</td><td class="id">{ids(e.get("parents") or [])}</td>'
                   f'<td class="id">{ids(e["adopted_by"])}</td><td class="id">{esc(e["duplicate_of"] or "")}</td>'
                   f"<td>{esc(text[:300])}</td></tr>")
    out.append("</table></div>")
    out.append('<p class="sub">Written by herdr_py.coopview from run.jsonl, kb/ and the members\' logs; '
               "scores are the judge's, never a member's own claim.</p></main></body></html>")
    return "\n".join(out)


def save(run_dir, out_path=None):
    """Write the page (atomically) and return its path."""
    out_path = out_path or os.path.join(run_dir, "view.html")
    page = build(run_dir)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(page)
    os.replace(tmp, out_path)
    return out_path


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m herdr_py.coopview", description=__doc__.split("\n\n")[0])
    ap.add_argument("run_dir", metavar="RUN_DIR", help="a folder made by python3 -m herdr_py.coop --out")
    ap.add_argument("--html", metavar="FILE", help="where to write the page (default RUN_DIR/view.html)")
    a = ap.parse_args(argv)
    if not os.path.isfile(os.path.join(a.run_dir, "run.jsonl")):
        print(f"herdr-py coopview: no run in {a.run_dir} (run.jsonl is missing)", file=sys.stderr)
        return 2
    print(save(a.run_dir, a.html))
    return 0


if __name__ == "__main__":
    sys.exit(main())
