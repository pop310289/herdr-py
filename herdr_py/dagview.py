"""One HTML page for a DAG run (dag.py): the plan drawn as a graph, every step's state, and every attempt with its
verdict, built from events.jsonl and plan.json only, so it can be opened while the run goes on.

    python3 -m herdr_py.dagview runs/p1        # writes runs/p1/view.html

Steps are boxes in rows by level, top to bottom (a step's level is one more than the deepest step it needs), so a
phone scrolls down through the plan; an arrow runs from each step to every step that needs it, solid once the upstream
step has passed. A graph a little too wide for the screen is scaled down (to 75% at most), a wider one scrolls
sideways. No JavaScript: the page is the same in a phone's preview as in a browser; data is escaped, never written
into the page as markup.
"""
import html
import json
import os
import sys
import time

STATE_WORD = {"waiting": "waiting", "running": "running", "judging": "judging", "passed": "passed", "failed": "failed",
              "blocked": "blocked"}
BOX_W, BOX_H, COL_W, ROW_H, PAD = 116, 50, 128, 86, 8


def load(out):
    with open(os.path.join(out, "plan.json"), encoding="utf-8") as handle:
        plan = json.load(handle)
    events = []
    with open(os.path.join(out, "events.jsonl"), encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                try:
                    events.append(json.loads(line))
                except ValueError:  # a line being written while we read: the next page will have it
                    continue
    summary = None
    if os.path.isfile(os.path.join(out, "summary.json")):
        with open(os.path.join(out, "summary.json"), encoding="utf-8") as handle:
            summary = json.load(handle)
    return plan, events, summary


def states(plan, events):
    """{id: {"state", "attempts", "score", "sha", "why"}} from the events, the way dag.py moves a step."""
    out = {n: {"state": "waiting", "attempts": 0, "score": None, "sha": None, "why": None} for n in plan["order"]}
    for e in events:
        st = out.get(e.get("node"))
        if st is None:
            continue
        kind = e["kind"]
        if kind == "node.dispatch":
            st.update(state="running", attempts=max(st["attempts"], e["attempt"]))
        elif kind == "node.return":
            st["state"] = "judging"
        elif kind == "node.retry":
            st["state"] = "running"
        elif kind == "node.pass":
            st.update(state="passed", score=e.get("score"), sha=e.get("sha"), why=None)
        elif kind == "node.fail":
            st.update(state="waiting" if e.get("infra") and any(x["kind"] == "run.resume" and x["seq"] > e["seq"] for x in events)
                      else "failed", why=e.get("why"))
        elif kind == "node.blocked":
            st.update(state="blocked", why=e.get("why"))
    return out


def levels(plan):
    out = {}

    def level(n):
        if n not in out:
            out[n] = 1 + max([level(m) for m in plan["nodes"][n]["needs"]] or [-1])
        return out[n]

    for n in plan["order"]:
        level(n)
    return out


def layout(plan):
    """{id: (x, y)} of each box's top-left corner, and the drawing's (width, height): one row per level."""
    lv = levels(plan)
    rows = {}
    for n in plan["order"]:
        rows.setdefault(lv[n], []).append(n)
    widest = max(len(r) for r in rows.values())
    where = {}
    for level, names in rows.items():
        offset = (widest - len(names)) * COL_W / 2  # centre short rows
        for i, n in enumerate(names):
            where[n] = (PAD + offset + i * COL_W, PAD + level * ROW_H)
    return where, (PAD * 2 + widest * COL_W - (COL_W - BOX_W), PAD * 2 + (max(rows) + 1) * ROW_H - (ROW_H - BOX_H))


def plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


def esc(text):
    return html.escape(str(text), quote=True)


def graph_svg(plan, st):
    where, (width, height) = layout(plan)
    parts = [f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
             f'style="width:max(100%,{round(width * 0.75)}px);max-width:{width}px;height:auto" role="img" '
             f'aria-label="{esc("the plan as a graph: one box per step, arrows from each step to the steps that need it")}">',
             '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
             'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" class="head"/></marker></defs>']
    for n in plan["order"]:
        for m in plan["nodes"][n]["needs"]:
            (x1, y1), (x2, y2) = where[m], where[n]
            sx, sy, ex, ey = x1 + BOX_W / 2, y1 + BOX_H, x2 + BOX_W / 2, y2 - 2
            mid = (sy + ey) / 2
            cls = "edge done" if st[m]["state"] == "passed" else "edge"
            parts.append(f'<path class="{cls}" d="M{sx},{sy} C{sx},{mid} {ex},{mid} {ex},{ey}" marker-end="url(#arrow)"/>')
    for n in plan["order"]:
        x, y = where[n]
        s = st[n]
        detail = s["state"] + (f" · try {s['attempts']}" if s["attempts"] > 1 else "") + (
            f" · {s['score']:.6g}" if isinstance(s["score"], (int, float)) else "")
        tip = f"{n}: {s['state']}" + (f" ({s['why']})" if s["why"] else "") + (f", commit {s['sha'][:12]}" if s["sha"] else "")
        parts.append(f'<g class="box {esc(s["state"])}"><title>{esc(tip)}</title>'
                     f'<rect x="{x}" y="{y}" width="{BOX_W}" height="{BOX_H}" rx="8"/>'
                     f'<text x="{x + 9}" y="{y + 20}" class="id">{esc(n)}</text>'
                     f'<text x="{x + 9}" y="{y + 38}" class="sub">{esc(plan["nodes"][n]["member"]["name"])} · {esc(detail)}</text></g>')
    parts.append("</svg>")
    return "\n".join(parts)


def attempts_list(plan, events):
    """Every attempt as three short lines (a phone has no room for an eight-column table)."""
    rows, verdicts, commits, returns = [], {}, {}, {}
    for e in events:
        key = (e.get("node"), e.get("attempt"))
        if e["kind"] == "node.verdict":
            verdicts[key] = e
        elif e["kind"] == "node.commit":
            commits[key] = e
        elif e["kind"] == "node.return":
            returns[key] = e
    fails = {(e.get("node"), e.get("attempt")): e for e in events if e["kind"] == "node.fail"}
    for e in events:
        if e["kind"] != "node.dispatch":
            continue
        key = (e["node"], e["attempt"])
        r, v, c, f = returns.get(key, {}), verdicts.get(key, {}), commits.get(key, {}), fails.get(key, {})
        verdict = v.get("status") or ("infra_error" if f.get("infra") else ("" if r else "running"))
        word = {"valid": "passed", "invalid": "failed", "infra_error": "setup broke", "running": "running"}.get(verdict, "")
        score = v.get("score")
        first = (f'<b>{esc(e["node"])}</b> · try {esc(e["attempt"])} · {esc(e["member"])} — '
                 f'<span class="v {esc(verdict)}">{esc(word)}</span>'
                 + (f" ({esc(f'{score:.6g}')})" if isinstance(score, (int, float)) else ""))
        second = " · ".join(x for x in (
            ("turn " + r["state"]) if r.get("state") else "", f"{r['seconds']:.0f} s" if r.get("seconds") is not None else "",
            ("commit " + c["sha"][:12] + " · " + plural(len(c.get("changed") or []), "file") + " changed") if c.get("sha") else "") if x)
        said = v.get("detail") or f.get("why") or ""
        rows.append(f'<li><div>{first}</div>' + (f'<div class="muted">{esc(second)}</div>' if second else "")
                    + (f'<div class="said">{esc(said)}</div>' if said else "") + "</li>")
    return f'<ul class="attempts">{"".join(rows) or "<li>nothing dispatched yet</li>"}</ul>'


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
:root {{ --bg:#F3F5F4; --surface:#FFFFFF; --ink:#16201C; --muted:#5A6661; --rule:#D4DBD8; --accent:#1D5C70;
  --pass:#2B7448; --pass-bg:#E2F1E7; --fail:#A93636; --fail-bg:#F6E0DF; --wait:#94600E; --run-bg:#E1EEF2; }}
@media (prefers-color-scheme: dark) {{ :root {{ color-scheme:dark; --bg:#101514; --surface:#171E1C; --ink:#E0E7E4;
  --muted:#96A29D; --rule:#2A3431; --accent:#6DB3C8; --pass:#79C995; --pass-bg:#1A3123; --fail:#E68B88; --fail-bg:#3A1D1C;
  --wait:#E1AE5C; --run-bg:#16303A; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 -apple-system,"PingFang TC","Noto Sans TC",sans-serif;
  padding-inline:16px; padding-block:16px 40px; }}
main {{ max-width:1100px; margin:0 auto; display:flex; flex-direction:column; gap:14px; }}
h1 {{ font-size:1.25rem; margin:0; overflow-wrap:anywhere; }} .muted {{ color:var(--muted); font-size:.85rem; overflow-wrap:anywhere; }}
.chips {{ display:flex; flex-wrap:wrap; gap:6px; }} .chip {{ font-size:.78rem; border:1px solid var(--rule); border-radius:999px; padding:1px 9px; white-space:nowrap; }}
.chip.bad {{ color:var(--fail); background:var(--fail-bg); border-color:transparent; }}
.chip.good {{ color:var(--pass); background:var(--pass-bg); border-color:transparent; }}
.graph {{ overflow-x:auto; background:var(--surface); border:1px solid var(--rule); border-radius:8px; padding:6px; }}
.graph svg {{ display:block; }}
.box rect {{ fill:var(--surface); stroke:var(--muted); stroke-width:1.5; }}
.box.passed rect {{ fill:var(--pass-bg); stroke:var(--pass); }} .box.failed rect {{ fill:var(--fail-bg); stroke:var(--fail); }}
.box.running rect, .box.judging rect {{ fill:var(--run-bg); stroke:var(--accent); stroke-width:2.5; }}
.box.blocked rect {{ stroke-dasharray:5 4; }} .box.blocked text {{ fill:var(--muted); }}
.box text {{ fill:var(--ink); font-size:13px; }} .box text.id {{ font-weight:600; font-size:14px; }} .box text.sub {{ font-size:11px; fill:var(--muted); }}
.edge {{ fill:none; stroke:var(--muted); stroke-width:1.5; stroke-dasharray:4 4; }} .edge.done {{ stroke:var(--pass); stroke-dasharray:none; }}
.head {{ fill:var(--muted); }}
.legend {{ display:flex; flex-wrap:wrap; gap:4px 14px; font-size:.78rem; color:var(--muted); }}
.legend i {{ display:inline-block; width:12px; height:10px; border-radius:3px; border:1.5px solid var(--muted); margin-right:5px; vertical-align:-1px; }}
.legend i.passed {{ background:var(--pass-bg); border-color:var(--pass); }} .legend i.failed {{ background:var(--fail-bg); border-color:var(--fail); }}
.legend i.running {{ background:var(--run-bg); border-color:var(--accent); }} .legend i.blocked {{ border-style:dashed; }}
.tw {{ overflow-x:auto; }} table {{ border-collapse:collapse; width:100%; font-size:.8rem; }}
td, th {{ text-align:left; padding:4px 6px; border-top:1px solid var(--rule); vertical-align:top; }} th {{ color:var(--muted); font-weight:500; white-space:nowrap; }}
td {{ overflow-wrap:anywhere; }} .v.valid {{ color:var(--pass); }} .v.invalid, .v.infra_error {{ color:var(--fail); }}
.attempts {{ list-style:none; margin:0; padding:0; display:flex; flex-direction:column; gap:8px; font-size:.86rem; }}
.attempts li {{ background:var(--surface); border:1px solid var(--rule); border-radius:8px; padding:8px 10px; overflow-wrap:anywhere; }}
.attempts .said {{ font-size:.8rem; }}
</style></head>
<body><main>
<h1>{title}</h1>
<div class="muted">{when}</div>
<div class="chips">{chips}</div>
<div class="graph">{graph}</div>
<div class="legend"><span><i class="passed"></i>passed</span><span><i class="running"></i>running or being judged</span>
<span><i class="failed"></i>failed</span><span><i class="blocked"></i>blocked: a step it needs did not pass</span><span><i></i>waiting</span>
<span>solid arrow: the upstream step passed, its commit is in the downstream step's clone</span></div>
{checks}
<h2 class="muted">every attempt</h2>
{attempts}
</main></body></html>
"""


def render(out):
    plan, events, summary = load(out)
    st = states(plan, events)
    start = next((e for e in events if e["kind"] == "run.start"), {})
    counts = {}
    for s in st.values():
        counts[s["state"]] = counts.get(s["state"], 0) + 1
    stopped = next((e["why"] for e in events if e["kind"] == "run.stop"), None)
    chips = [f'<span class="chip good">{counts.get("passed", 0)} passed</span>']
    for state in ("running", "judging", "waiting", "blocked", "failed"):
        if counts.get(state):
            chips.append(f'<span class="chip{" bad" if state == "failed" else ""}">{counts[state]} {state}</span>')
    if stopped:
        chips.append(f'<span class="chip bad">stopped: {esc(stopped)}</span>')
    checks = ""
    if summary:
        ob = summary.get("out_of_bounds") or {}
        checks = (f'<div class="muted">{summary["seconds"]} s wall, {summary["busy_seconds"]} s of step work '
                  f'(parallelism {summary["parallelism"]}). Invariants: started before their needs passed '
                  f'{summary["released_early"]}, passed steps dispatched again {summary["dispatched_after_pass"]}, '
                  f'workspace crossings {ob.get("count", "?")} (measured for: {esc(", ".join(ob.get("measured") or []) or "no member logs")}).</div>')
    when = "base " + esc((start.get("base") or "no repository")[:12]) + " · started " + esc(
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start["t"])) if start.get("t") else "?") + (
        " · finished" if summary else " · still running (written after every step)")
    return PAGE.format(title=esc(f"DAG {plan['name']}"), when=when, chips="".join(chips), graph=graph_svg(plan, st),
                       checks=checks, attempts=attempts_list(plan, events))


def save(out):
    page = render(out)
    tmp = os.path.join(out, "view.html.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(page)
    os.replace(tmp, os.path.join(out, "view.html"))  # a reader never sees half a page
    return os.path.join(out, "view.html")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python3 -m herdr_py.dagview RUN_FOLDER")
    print(save(sys.argv[1]))
