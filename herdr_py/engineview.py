"""One HTML page for an event-driven team run (engine.py): the loop drawn with this run's numbers, a timeline from the
first event to the end (the planner and every member in lanes, every turn a bar, every result that woke the planner
a dashed line), every todo from added to ended, every planner wake, and the best score over time. Built from
engine.jsonl, run.jsonl, kb/events.jsonl, the members' logs and summary.json only, so it can be opened while the run
goes on (the engine rewrites it after every event). Two more parts show how the team works: the tools each member
used in each turn (web searches, pages fetched, files read: Claude and OpenCode logs), drawn as dots in its bar, and
what the team made, entry by entry, with a line from every entry to each one it built on. An answer whose first line
is "ARTIFACT: <kind>" is grouped by that kind.

    python3 -m herdr_py.engineview runs/e1        # writes runs/e1/view.html

The page is complete without JavaScript (a phone app's preview runs none): it shows the run's final state. With
JavaScript a player is added on top: replay the run from the first event to the end, drag the time, tap a bar for its
details; the loop's boxes light up while that stage works and its counts follow the time. While the run goes on the
page reloads itself every few seconds. Data is escaped, never written into the page as markup.

    python3 -m herdr_py.engineview runs/e1 --serve [--port 8780] [--host 127.0.0.1]

serves the page as it is at each request, with buttons for a person while the run goes on (pause, resume, stop, the
member turns, the time limit: engine.send_control). It prints a link with a token after "#"; a command without that
token is refused. It listens on this machine only unless --host says otherwise. A page opened as a file has no
buttons: it cannot reach the run.
"""
import hmac
import html
import http.server
import json
import os
import re
import secrets
import socketserver
import sys
import time
import urllib.parse


TAG = re.compile(r"^\W*(artifact|kind)\s*:\s*([A-Za-z][\w-]*)", re.I)
WEB, FETCH = ("websearch", "web_search", "search"), ("webfetch", "web_fetch", "fetch")


def tool_class(name):
    n = (name or "").lower()
    return "web" if n in WEB else ("fetch" if n in FETCH else "read")


def esc(text):
    return html.escape(str(text), quote=True)


def times(n, word="time"):
    return f"{n} {word}" + ("" if n == 1 else "s")


def secs(v, span):
    return f"{v:.1f} s" if span < 10 else f"{v:.0f} s"


def read_jsonl(path):
    rows = []
    if not os.path.isfile(path):
        return rows
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except ValueError:  # a line still being written
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def load(out):
    engine = read_jsonl(os.path.join(out, "engine.jsonl"))
    turns = read_jsonl(os.path.join(out, "run.jsonl"))
    todos = {}
    for e in read_jsonl(os.path.join(out, "kb", "events.jsonl")):
        if e.get("type") != "todo":
            continue
        tid, op = e.get("id"), e.get("op")
        if op == "add":
            todos[tid] = {"id": tid, "text": e.get("text") or "", "for": e.get("for"), "parents": e.get("parents") or [],
                          "after": e.get("after") or [], "review": bool(e.get("review")),
                          "wake": e.get("wake"), "added": e.get("t"), "state": "open"}
        elif tid in todos:
            todo = todos[tid]
            if op == "take":
                todo.update(state="taken", member=e.get("member"), taken=e.get("t"))
            elif op == "end":
                todo.update(state="done" if e.get("outcome") == "done" else "failed", ended=e.get("t"), entry=e.get("entry"),
                            status=e.get("status"), score=e.get("score"), detail=e.get("detail"))
            elif op == "drop":
                todo.update(state="dropped", ended=e.get("t"))
    entries, verdicts = {}, {}
    for e in read_jsonl(os.path.join(out, "kb", "events.jsonl")):
        if e.get("type") == "propose" and e.get("id") not in entries:
            entries[e["id"]] = e
        elif e.get("type") == "verdict":
            verdicts[e.get("id")] = e
    made = []
    for eid, p in entries.items():
        v = verdicts.get(eid) or {}
        kind, name = None, None
        if p.get("artifact"):
            try:
                with open(os.path.join(out, "kb", p["artifact"]), encoding="utf-8", errors="replace") as handle:
                    head = [handle.readline() for _ in range(30)]
                m = TAG.match(head[0])
                kind = m.group(2).lower() if m else None
                if kind == "skill":  # a skill names itself in its frontmatter (name: ...)
                    name = next((re.sub(r"^\s*name\s*:\s*", "", h).strip().strip('"') for h in head if re.match(r"^\s*name\s*:", h)), None)
            except OSError:
                pass
        made.append({"id": eid, "t": p.get("t"), "member": p.get("member"), "summary": p.get("summary") or "", "name": name,
                     "parents": p.get("parents") or [], "kind": kind or p.get("kind"), "status": v.get("status", "unjudged"),
                     "score": v.get("score"), "detail": v.get("detail") or ""})
    tools = []
    for backend in ("claude", "opencode"):
        for row in read_jsonl(os.path.join(out, "members", backend, "events.jsonl")):
            ev = row.get("event")
            if not isinstance(ev, dict):
                continue
            calls = []
            if ev.get("type") == "assistant":  # Claude's stream-json
                calls = [(b.get("name"), b.get("input") or {}) for b in (ev.get("message") or {}).get("content") or []
                         if isinstance(b, dict) and b.get("type") == "tool_use"]
            elif ev.get("kind") == "tool":  # herdr-py's OpenCode member log
                calls = [(ev.get("tool"), ev.get("input") or {})]
            for name, inp in calls:
                what = inp.get("query") or inp.get("url") or inp.get("file_path") or inp.get("filePath") or inp.get("pattern") or ""
                tools.append({"t": row.get("t"), "agent": row.get("agent"), "tool": str(name), "what": str(what)[:160]})
    summary = None
    path = os.path.join(out, "summary.json")
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as handle:
                summary = json.load(handle)
        except ValueError:
            summary = None
    return engine, turns, list(todos.values()), summary, made, tools


def text_width(text, size):
    """About how wide text is drawn at this font size: a CJK character as wide as the size, any other 0.55 of it (in
    Chrome with the system font the loop's labels measured 0.43 to 0.52 of the size per character)."""
    return sum(size if ord(c) > 0x2E80 else size * 0.55 for c in str(text))


def fit(text, px, size):
    """The text, cut with an ellipsis when it would be drawn wider than px (an SVG label does not wrap)."""
    text = str(text)
    if text_width(text, size) <= px:
        return text
    while text and text_width(text + "…", size) > px:
        text = text[:-1]
    return text.rstrip(" ,·") + "…"


def clock(seconds):
    s = int(round(max(0, seconds)))
    return f"{s // 60}m {s % 60:02d}s" if s >= 60 else f"{s}s"


def median(values):
    v = sorted(values)
    if not v:
        return None
    m = len(v) // 2
    return v[m] if len(v) % 2 else (v[m - 1] + v[m]) / 2


LED = {"pass": "ok", "fail": "bad", "bad": "bad"}  # a turn's outcome -> the colour of its member's status light


def agents_panel(wakes, turns, todos, summary, members, planner, finished, made=(), reads=()):
    """One row per agent: the planner, every member, the judge; what each did, from the records only. A member with a
    todo taken and not ended works now (only while the run goes on); otherwise its light shows its last turn."""
    rows = []
    wake_n = len({w.get("wake") for w in wakes})
    back = sum(1 for w in wakes if w.get("problems") and w.get("state") == "idle")
    tokens = sum(w["tokens"] for w in wakes if isinstance(w.get("tokens"), int))
    rows.append(("planner", planner, "planner", "idle", f"{times(wake_n, 'wake')} · {times(len(wakes), 'turn')}"
                 + (f" · {back} sent back" if back else ""), f"{tokens:,} tokens" if tokens else ""))
    idle, span = (summary or {}).get("idle_seconds") or {}, (summary or {}).get("seconds")
    working = {t.get("member"): t for t in todos if t["state"] == "taken"} if not finished else {}
    for name in members:
        mine = [t for t in turns if t.get("member") == name]
        last = mine[-1] if mine else None
        light = "run" if name in working else (LED.get(outcome(last), "idle") if last else "idle")
        valid = sum(1 for t in mine if t.get("status") == "valid")
        used = sum(t["tokens"] for t in mine if isinstance(t.get("tokens"), int))
        line = f"{times(len(mine), 'turn')} · {valid} valid" + (f" · {len(mine) - valid} not" if len(mine) > valid else "")
        free = (f"free {idle[name]:.0f} s" + (f" ({idle[name] / span:.0%})" if span else "")) if name in idle else ""
        now = (f"works on {working[name]['id']}" if name in working else
               f"last: {last.get('todo')} {last.get('status') or last.get('kind') or last.get('state')}" if last else "no turn yet")
        skills = {e["id"] for e in made if (e["kind"] or "").lower() == "skill"}
        wrote = [e["id"] for e in made if e["id"] in skills and e["member"] == name]
        opened = sorted({eid for agent, eid, _ in reads if agent == name and eid in skills})
        know = " · ".join(x for x in (f"wrote skill {', '.join(wrote)}" if wrote else "",
                                       f"read skill {', '.join(opened)}" if opened else "") if x)
        rows.append(("member", name, "member", light, line,
                     " · ".join(x for x in (f"{used:,} tokens" if used else "", free, now, know) if x)))
    judged = [t for t in turns if t.get("status") in ("valid", "invalid")]
    scores = [t["score"] for t in judged if t.get("status") == "valid" and isinstance(t.get("score"), (int, float))]
    rows.append(("judge", "judge", "a program", "ok" if scores else "idle", f"{len(scores)} valid of {len(judged)} judged",
                 f"best {max(scores):.6g}" if scores else "no valid answer yet"))
    items = "".join(f'<li data-role="{kind}" data-agent="{esc(n)}" data-led="{light}"><i class="led {light}"></i><b>{esc(n)}</b> '
                    f'<span class="role">{esc(role)}</span><div class="sub">{esc(a)}</div>'
                    + (f'<div class="sub">{esc(b)}</div>' if b else "") + "</li>" for kind, n, role, light, a, b in rows)
    return f'<ul class="agents" id="agents">{items}</ul>'


def health_panel(start, wakes, turns, summary):
    """The run's numbers that say whether the team worked well, each from the records (nothing estimated)."""
    added = {}
    for w in wakes:
        added[w.get("wake")] = added.get(w.get("wake"), 0) + len(w.get("added") or [])
    items = [("member turns", f"{len(turns)} / {start.get('turns', '?')}", ""),
             ("planner wakes", str(len(added)), f"{sum(1 for n in added.values() if n == 0)} added no todo"),
             ("replies sent back", f"{sum(1 for w in wakes if w.get('problems'))} of {len(wakes)}", "")]
    spent = [t["seconds"] for t in turns if isinstance(t.get("seconds"), (int, float))]
    if spent:
        items.append(("member turn", f"{median(spent):.0f} s", f"median; {min(spent):.0f}–{max(spent):.0f} s"))
    if summary:
        items.append(("taken twice", str(summary.get("double_takes")), "must be 0"))
        idle, span = summary.get("idle_seconds") or {}, summary.get("seconds")
        if idle and span:
            shares = [v / span for v in idle.values()]
            items.append(("free, nothing to take", f"{sum(shares) / len(shares):.0%}", f"mean; {min(shares):.0%}–{max(shares):.0%}"))
        if summary.get("tokens") is not None:
            share = summary.get("planner_share")
            items.append(("tokens", f"{summary['tokens']:,}" if isinstance(summary["tokens"], int) else esc(summary["tokens"]),
                          f"planner {share:.0%}" if isinstance(share, (int, float)) else ""))
    cells = "".join(f'<div><dt>{esc(k)}</dt><dd>{esc(v)}' + (f'<small>{esc(n)}</small>' if n else "") + "</dd></div>"
                    for k, v, n in items)
    return f'<dl class="health">{cells}</dl>'


def outcome(turn):
    if turn.get("status") == "valid":
        return "pass"
    if turn.get("status") == "invalid" or turn.get("kind") == "failure":
        return "fail"
    if turn.get("state") in ("error", "aborted", "provider_stall"):
        return "bad"
    return "none"  # no usable answer, a timeout, or still running


def loop_svg(start, wakes, turns, todos, summary, members, planner):
    """The loop, top to bottom, each arrow labelled with what went along it in this run."""
    sent_back = sum(1 for w in wakes if w.get("problems") and w.get("state") == "idle")
    added = sum(len(w.get("added") or []) for w in wakes)
    dropped = sum(len(w.get("dropped") or []) for w in wakes)
    states = {}
    for t in todos:
        states[t["state"]] = states.get(t["state"], 0) + 1
    answers = sum(1 for t in turns if t.get("kind") == "result")
    other = len(turns) - answers
    valid = sum(1 for t in turns if t.get("status") == "valid")
    invalid = sum(1 for t in turns if t.get("status") == "invalid")
    scores = [t["score"] for t in turns if t.get("status") == "valid" and isinstance(t.get("score"), (int, float))]
    best = f"best {max(scores):.6g}" if scores else "no valid answer yet"
    planner_tokens = [w["tokens"] for w in wakes if isinstance(w.get("tokens"), int)]
    reads = (summary or {}).get("board_reads") or {}
    read_note = (f"{sum((reads.get('by_member') or {}).values())} board reads" if reads.get("measured")
                 else "read TEAM_BOARD.md while they work")
    nodes = [("events", "judged answers, ended todos, the start"),
             (f"planner: {planner}", f"{len(wakes)} turns" + (f", {sum(planner_tokens)} tokens" if planner_tokens else "")),
             ("shared todo list", " · ".join(f"{n} {k}" for k, n in sorted(states.items())) or "empty"),
             (f"members: {', '.join(members)}" if text_width(f"members: {', '.join(members)}", 13) <= 232
              else f"members ({len(members)})", read_note),
             ("judge (a program)", f"{valid} valid, {invalid} invalid; {best}")]
    arrows = [f"woke the planner {times(len({w.get('wake') for w in wakes}))}" + (f" ({sent_back} sent back)" if sent_back else ""),
              f"{added} todos added, {dropped} dropped",
              f"{sum(1 for t in todos if t.get('member'))} todos taken",
              f"{answers} answers" + (f", {other} without one" if other else "")]
    box_x, box_w, box_h, gap = 14, 256, 48, 52
    rx = box_x + box_w + 50  # the line back to the top, right of the arrows' labels
    parts = []
    keys = ("events", "planner", "todos", "members", "judge")
    arrow_keys = ("wake", "todo", "take", "answer")
    for i, (title, sub) in enumerate(nodes):
        y = 8 + i * (box_h + gap)
        parts.append(f'<g class="node" id="n-{keys[i]}"><rect x="{box_x}" y="{y}" width="{box_w}" height="{box_h}" rx="10"/>'
                     f'<text x="{box_x + 12}" y="{y + 20}" class="t">{esc(fit(title, box_w - 24, 13))}</text>'
                     f'<text x="{box_x + 12}" y="{y + 38}" class="s" id="s-{keys[i]}">{esc(fit(sub, box_w - 24, 11))}</text></g>')
        if i < len(arrows):
            y1, y2 = y + box_h, y + box_h + gap
            # a small dot runs down the arrow while that step happens in a replay (none at rest: nothing moves then)
            parts.append(f'<g class="flowg" id="f-{arrow_keys[i]}"><line class="flow" x1="{box_x + 40}" y1="{y1 + 2}" x2="{box_x + 40}" y2="{y2 - 8}"/>'
                         f'<polygon class="head" points="{box_x + 34},{y2 - 9} {box_x + 46},{y2 - 9} {box_x + 40},{y2 - 1}"/>'
                         f'<circle class="pulse" cx="{box_x + 40}" cy="{y1 + 3}" r="1.6" style="--len:{gap - 13}px"/>'
                         f'<text x="{box_x + 52}" y="{y1 + gap / 2 + 4}" class="a" id="a-{arrow_keys[i]}">'
                         f'{esc(fit(arrows[i], rx - box_x - 60, 11))}</text></g>')
    top, bottom = 8 + box_h / 2, 8 + 4 * (box_h + gap) + box_h / 2
    parts.append(f'<path class="flow back" d="M {box_x + box_w} {bottom} H {rx} V {top} H {box_x + box_w + 9}"/>'
                 f'<polygon class="head" points="{box_x + box_w + 9},{top - 6} {box_x + box_w + 9},{top + 6} {box_x + box_w + 1},{top}"/>'
                 f'<text class="a" transform="translate({rx + 14},{(top + bottom) / 2}) rotate(-90)" text-anchor="middle">'
                 f'verdicts go to the team knowledge base: the next event</text>')
    height = 8 + 5 * box_h + 4 * gap + 8
    return (f'<svg viewBox="0 0 350 {height}" width="350" height="{height}" role="img" '
            f'aria-label="the loop: events wake the planner, the planner keeps the todo list, members take todos, '
            f'a program judges, verdicts are the next events">{"".join(parts)}</svg>')


def timeline_svg(t0, t1, wakes, turns, members, stop_why, tools=()):
    """A column for the planner and for every member, time running down the page (a phone is tall, not wide): every
    turn is a bar from its start to its end, and every result that woke the planner a dotted line to its column."""
    lanes = ["planner"] + list(members)
    width, axis_w, top = 340, 44, 30
    col_w = (width - axis_w - 4) / len(lanes)
    plot_h = max(240, min(1600, 30 * (len(wakes) + len(turns))))
    height = top + plot_h + 22
    span = max(t1 - t0, 1e-6)
    y = lambda t: top + (t - t0) / span * plot_h  # noqa: E731
    col_x = {name: axis_w + i * col_w for i, name in enumerate(lanes)}
    parts = []
    for name, cx in col_x.items():
        parts.append(f'<text x="{cx + col_w / 2:.1f}" y="{top - 12}" text-anchor="middle" class="ln">{esc(name)}</text>'
                     f'<line class="lane" x1="{cx:.1f}" y1="{top}" x2="{cx:.1f}" y2="{top + plot_h}"/>')
    parts.append(f'<line class="lane" x1="{width - 4}" y1="{top}" x2="{width - 4}" y2="{top + plot_h}"/>')

    def bar(kind, key, s, e, lane, tip, label):
        x1, y1 = col_x[lane] + 4, y(s)
        h = max(y(e) - y1, 3)
        text = f'<text x="{x1 + 4:.1f}" y="{y1 + 12:.1f}">{esc(label)}</text>' if h > 15 and label else ""
        return (f'<g class="bar {kind}" data-s="{s}" data-e="{e}" data-i="{key}"><title>{esc(tip)}</title>'
                f'<rect x="{x1:.1f}" y="{y1:.1f}" width="{col_w - 8:.1f}" height="{h:.1f}" rx="3"/>{text}</g>')
    for wi, w in enumerate(wakes):
        s, e = w.get("start") or (w.get("t", t0) - (w.get("seconds") or 0)), w.get("end") or w.get("t", t0)
        kind = "plan" if w.get("state") == "idle" and not w.get("problems") else "planback"
        parts.append(bar(kind, f"w{wi}", s, e, "planner", f"wake {w.get('wake')} attempt {w.get('attempt')}: {w.get('reason', '')}",
                         f"w{w.get('wake')}"))
    for ti, t in enumerate(turns):
        if t.get("member") not in col_x:
            continue
        s, e = t.get("start") or t.get("t", t0), t.get("end") or (t.get("t", t0) + (t.get("seconds") or 0))
        score = f" score {t['score']:.6g}" if isinstance(t.get("score"), (int, float)) else ""
        parts.append(bar(outcome(t), f"m{ti}", s, e, t["member"],
                         f"{t['member']} turn {t.get('turn')}: todo {t.get('todo')}: {t.get('status') or t.get('state')}{score}",
                         (t.get("todo") or "")[:5]))
        if t.get("end"):  # this result is an event: it woke the planner
            ey = y(e)
            parts.append(f'<line class="event timed" data-t="{e}" x1="{col_x[t["member"]] + 4:.1f}" y1="{ey:.1f}" '
                         f'x2="{col_x["planner"] + col_w - 4:.1f}" y2="{ey:.1f}"/>')
    for c in tools:  # every tool call a dot in its member's bar: searches, pages fetched, files read
        if c.get("agent") not in col_x or not isinstance(c.get("t"), (int, float)):
            continue
        kind = tool_class(c["tool"])
        cx = col_x[c["agent"]] + col_w - {"web": 12, "fetch": 19, "read": 26}[kind]
        parts.append(f'<circle class="tool {kind} timed" data-t="{c["t"]}" cx="{cx:.1f}" cy="{y(c["t"]):.1f}" r="2.6">'
                     f'<title>{esc(c["tool"] + ": " + c["what"])}</title></circle>')
    for frac in (0, 0.25, 0.5, 0.75, 1):
        ty = y(t0 + frac * span)
        parts.append(f'<line class="tick" x1="{axis_w - 4}" y1="{ty:.1f}" x2="{axis_w}" y2="{ty:.1f}"/>'
                     f'<text x="{axis_w - 6}" y="{ty + 4:.1f}" text-anchor="end" class="ax">{secs(frac * span, span)}</text>')
    if stop_why:
        sy = y(t1)
        parts.append(f'<line class="stop" x1="{axis_w}" y1="{sy:.1f}" x2="{width - 4}" y2="{sy:.1f}"/>'
                     f'<text x="{width - 6}" y="{sy + 14:.1f}" text-anchor="end" class="ax">stop</text>')
    parts.append(f'<line id="now" class="now" x1="{axis_w - 4}" y1="{top}" x2="{width - 4}" y2="{top}" style="opacity:0"/>')
    return (f'<svg id="timeline" data-top="{top}" data-h="{plot_h}" viewBox="0 0 {width} {height}" width="{width}" '
            f'height="{height}" role="img" aria-label="timeline from the first event to the end, time running down">{"".join(parts)}</svg>')


def lineage_svg(made):
    """Every entry the team made, a row each in time order, in a column by its kind; a line from each entry it built on."""
    if not made:
        return '<div class="muted">nothing made yet</div>'
    rows = sorted(made, key=lambda e: e.get("t") or 0)[:150]
    order = []
    for e in rows:
        if e["kind"] not in order:
            order.append(e["kind"])
    if len(order) > 6:
        order = order[:5] + ["other"]
    col = lambda e: order.index(e["kind"]) if e["kind"] in order else len(order) - 1  # noqa: E731
    width, left, top, row_h = 340, 6, 34, 26
    col_w = (width - 2 * left) / len(order)
    height = top + len(rows) * row_h + 8
    pos, parts = {}, []
    counts = {k: sum(1 for e in rows if (e["kind"] if e["kind"] in order else "other") == k) for k in order}
    for i, k in enumerate(order):
        parts.append(f'<text x="{left + i * col_w + col_w / 2:.1f}" y="16" text-anchor="middle" class="kh">{esc(k or "?")}</text>'
                     f'<text x="{left + i * col_w + col_w / 2:.1f}" y="28" text-anchor="middle" class="ax">{counts[k]}</text>')
    for i, e in enumerate(rows):
        pos[e["id"]] = (left + col(e) * col_w + col_w / 2, top + i * row_h + row_h / 2)
    for e in rows:
        for p in e["parents"]:
            if p in pos:
                (x1, y1), (x2, y2) = pos[p], pos[e["id"]]
                parts.append(f'<path class="lin timed" data-t="{e.get("t") or 0}" d="M {x1:.1f} {y1 + 9:.1f} C {x1:.1f} {y2 - 12:.1f}, '
                             f'{x2:.1f} {y1 + 12:.1f}, {x2:.1f} {y2 - 9:.1f}"/>')
    for e in rows:
        x, y = pos[e["id"]]
        w = min(col_w - 6, 64)
        state = "pass" if e["status"] == "valid" else ("fail" if e["status"] in ("invalid", "infra_error") else "wait")
        score = f"{e['score']:.3g}" if isinstance(e.get("score"), (int, float)) and e["status"] == "valid" else (
            "x" if state == "fail" else "…")
        tip = f"{e['id']} by {e['member']} ({e['kind']}): {e['status']} {score}: {e['summary']}" + (
            f" | builds on {', '.join(e['parents'])}" if e["parents"] else "") + (f" | {e['detail']}" if e["detail"] else "")
        parts.append(f'<g class="kb {state} timed" data-t="{e.get("t") or 0}"><title>{esc(tip)}</title>'
                     f'<rect x="{x - w / 2:.1f}" y="{y - 9:.1f}" width="{w:.1f}" height="18" rx="5"/>'
                     f'<text x="{x:.1f}" y="{y + 3.5:.1f}" text-anchor="middle">{esc((e["member"] or "?")[:4])} {esc(score)}</text></g>')
    return (f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img" '
            f'aria-label="what the team made, entry by entry, and what built on what">{"".join(parts)}</svg>')


ART = re.compile(r"artifacts/(k[\w-]+?)\.txt")  # board/artifacts/<entry id>.txt (only ids the knowledge base has count)


def artifact_reads(tools):
    """Which agent opened which entry's file (board/artifacts/<id>.txt) and when, from the members' tool logs."""
    out = []
    for c in tools:
        m = ART.search(c.get("what") or "") if tool_class(c.get("tool")) == "read" else None
        if m and c.get("agent"):
            out.append((c["agent"], m.group(1), c.get("t")))
    return out


def division_svg(t0, t1, todos, made, members):
    """Who did which todo: a column for each member (and one for todos nobody took), each todo a box where and when it
    was taken, coloured by how it ended and named by what it made; a solid arrow from a todo it had to wait for
    ("after"), a dashed line from the todo that made an entry it was told to build on."""
    if not todos:
        return '<div class="muted">no todos yet</div>'
    loose = any(not t.get("member") and t.get("for") not in members for t in todos)
    lanes = list(members) + (["anyone"] if loose else [])
    width, top, box_h = 340, 30, 28
    col_w = (width - 8) / len(lanes)
    span = max(t1 - t0, 1e-6)
    plot_h = max(260, min(1400, 34 * len(todos)))
    kind_of = {e["id"]: e["kind"] for e in made}
    by_entry = {t.get("entry"): t for t in todos if t.get("entry")}
    pos, parts, bottom = {}, [], {}
    for name in lanes:
        x = 4 + lanes.index(name) * col_w
        parts.append(f'<text x="{x + col_w / 2:.1f}" y="{top - 12}" text-anchor="middle" class="ln">{esc(fit(name, col_w - 4, 12))}</text>'
                     f'<line class="lane" x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{top + plot_h}"/>')
    for t in sorted(todos, key=lambda t: t.get("taken") or t.get("added") or 0):
        lane = t.get("member") if t.get("member") in members else (t.get("for") if t.get("for") in members else "anyone")
        x = 4 + lanes.index(lane) * col_w + 3
        when = t.get("taken") or t.get("added") or t0
        y = max(top + 4 + (when - t0) / span * (plot_h - box_h - 8), bottom.get(lane, 0) + 4)
        bottom[lane] = y + box_h
        pos[t["id"]] = (x, y)
    height = max(bottom.values()) + 12
    def link(cls, a, b, when):
        """From the bottom of a's box to the top of b's; when b does not start below a's bottom (both were added at
        once), a U under both boxes, from bottom to bottom, so the line is not hidden behind them."""
        (x1, y1), (x2, y2) = pos[a], pos[b]
        x1, x2 = x1 + col_w / 2 - 3, x2 + col_w / 2 - 3
        if y2 >= y1 + box_h + 6:
            d = f"M {x1:.1f} {y1 + box_h:.1f} C {x1:.1f} {y2 - 10:.1f}, {x2:.1f} {y1 + box_h + 10:.1f}, {x2:.1f} {y2 - 2:.1f}"
        else:
            low = max(y1, y2) + box_h + 16
            d = f"M {x1:.1f} {y1 + box_h:.1f} C {x1:.1f} {low:.1f}, {x2:.1f} {low:.1f}, {x2:.1f} {y2 + box_h:.1f}"
        return f'<path class="{cls} timed" data-t="{when}" d="{d}"/>'
    for t in todos:  # lines first, boxes on top
        for a in t.get("after") or []:
            if a in pos and t["id"] in pos:
                parts.append(link("dep", a, t["id"], t.get("added") or 0))
        for p in t.get("parents") or []:
            src = by_entry.get(p)
            if src and src["id"] in pos and t["id"] in pos:
                parts.append(link("uses", src["id"], t["id"], t.get("added") or 0))
    for t in todos:
        x, y = pos[t["id"]]
        state = t["state"]
        made_kind = kind_of.get(t.get("entry"))
        first = {"done": made_kind or "done", "failed": "failed", "dropped": "dropped", "open": "waiting", "taken": "working"}.get(state, state)
        second = (f"{t['score']:.3g}" if isinstance(t.get("score"), (int, float)) and state == "done" else t["id"][1:6])
        if t.get("review"):
            second = "review " + second
        cls = {"done": "pass", "failed": "fail", "dropped": "drop", "open": "wait", "taken": "work"}.get(state, "wait")
        tip = (f"{t['id']} ({state}) for {t.get('for') or 'anyone'}" + (f", taken by {t['member']}" if t.get("member") else "")
               + f": {t['text']}" + (f" | after {', '.join(t['after'])}" if t.get("after") else "")
               + (f" | builds on {', '.join(t['parents'])}" if t.get("parents") else ""))
        w = col_w - 6
        parts.append(f'<g class="td {cls} timed" data-t="{t.get("taken") or t.get("added") or 0}"><title>{esc(tip)}</title>'
                     f'<rect class="under" x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{box_h}" rx="5"/>'
                     f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{box_h}" rx="5"/>'
                     f'<text x="{x + w / 2:.1f}" y="{y + 12:.1f}" text-anchor="middle">{esc(fit(first, w - 4, 9.5))}</text>'
                     f'<text x="{x + w / 2:.1f}" y="{y + 23:.1f}" text-anchor="middle" class="ax">{esc(fit(second, w - 4, 9))}</text></g>')
    return (f'<svg viewBox="0 0 {width} {height:.0f}" width="{width}" height="{height:.0f}" role="img" aria-label="who did which todo, '
            f'when, and which todos waited for or built on others">{"".join(parts)}</svg>')


def knowledge_svg(made, reads, members, t_start):
    """The team knowledge base as a flow: who wrote each entry (left), the entries by kind with skills first (middle),
    and who read the entry's file or built on it (right). Entries from before this run (a seeded knowledge base) have
    a dashed edge."""
    if not made:
        return '<div class="muted">nothing in the knowledge base yet</div>'
    kinds = sorted({e["kind"] or "?" for e in made}, key=lambda k: (k != "skill", k))
    rows = []
    for k in kinds:
        rows.append(("kind", k))
        rows += [("entry", e) for e in sorted((e for e in made if (e["kind"] or "?") == k), key=lambda e: e.get("t") or 0)]
    width, row_h, top = 340, 19, 22
    left_w, right_x = 62, 278
    mid_x, mid_w = 92, 162
    by_id = {e["id"]: e for e in made}
    used = {}
    for e in made:
        for p in e["parents"]:
            if p in by_id and e["member"]:
                used.setdefault(p, {}).setdefault(e["member"], set()).add("built on")
    for agent, eid, _ in reads:
        if eid in by_id:
            used.setdefault(eid, {}).setdefault(agent, set()).add("read")
    agents = list(members) + sorted({e["member"] for e in made if e["member"] and e["member"] not in members})
    height = top + len(rows) * row_h + 10
    a_gap = max(row_h, (height - top - 10) / max(1, len(agents)))
    a_y = {a: top + i * a_gap + a_gap / 2 for i, a in enumerate(agents)}
    parts = [f'<text x="{left_w / 2:.0f}" y="12" text-anchor="middle" class="ax">wrote</text>'
             f'<text x="{mid_x + mid_w / 2:.0f}" y="12" text-anchor="middle" class="ax">entries</text>'
             f'<text x="{right_x + (width - right_x) / 2:.0f}" y="12" text-anchor="middle" class="ax">used by</text>']
    e_y = {}
    for i, (what, item) in enumerate(rows):
        y = top + i * row_h + row_h / 2
        if what == "kind":
            n = sum(1 for e in made if (e["kind"] or "?") == item)
            parts.append(f'<text x="{mid_x}" y="{y + 4:.1f}" class="kh">{esc(item)} · {n}</text>')
        else:
            e_y[item["id"]] = y
    for e in made:  # wrote: writer -> entry; read or built on: entry -> agent
        y = e_y[e["id"]]
        if e["member"] in a_y:
            ay = a_y[e["member"]]
            parts.append(f'<path class="wrote{" old" if (e.get("t") or 0) < t_start else ""} timed" data-t="{e.get("t") or 0}" '
                         f'd="M {left_w:.1f} {ay:.1f} C {left_w + 20:.1f} {ay:.1f}, {mid_x - 20:.1f} {y:.1f}, {mid_x:.1f} {y:.1f}"/>')
        for agent, how in sorted(used.get(e["id"], {}).items()):
            if agent in a_y:
                ay = a_y[agent]
                parts.append(f'<path class="{"built" if "built on" in how else "read"}" d="M {mid_x + mid_w:.1f} {y:.1f} '
                             f'C {mid_x + mid_w + 18:.1f} {y:.1f}, {right_x - 18:.1f} {ay:.1f}, {right_x:.1f} {ay:.1f}"><title>'
                             f'{esc(agent + " " + " and ".join(sorted(how)) + " " + e["id"])}</title></path>')
    for e in made:
        y = e_y[e["id"]]
        old = (e.get("t") or 0) < t_start
        state = "pass" if e["status"] == "valid" else ("fail" if e["status"] in ("invalid", "infra_error") else "wait")
        score = f" {e['score']:.3g}" if isinstance(e.get("score"), (int, float)) and e["status"] == "valid" else ""
        tip = (f"{e['id']} {e['kind']} by {e['member']}{' (from before this run)' if old else ''}: {e['summary']}")
        label = (e.get("name") or e["id"][:7]) if (e["kind"] or "") == "skill" else f"{e['member'] or '?'}"
        parts.append(f'<g class="kb {state}{" old" if old else ""}"><title>{esc(tip)}</title>'
                     f'<rect class="under" x="{mid_x}" y="{y - 7.5:.1f}" width="{mid_w}" height="15" rx="4"/>'
                     f'<rect x="{mid_x}" y="{y - 7.5:.1f}" width="{mid_w}" height="15" rx="4"/>'
                     f'<text x="{mid_x + 6}" y="{y + 3.5:.1f}">{esc(fit(label + score, mid_w - 10, 9.5))}</text></g>')
    for a, y in a_y.items():
        parts.append(f'<text x="{left_w - 4}" y="{y + 4:.1f}" text-anchor="end" class="ln">{esc(fit(a, left_w - 6, 12))}</text>'
                     f'<text x="{right_x + 4}" y="{y + 4:.1f}" class="ln">{esc(fit(a, width - right_x - 6, 12))}</text>')
    return (f'<svg viewBox="0 0 {width} {height:.0f}" width="{width}" height="{height:.0f}" role="img" aria-label="the knowledge '
            f'base: who wrote each entry, and who read it or built on it">{"".join(parts)}</svg>')


def skills_list(made, reads=()):
    skills = [e for e in made if (e["kind"] or "").lower() == "skill" and e["status"] == "valid"]
    if not skills:
        return ""
    users = {s["id"]: [e for e in made if s["id"] in e["parents"]] for s in skills}
    readers = {}
    for agent, eid, _ in reads:
        readers.setdefault(eid, []).append(agent)
    rows = []
    for s in sorted(skills, key=lambda e: e.get("t") or 0):
        used = users[s["id"]]
        read = sorted(set(readers.get(s["id"], [])))
        rows.append(f'<li><div><b>{esc(s["id"])}</b> by {esc(s["member"])}</div><div>{esc(s["summary"])}</div>'
                    f'<div class="muted">' + (f"read by {len(read)}: " + esc(", ".join(read)) if read else "nobody opened its file")
                    + '</div><div class="muted">' + (f"built on by {len(used)}: " + esc(", ".join(f"{u['id']} ({u['member']}, {u['kind']})" for u in used))
                                                     if used else "not built on yet") + '</div></li>')
    return '<h2>Tools the team wrote for itself</h2><ul class="cards">' + "".join(rows) + "</ul>"


def calls_in(turn, tools, t0):
    s = turn.get("start") or turn.get("t", t0)
    e = turn.get("end") or (s + (turn.get("seconds") or 0))
    return [c for c in tools if c.get("agent") == turn.get("member") and isinstance(c.get("t"), (int, float)) and s - 1 <= c["t"] <= e + 1]


def gather_list(turns, tools, t0):
    rows = []
    for t in sorted(turns, key=lambda t: t.get("start") or t.get("t") or 0):
        calls = calls_in(t, tools, t0)
        if not calls:
            continue
        web = [c["what"] for c in calls if tool_class(c["tool"]) == "web"]
        pages = list(dict.fromkeys(re.sub(r"^https?://([^/]+).*$", r"\1", c["what"]) for c in calls if tool_class(c["tool"]) == "fetch"))
        files = list(dict.fromkeys(os.path.basename(c["what"]) or c["what"] for c in calls if tool_class(c["tool"]) == "read"))
        lines = []
        if web:
            lines.append(f"searched {times(len(web))}: " + "; ".join(web[:8]) + (f" (and {len(web) - 8} more)" if len(web) > 8 else ""))
        if pages:
            lines.append(f"fetched {len(pages)} sites: " + ", ".join(pages[:8]))
        if files:
            lines.append(f"read {len(files)} files: " + ", ".join(files[:8]))
        verdict = f" · {t['status']}" + (f" {t['score']:.6g}" if isinstance(t.get("score"), (int, float)) else "") if t.get("status") else ""
        rows.append(f'<li data-added="{t.get("start") or t.get("t") or 0}"><div><b>{esc(t["member"])}</b> · turn {esc(t.get("turn"))} · '
                    f'todo {esc(t.get("todo"))}{esc(verdict)}</div>' + "".join(f'<div class="muted">{esc(x)}</div>' for x in lines) + "</li>")
    return f'<ul class="cards" id="gather">{"".join(rows) or "<li>no tool calls recorded</li>"}</ul>'


def best_svg(t0, t1, turns):
    points, best = [], None
    for t in sorted((t for t in turns if t.get("status") == "valid" and isinstance(t.get("score"), (int, float))),
                    key=lambda t: t.get("end") or t.get("t", 0)):
        if best is None or t["score"] > best:
            best = t["score"]
            points.append((t.get("end") or t.get("t", t0), best))
    if not points:
        return '<div class="muted">no valid answer yet</div>'
    width, height, left, pad = 340, 150, 48, 14
    lo, hi = min(p[1] for p in points), max(p[1] for p in points)
    if hi == lo:
        lo, hi = lo - 1, hi + 1
    span = max(t1 - t0, 1e-6)
    x = lambda t: left + (t - t0) / span * (width - left - pad)  # noqa: E731
    y = lambda v: pad + 14 + (hi - v) / (hi - lo) * (height - 2 * pad - 28)  # noqa: E731  (room above for labels)
    path, last = [], None
    for t, v in points:
        if last is None:
            path.append(f"M {x(t):.1f} {y(v):.1f}")
        else:
            path.append(f"H {x(t):.1f} V {y(v):.1f}")
        last = (t, v)
    path.append(f"H {x(t1):.1f}")
    labels = []
    for i, (t, v) in enumerate(points):  # a label never runs into the next step; the last one sits at the right end
        text = f"{v:.6g}"
        if i == len(points) - 1:
            labels.append(f'<text x="{width - pad}" y="{y(v) - 8:.1f}" text-anchor="end" class="ax">{text}</text>')
        elif x(points[i + 1][0]) - x(t) >= 6.6 * len(text) + 10:
            labels.append(f'<text x="{x(t) + 5:.1f}" y="{y(v) - 8:.1f}" class="ax">{text}</text>')
    dots = "".join(f'<circle cx="{x(t):.1f}" cy="{y(v):.1f}" r="3.5"/>' for t, v in points) + "".join(labels)
    axis = (f'<text x="{left - 4}" y="{y(hi) + 4:.1f}" text-anchor="end" class="ax">{hi:.4g}</text>'
            f'<text x="{left - 4}" y="{y(lo) + 4:.1f}" text-anchor="end" class="ax">{lo:.4g}</text>'
            f'<text x="{left}" y="{height - 2}" class="ax">0 s</text>'
            f'<text x="{width - pad}" y="{height - 2}" text-anchor="end" class="ax">{secs(span, span)}</text>')
    return (f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img" '
            f'aria-label="best verified score over time"><path class="best" d="{" ".join(path)}"/>'
            f'<g class="dots">{dots}</g>{axis}</svg>')


def rel(t, t0):
    return f"+{t - t0:.1f} s" if isinstance(t, (int, float)) else "?"


def todo_list(todos, t0):
    rows = []
    for t in sorted(todos, key=lambda t: t.get("added") or 0):
        state = t["state"]
        head = f'<b>{esc(t["id"])}</b> <span class="st {esc(state)}">{esc(state)}</span>'
        if isinstance(t.get("score"), (int, float)):
            head += f" · score {t['score']:.6g}"
        when = [f"added {rel(t.get('added'), t0)} (wake {esc(t.get('wake'))}, for {esc(t.get('for') or 'anyone')})"]
        if t.get("member"):
            when.append(f"taken {rel(t.get('taken'), t0)} by {esc(t['member'])}")
        if t.get("ended"):
            took = f", {t['ended'] - t['taken']:.1f} s" if isinstance(t.get("taken"), (int, float)) else ""
            when.append(f"ended {rel(t.get('ended'), t0)}{took}")
        builds = f'<div class="muted">builds on {esc(", ".join(t["parents"]))}</div>' if t.get("parents") else ""
        said = f'<div class="said">{esc(t["detail"])}</div>' if t.get("detail") and state == "failed" else ""
        rows.append(f'<li data-added="{t.get("added") or 0}"><div>{head}</div><div>{esc(t["text"])}</div>{builds}'
                    f'<div class="muted">{" · ".join(when)}</div>{said}</li>')
    return f'<ul class="cards" id="todos">{"".join(rows) or "<li>no todos yet</li>"}</ul>'


def control_list(controls, t0):
    if not controls:
        return ""
    rows = []
    for c in controls:
        what = (c.get("command") or "?") + (f" {c['value']}" if c.get("value") is not None else "")
        rows.append(f'<li data-s="{c.get("t") or 0}"><div><b>{esc(what)}</b> · {rel(c.get("t"), t0)} · by {esc(c.get("by") or "?")}</div>'
                    + (f'<div class="said">refused: {esc(c["refused"])}</div>' if c.get("refused") else "")
                    + (f'<div class="muted">the member turns are now {esc(c["applied"])}</div>' if c.get("applied") else "") + "</li>")
    return '<section class="panel"><h2>Commands from people</h2><ul class="cards" id="controls-sent">' + "".join(rows) + "</ul></section>"


def wake_list(wakes, t0):
    rows = []
    for w in wakes:
        head = f'<b>wake {esc(w.get("wake"))}</b> · attempt {esc(w.get("attempt"))} · {rel(w.get("start"), t0)} · {esc(w.get("seconds"))} s'
        what = []
        if w.get("added"):
            what.append("added " + ", ".join(w["added"]))
        if w.get("dropped"):
            what.append("dropped " + ", ".join(w["dropped"]))
        if w.get("done"):
            what.append("said the task is done")
        if w.get("problems"):
            what.append("sent back: " + "; ".join(w["problems"]))
        rows.append(f'<li data-s="{w.get("start") or w.get("t") or 0}"><div>{head}</div><div class="muted">woken because: {esc(w.get("reason", ""))}</div>'
                    + (f'<div>{esc(" · ".join(what))}</div>' if what else "")
                    + (f'<div class="said">{esc(w["why"])}</div>' if w.get("why") else "") + "</li>")
    return f'<ul class="cards" id="wakes">{"".join(rows) or "<li>the planner has not been woken yet</li>"}</ul>'


SCRIPT = '(function () {\n  "use strict";\n  var el = document.getElementById("run-data"), svg = document.getElementById("timeline");\n  if (!el || !svg) { return; }\n  var D;\n  try { D = JSON.parse(el.textContent); } catch (err) { return; }\n  var span = Math.max(D.t1 - D.t0, 1e-6), TOP = +svg.getAttribute("data-top"), PH = +svg.getAttribute("data-h");\n  function Y(t) { return TOP + (t - D.t0) / span * PH; }\n  function $(id) { return document.getElementById(id); }\n  function fmt(v) { return String(Math.round(v * 1e6) / 1e6); }\n  function all(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }\n  var texts = ["s-events", "s-planner", "s-todos", "s-members", "s-judge", "a-wake", "a-todo", "a-take", "a-answer"];\n  var original = {};\n  texts.forEach(function (id) { if ($(id)) { original[id] = $(id).textContent; } });\n  var bars = all(".bar", svg), lines = all(".timed"), now = $("now");\n  var todoCards = all("#todos li[data-added], #gather li[data-added]"), wakeCards = all("#wakes li[data-s]");\n  var agentRows = all("#agents li[data-role]"), flows = ["f-wake", "f-todo", "f-take", "f-answer"];\n  bars.forEach(function (g) {\n    var r = g.querySelector("rect");\n    g._y = +r.getAttribute("y"); g._h = +r.getAttribute("height");\n    g._s = +g.getAttribute("data-s"); g._e = +g.getAttribute("data-e");\n    g.style.cursor = "pointer";\n    g.addEventListener("click", function () { show(g.getAttribute("data-i")); });\n  });\n  var slot = $("player");\n  slot.innerHTML = \'<button type="button" id="play">▶ Replay</button>\' +\n    \'<input type="range" id="scrub" min="0" max="1000" step="1" value="1000" aria-label="time in the run">\' +\n    \'<span id="clock" class="muted"></span>\';\n  var play = $("play"), scrub = $("scrub"), clock = $("clock"), detail = $("detail");\n  var playing = false, t = D.t1, last = 0, rate = Math.max(1, span / 24);\n  function setText(id, text) { var n = $(id); if (n) { n.textContent = text.length > 40 ? text.slice(0, 39) + "…" : text; } }\n  function setOn(id, on) { var n = $(id); if (n) { n.classList.toggle("on", !!on); } }\n  function count(list, test) { var n = 0; list.forEach(function (x) { if (test(x)) { n += 1; } }); return n; }\n  function at(time) {\n    t = time;\n    bars.forEach(function (g) {\n      var r = g.querySelector("rect");\n      if (time < g._s) { g.style.opacity = "0"; return; }\n      g.style.opacity = "1";\n      r.setAttribute("height", (time >= g._e ? g._h : Math.max(3, Y(time) - g._y)).toFixed(1));\n      g.classList.toggle("live", time < g._e);\n    });\n    lines.forEach(function (l) { l.style.opacity = +l.getAttribute("data-t") <= time ? "1" : "0"; });\n    now.setAttribute("y1", Y(time).toFixed(1)); now.setAttribute("y2", Y(time).toFixed(1));\n    now.style.opacity = "1";\n    var flash = Math.max(0.6, span / 60);\n    var planning = D.wakes.some(function (w) { return w.s <= time && time < w.e; });\n    var working = D.turns.filter(function (m) { return m.s <= time && time < m.e; });\n    var ended = D.turns.filter(function (m) { return m.e <= time; });\n    var judged = ended.some(function (m) { return time - m.e < flash; });\n    setOn("n-planner", planning); setOn("n-members", working.length > 0); setOn("n-judge", judged);\n    setOn("n-events", judged || D.wakes.some(function (w) { return w.s <= time && time - w.s < flash; }));\n    var woke = {};\n    D.wakes.forEach(function (w) { if (w.s <= time) { woke[w.wake] = 1; } });\n    var back = count(D.wakes, function (w) { return w.e <= time && w.kind === "planback"; });\n    var added = count(D.todos, function (x) { return x.added <= time; });\n    var dropped = count(D.todos, function (x) { return x.state === "dropped" && x.ended <= time; });\n    var taken = count(D.todos, function (x) { return x.taken && x.taken <= time; });\n    var done = count(D.todos, function (x) { return x.state === "done" && x.ended <= time; });\n    var failed = count(D.todos, function (x) { return x.state === "failed" && x.ended <= time; });\n    var answers = count(ended, function (m) { return m.kind === "result"; });\n    var valid = ended.filter(function (m) { return m.status === "valid"; });\n    var invalid = count(ended, function (m) { return m.status === "invalid"; });\n    var best = null;\n    valid.forEach(function (m) { if (best === null || m.score > best) { best = m.score; } });\n    setOn("f-wake", planning); setOn("f-todo", D.wakes.some(function (w) { return w.e <= time && time - w.e < flash; }));\n    setOn("f-take", D.turns.some(function (m) { return m.s <= time && time - m.s < flash; })); setOn("f-answer", judged);\n    agentRows.forEach(function (li) {\n      var role = li.getAttribute("data-role"), name = li.getAttribute("data-agent"), k = "idle";\n      if (role === "planner") { k = planning ? "run" : "idle"; }\n      else if (role === "judge") { k = valid.length ? "ok" : "idle"; }\n      else if (working.some(function (m) { return m.member === name; })) { k = "run"; }\n      else {\n        var mine = ended.filter(function (m) { return m.member === name; }), o = mine[mine.length - 1];\n        if (o) { k = o.status === "valid" ? "ok" : (o.status === "invalid" || o.kind === "failure" || ["error", "aborted", "provider_stall"].indexOf(o.state) >= 0 ? "bad" : "idle"); }\n      }\n      li.querySelector(".led").className = "led " + k;\n    });\n    var nw = Object.keys(woke).length;\n    setText("a-wake", "woke the planner " + nw + (nw === 1 ? " time" : " times") + (back ? " (" + back + " sent back)" : ""));\n    setText("a-todo", added + " todos added, " + dropped + " dropped");\n    setText("a-take", taken + " todos taken");\n    setText("a-answer", answers + " answers" + (ended.length > answers ? ", " + (ended.length - answers) + " without one" : ""));\n    setText("s-todos", (added - taken - dropped) + " open · " + (taken - done - failed) + " taken · " + done + " done · " + failed + " failed");\n    setText("s-members", working.length ? "working now: " + working.map(function (m) { return m.member; }).join(", ") : "waiting for a todo");\n    setText("s-planner", planning ? "planning now" : original["s-planner"]);\n    setText("s-judge", valid.length + " valid, " + invalid + " invalid; " + (best === null ? "no valid answer yet" : "best " + fmt(best)));\n    todoCards.forEach(function (li) { li.classList.toggle("future", +li.getAttribute("data-added") > time); });\n    wakeCards.forEach(function (li) { li.classList.toggle("future", +li.getAttribute("data-s") > time); });\n    clock.textContent = (time - D.t0).toFixed(1) + " s of " + span.toFixed(0) + " s";\n    scrub.value = String(Math.round((time - D.t0) / span * 1000));\n  }\n  function rest() {\n    bars.forEach(function (g) { g.style.opacity = "1"; g.classList.remove("live"); g.querySelector("rect").setAttribute("height", g._h.toFixed(1)); });\n    lines.forEach(function (l) { l.style.opacity = "1"; });\n    now.style.opacity = "0";\n    ["n-events", "n-planner", "n-todos", "n-members", "n-judge"].concat(flows).forEach(function (id) { setOn(id, false); });\n    agentRows.forEach(function (li) { li.querySelector(".led").className = "led " + li.getAttribute("data-led"); });\n    Object.keys(original).forEach(function (id) { setText(id, original[id]); });\n    todoCards.concat(wakeCards).forEach(function (li) { li.classList.remove("future"); });\n    clock.textContent = span.toFixed(0) + " s in all" + (D.finished ? "" : " so far");\n    scrub.value = "1000";\n    t = D.t1;\n  }\n  function stop() { playing = false; play.textContent = "▶ Replay"; }\n  function frame(ts) {\n    if (!playing) { return; }\n    var next = t + (last ? (ts - last) / 1000 : 0) * rate;\n    last = ts;\n    if (next >= D.t1) { stop(); rest(); return; }\n    at(next);\n    window.requestAnimationFrame(frame);\n  }\n  play.addEventListener("click", function () {\n    if (playing) { stop(); return; }\n    playing = true; last = 0; play.textContent = "❚❚ Pause";\n    if (t >= D.t1) { at(D.t0); }\n    window.requestAnimationFrame(frame);\n  });\n  scrub.addEventListener("input", function () {\n    stop();\n    var v = +scrub.value;\n    if (v >= 1000) { rest(); } else { at(D.t0 + v / 1000 * span); }\n  });\n  function show(key) {\n    var item = key.charAt(0) === "w" ? D.wakes[+key.slice(1)] : D.turns[+key.slice(1)];\n    if (!item) { return; }\n    var out = [];\n    if (key.charAt(0) === "w") {\n      out.push("planner wake " + item.wake + ", attempt " + item.attempt + " · " + (item.e - item.s).toFixed(1) + " s");\n      out.push("woken because: " + item.reason);\n      if (item.added && item.added.length) { out.push("added " + item.added.join(", ")); }\n      if (item.dropped && item.dropped.length) { out.push("dropped " + item.dropped.join(", ")); }\n      if (item.problems && item.problems.length) { out.push("sent back: " + item.problems.join("; ")); }\n      if (item.done) { out.push("said the task is done"); }\n      if (item.why) { out.push(item.why); }\n    } else {\n      var todo = D.todos.filter(function (x) { return x.id === item.todo; })[0] || {};\n      out.push(item.member + ", turn " + item.turn + " · " + (item.e - item.s).toFixed(1) + " s");\n      out.push("todo " + item.todo + ": " + (todo.text || ""));\n      out.push((item.status || item.state || "") + (typeof item.score === "number" ? " · score " + fmt(item.score) : "") + (item.entry ? " · entry " + item.entry : ""));\n      if (item.problem) { out.push(item.problem); }\n      var web = [], got = [], read = [];\n      (item.tools || []).forEach(function (c) { var n = (c[0] || "").toLowerCase(); if (n === "websearch") { web.push(c[1]); } else if (n === "webfetch") { got.push(c[1]); } else { read.push(c[1]); } });\n      if (web.length) { out.push("searched: " + web.join("; ")); }\n      if (got.length) { out.push("fetched: " + got.join(", ")); }\n      if (read.length) { out.push("read: " + read.join(", ")); }\n    }\n    detail.textContent = "";\n    out.forEach(function (line) { var d = document.createElement("div"); d.textContent = line; detail.appendChild(d); });\n    detail.hidden = false;\n  }\n  rest();\n  var ctl = $("controls"), token = (window.location.hash.match(/token=([A-Za-z0-9_-]+)/) || [])[1];\n  if (ctl && D.live && token && !D.finished) {\n    ctl.innerHTML = \'<button type="button" data-c="\' + (D.paused ? "resume" : "pause") + \'">\' + (D.paused ? "\\u25B6 Resume" : "\\u275A\\u275A Pause") + "</button>" +\n      \'<button type="button" class="warn" data-c="stop">\\u25A0 Stop</button>\' +\n      \'<label>turns <input id="c-turns" type="number" min="1" step="1"></label><button type="button" data-c="turns">Set</button>\' +\n      \'<label>time limit (s) <input id="c-time" type="number" min="1" step="1"></label><button type="button" data-c="time_limit">Set</button>\' +\n      \'<span id="c-said" class="muted"></span>\';\n    $("c-turns").value = D.turns || ""; $("c-time").value = D.time_limit || "";\n    ctl.hidden = false;\n    ctl.addEventListener("click", function (ev) {\n      var b = ev.target, c = b && b.getAttribute ? b.getAttribute("data-c") : null;\n      if (!c) { return; }\n      if (c === "stop" && !b.getAttribute("data-sure")) { b.setAttribute("data-sure", "1"); b.textContent = "Stop the run? Tap again"; return; }\n      var value = c === "turns" ? +$("c-turns").value : (c === "time_limit" ? +$("c-time").value : null);\n      $("c-said").textContent = "sending " + c + "\\u2026";\n      fetch("control", { method: "POST", headers: { "Content-Type": "application/json", "Authorization": "Bearer " + token },\n                         body: JSON.stringify({ command: c, value: value }) })\n        .then(function (r) { return r.json().then(function (j) {\n          $("c-said").textContent = r.ok ? "sent " + c + "; the run reads it within a quarter second" : "refused: " + (j.error || r.status); }); })\n        .catch(function () { $("c-said").textContent = "the run\'s server did not answer"; });\n    });\n  }\n  if (!D.finished) {\n    window.setTimeout(function again() {\n      var typing = document.activeElement && ctl && ctl.contains(document.activeElement);\n      if (!playing && +scrub.value >= 1000 && !typing) { window.location.reload(); } else { window.setTimeout(again, 4000); }\n    }, 5000);\n  }\n})();'


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
:root {{ color-scheme:dark; --bg:#090A0F; --panel:#11141A; --inset:#0C0F14; --line:rgba(255,255,255,.07); --hi:rgba(255,255,255,.10);
  --ink:#E5E7EB; --muted:#9CA3AF; --faint:#6B7280; --wire:#3B4352; --grid:rgba(255,255,255,.06); --ice:#93C5FD;
  --ice-bg:rgba(147,197,253,.10); --ok:#10B981; --ok-bg:rgba(16,185,129,.12); --bad:#EF4444; --bad-bg:rgba(239,68,68,.12);
  --amber:#D4A24C; --amber-bg:rgba(212,162,76,.12); }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 -apple-system,"SF Pro Text","PingFang TC","Noto Sans TC",sans-serif;
  padding-inline:16px; padding-block:16px 40px; -webkit-font-smoothing:antialiased; }}
main {{ max-width:1180px; margin:0 auto; display:flex; flex-direction:column; gap:14px; }}
.panel {{ background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:14px; min-width:0;
  box-shadow:inset 0 1px 0 var(--hi), 0 10px 30px rgba(0,0,0,.6); }}
.top {{ display:flex; flex-direction:column; gap:6px; }}
.brand {{ font-size:.72rem; letter-spacing:.08em; text-transform:uppercase; color:var(--faint); }}
h1 {{ font-size:1.3rem; margin:0; overflow-wrap:anywhere; font-weight:650; text-wrap:balance; }}
h2 {{ font-size:.76rem; margin:0 0 10px; letter-spacing:.06em; text-transform:uppercase; color:var(--muted); font-weight:600; }}
.state, .muted {{ color:var(--muted); font-size:.85rem; overflow-wrap:anywhere; }}
.chips {{ display:flex; flex-wrap:wrap; gap:6px; }}
.chip {{ font-size:.76rem; color:var(--muted); background:var(--inset); border:1px solid var(--line); border-radius:999px;
  padding:1px 9px; white-space:nowrap; font-variant-numeric:tabular-nums; }}
.chip.bad {{ color:#F3A6A6; border-color:rgba(239,68,68,.35); }} .chip.good {{ color:#7FD8B6; border-color:rgba(16,185,129,.35); }}
.led {{ display:inline-block; width:6px; height:6px; border-radius:50%; background:var(--faint); margin-right:7px; vertical-align:2px; }}
.led.ok {{ background:var(--ok); }} .led.bad {{ background:var(--bad); }}
.led.run {{ background:var(--ink); animation:breathe 1.6s ease-in-out infinite; }}
@keyframes breathe {{ 50% {{ opacity:.3; }} }}
.grid2 {{ display:grid; gap:14px; }}
@media (min-width:900px) {{ .grid2 {{ grid-template-columns:minmax(0,1fr) minmax(0,1fr); align-items:start; }} }}
.side {{ display:flex; flex-direction:column; gap:14px; min-width:0; }}
.fig {{ overflow-x:auto; }} .fig svg {{ display:block; margin:0 auto; }}
.fig.fit svg {{ width:100%; max-width:420px; height:auto; }} .fig.wide svg {{ width:100%; max-width:470px; height:auto; }}
.node rect {{ fill:var(--inset); stroke:var(--wire); stroke-width:1; }} .node.on rect {{ stroke:var(--ice); fill:var(--ice-bg); }}
.node .t {{ fill:var(--ink); font-size:13px; font-weight:600; }} .node .s, .a, .ax {{ fill:var(--muted); font-size:11px; }}
.ln {{ fill:var(--ink); font-size:12px; }}
.flow {{ fill:none; stroke:var(--wire); stroke-width:1.2; }} .flow.back {{ stroke-dasharray:5 4; }} .head {{ fill:var(--wire); }}
.pulse {{ fill:var(--ice); opacity:0; }} .flowg.on .pulse {{ opacity:1; animation:down 1.1s linear infinite; }}
@keyframes down {{ from {{ transform:translateY(0); }} to {{ transform:translateY(var(--len)); }} }}
.lane, .tick {{ stroke:var(--grid); stroke-width:1; }}
.bar rect {{ stroke-width:1; }} .bar text {{ font-size:10px; fill:var(--ink); }} .bar.live rect {{ stroke-width:1.8; }}
.bar.plan rect {{ fill:var(--ice-bg); stroke:var(--ice); }} .bar.planback rect {{ fill:var(--amber-bg); stroke:var(--amber); }}
.bar.pass rect {{ fill:var(--ok-bg); stroke:var(--ok); }} .bar.fail rect {{ fill:var(--bad-bg); stroke:var(--bad); }}
.bar.bad rect {{ fill:var(--bad); stroke:var(--bad); }} .bar.none rect {{ fill:none; stroke:var(--faint); stroke-dasharray:3 2; }}
.event {{ stroke:var(--faint); stroke-width:1; stroke-dasharray:2 3; }} .stop {{ stroke:var(--bad); stroke-width:1.2; }}
.best {{ fill:none; stroke:var(--ok); stroke-width:1.6; }} .dots circle {{ fill:var(--ok); }}
.now {{ stroke:var(--ice); stroke-width:1; }} .future {{ opacity:.3; }}
.tool.web {{ fill:var(--ice); }} .tool.fetch {{ fill:var(--amber); }} .tool.read {{ fill:var(--faint); }}
.lin {{ fill:none; stroke:var(--wire); stroke-width:1; }} .kh {{ fill:var(--ink); font-size:11px; font-weight:600; }}
.kb rect {{ stroke-width:1; }} .kb text {{ font-size:9.5px; fill:var(--ink); }}
.kb.pass rect {{ fill:var(--ok-bg); stroke:var(--ok); }} .kb.fail rect {{ fill:var(--bad-bg); stroke:var(--bad); }}
.kb.wait rect {{ fill:none; stroke:var(--faint); stroke-dasharray:3 2; }} .kb.old rect {{ stroke-dasharray:4 3; }}
.td rect {{ stroke-width:1; }} rect.under {{ fill:var(--panel) !important; stroke:none !important; }} .td text {{ font-size:9.5px; fill:var(--ink); }} .td text.ax {{ font-size:9px; fill:var(--muted); }}
.td.pass rect {{ fill:var(--ok-bg); stroke:var(--ok); }} .td.fail rect {{ fill:var(--bad-bg); stroke:var(--bad); }}
.td.drop rect {{ fill:none; stroke:var(--faint); stroke-dasharray:3 2; }} .td.drop text {{ fill:var(--faint); }}
.td.wait rect {{ fill:none; stroke:var(--amber); stroke-dasharray:3 2; }} .td.work rect {{ fill:var(--ice-bg); stroke:var(--ice); }}
.dep {{ fill:none; stroke:var(--amber); stroke-width:1.2; }} .uses {{ fill:none; stroke:var(--muted); stroke-width:1; stroke-dasharray:3 3; }}
.wrote {{ fill:none; stroke:var(--ice); stroke-width:1; opacity:.7; }} .wrote.old {{ stroke-dasharray:3 3; opacity:.5; }}
.built {{ fill:none; stroke:var(--ok); stroke-width:1; opacity:.8; }} .read {{ fill:none; stroke:var(--faint); stroke-width:1; stroke-dasharray:2 3; }}
.legend i.dep {{ border:0; border-top:2px solid var(--amber); height:0; border-radius:0; vertical-align:3px; }}
.legend i.uses {{ border:0; border-top:2px dashed var(--muted); height:0; border-radius:0; vertical-align:3px; }}
.legend i.wrote {{ border:0; border-top:2px solid var(--ice); height:0; border-radius:0; vertical-align:3px; }}
.legend i.built {{ border:0; border-top:2px solid var(--ok); height:0; border-radius:0; vertical-align:3px; }}
.legend i.read {{ border:0; border-top:2px dotted var(--faint); height:0; border-radius:0; vertical-align:3px; }}
.legend i.drop, .legend i.old {{ border-style:dashed; }} .legend i.old {{ border-color:var(--ok); }} .legend i.wait {{ border-color:var(--amber); border-style:dashed; }}
.legend {{ display:flex; flex-wrap:wrap; gap:4px 14px; font-size:.76rem; color:var(--muted); margin-top:10px; }}
.legend i {{ display:inline-block; width:12px; height:9px; border-radius:3px; border:1px solid var(--faint); margin-right:5px; vertical-align:-1px; }}
.legend i.plan {{ background:var(--ice-bg); border-color:var(--ice); }} .legend i.planback {{ background:var(--amber-bg); border-color:var(--amber); }}
.legend i.pass {{ background:var(--ok-bg); border-color:var(--ok); }} .legend i.fail {{ background:var(--bad-bg); border-color:var(--bad); }}
.legend i.bad {{ background:var(--bad); border-color:var(--bad); }} .legend i.none {{ border-style:dashed; }}
.dot {{ display:inline-block; width:6px; height:6px; border-radius:50%; margin-right:6px; vertical-align:1px; }}
.dot.web {{ background:var(--ice); }} .dot.fetch {{ background:var(--amber); }} .dot.read {{ background:var(--faint); }}
.agents {{ list-style:none; margin:0; padding:0; }}
.agents li {{ padding:9px 0; border-top:1px solid var(--line); overflow-wrap:anywhere; }} .agents li:first-child {{ border-top:0; padding-top:0; }}
.agents b {{ font-weight:600; }} .agents .role {{ color:var(--faint); font-size:.8rem; }}
.agents .sub {{ color:var(--muted); font-size:.8rem; padding-left:13px; font-variant-numeric:tabular-nums; }}
.health {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; margin:0; }}
.health div {{ background:var(--inset); border:1px solid var(--line); border-radius:8px; padding:8px 10px; min-width:0; }}
.health dt {{ font-size:.72rem; color:var(--faint); }} .health dd {{ margin:2px 0 0; font-size:1.05rem; font-variant-numeric:tabular-nums; }}
.health small {{ display:block; font-size:.72rem; color:var(--muted); overflow-wrap:anywhere; }}
.health div:last-child:nth-child(odd) {{ grid-column:1 / -1; }}
.cards {{ list-style:none; margin:0; padding:0; display:flex; flex-direction:column; gap:8px; font-size:.86rem; }}
.cards li {{ background:var(--inset); border:1px solid var(--line); border-radius:8px; padding:8px 10px; overflow-wrap:anywhere; }}
.st {{ font-size:.72rem; border-radius:999px; padding:0 7px; border:1px solid var(--line); color:var(--muted); }}
.st.done {{ color:#7FD8B6; border-color:rgba(16,185,129,.35); }} .st.failed {{ color:#F3A6A6; border-color:rgba(239,68,68,.35); }}
.st.taken {{ color:var(--ice); border-color:rgba(147,197,253,.35); }}
.said {{ font-size:.8rem; color:var(--muted); }}
.player {{ position:sticky; top:env(safe-area-inset-top, 0px); z-index:2; display:flex; flex-wrap:wrap; align-items:center; gap:10px;
  background:rgba(9,10,15,.94); padding-block:8px; border-bottom:1px solid var(--line); }}
.player:empty {{ display:none; }}
.player button {{ font:inherit; font-size:.85rem; padding:5px 14px; border-radius:999px; border:1px solid var(--line);
  background:var(--panel); color:var(--ink); box-shadow:inset 0 1px 0 var(--hi); }}
.player input {{ flex:1 1 150px; min-width:0; accent-color:var(--ice); }}
.detail {{ background:var(--inset); border:1px solid var(--line); border-radius:8px; padding:8px 10px; font-size:.86rem;
  overflow-wrap:anywhere; margin-top:10px; }}
.controls {{ display:flex; flex-wrap:wrap; align-items:center; gap:8px; padding:10px; background:var(--inset);
  border:1px solid var(--line); border-radius:10px; font-size:.85rem; }}
.controls[hidden] {{ display:none; }}
.controls button {{ font:inherit; padding:5px 12px; border-radius:999px; border:1px solid var(--line); background:var(--panel);
  color:var(--ink); box-shadow:inset 0 1px 0 var(--hi); }} .controls button.warn {{ border-color:rgba(239,68,68,.5); color:#F3A6A6; }}
.controls input {{ width:5.5em; font:inherit; background:var(--bg); color:var(--ink); border:1px solid var(--line); border-radius:6px; padding:3px 6px; }}
@media (prefers-reduced-motion:reduce) {{ .led.run, .flowg.on .pulse {{ animation:none; }} }}
</style></head>
<body><main>
<header class="top">
<div class="brand">herdr-py · event-driven team</div>
<h1>{name}</h1>
<div class="state"><i class="led {state}"></i>{when}</div>
<div class="chips">{chips}</div>
{controls}
</header>
<div class="player" id="player"></div>
<div class="grid2">
<section class="panel"><h2>The loop, with this run's numbers</h2><div class="fig fit">{loop}</div></section>
<div class="side">
<section class="panel"><h2>Agents</h2>{agents}</section>
<section class="panel"><h2>How the run went</h2>{health}</section>
</div>
</div>
<div class="grid2">
<section class="panel"><h2>From the first event to the end</h2>
<div class="fig">{timeline}</div>
<div class="legend"><span><i class="plan"></i>planner turn</span><span><i class="planback"></i>planner reply sent back</span>
<span><i class="pass"></i>valid answer</span><span><i class="fail"></i>invalid answer or failure</span><span><i class="bad"></i>backend broke</span>
<span><i class="none"></i>no answer or still running</span><span>dotted line: a result that woke the planner</span>
<span><b class="dot web"></b>web search</span><span><b class="dot fetch"></b>page fetched</span><span><b class="dot read"></b>file read</span></div>
<div class="detail" id="detail" hidden></div></section>
<div class="side">
<section class="panel"><h2>Best verified score over time</h2><div class="fig">{best}</div></section>
<section class="panel"><h2>What the team made, and what built on what</h2><div class="fig">{lineage}</div>
<div class="legend"><span><i class="pass"></i>verified (member, score)</span><span><i class="fail"></i>did not pass</span>
<span>a line runs from an entry to each one that built on it</span></div></section>
{skills}
</div>
</div>
<div class="grid2">
<section class="panel"><h2>Who did which todo</h2><div class="fig wide">{division}</div>
<div class="legend"><span><i class="pass"></i>done (what it made, score)</span><span><i class="fail"></i>failed</span>
<span><i class="drop"></i>dropped</span><span><i class="wait"></i>not taken yet</span><span><i class="dep"></i>had to wait for (after)</span>
<span><i class="uses"></i>told to build on its result</span></div></section>
<section class="panel"><h2>Knowledge: who wrote and who read each entry</h2><div class="fig wide">{knowledge}</div>
<div class="legend"><span><i class="wrote"></i>wrote (dashed: in an earlier run)</span><span><i class="built"></i>built on it</span>
<span><i class="read"></i>opened its file</span><span><i class="old"></i>an entry from an earlier run</span><span>skills first, by name</span></div></section>
</div>
<section class="panel"><h2>How the team gathered information</h2>
{gather}</section>
<section class="panel"><h2>Every todo</h2>
{todos}</section>
<section class="panel"><h2>Every planner turn</h2>
{wakes}</section>
{sent}
</main>
<script type="application/json" id="run-data">{data}</script>
<script>{script}</script>
</body></html>
"""


def render(out, live=False):
    """The page; live=True (served by serve()) adds the room for a person's buttons, which only its script fills."""
    engine, turns, todos, summary, made, tools = load(out)
    start = next((e for e in engine if e.get("kind") == "start"), {})
    controls = [e for e in engine if e.get("kind") == "control"]
    paused = next((c["command"] == "pause" for c in reversed(controls) if c.get("command") in ("pause", "resume")), False)
    budget = next((c["applied"] for c in reversed(controls) if c.get("applied")), start.get("turns"))
    limit = next((c["value"] for c in reversed(controls) if c.get("command") == "time_limit" and not c.get("refused")),
                 start.get("time_limit"))
    stop = next((e for e in engine if e.get("kind") == "stop"), None)
    wakes = [e for e in engine if e.get("kind") == "wake"]
    members = start.get("members") or sorted({t.get("member") for t in turns if t.get("member")})
    planner = start.get("planner") or "planner"
    t0 = start.get("t") or min([e.get("t", time.time()) for e in engine + turns] or [time.time()])
    ends = [e.get("end") or e.get("t") for e in wakes + turns] + [stop.get("t") if stop else None]
    t1 = max([t for t in ends if isinstance(t, (int, float))] + [t0 + 1])
    chips = []
    scores = [t["score"] for t in turns if t.get("status") == "valid" and isinstance(t.get("score"), (int, float))]
    chips.append(f'<span class="chip good">best {max(scores):.6g}</span>' if scores else '<span class="chip">no valid answer yet</span>')
    chips.append(f'<span class="chip">{len(turns)} of {esc(start.get("turns", "?"))} member turns</span>')
    chips.append(f'<span class="chip">{times(len({w.get("wake") for w in wakes}), "planner wake")}</span>')
    if summary:
        chips.append(f'<span class="chip{" bad" if summary.get("double_takes") else ""}">taken twice {esc(summary.get("double_takes"))}</span>')
        chips.append(f'<span class="chip">traceable {esc(summary.get("traceable"))}</span>')
        if summary.get("tokens") is not None:
            share = summary.get("planner_share")
            chips.append(f'<span class="chip">{summary["tokens"]:,} tokens' if isinstance(summary["tokens"], int)
                         else f'<span class="chip">{esc(summary["tokens"])} tokens'
                         + (f", planner {share:.0%}" if isinstance(share, (int, float)) else "") + "</span>")
    if paused and not stop:
        chips.append('<span class="chip bad">paused by a person</span>')
    if summary and summary.get("paused_seconds"):
        chips.append(f'<span class="chip">paused {summary["paused_seconds"]:g} s</span>')
    if stop:
        chips.append(f'<span class="chip bad">stopped: {esc(stop.get("why"))}</span>')
    when = (("finished" if stop else "still running (written after every event)") + f" · {clock(t1 - t0)}"
            + f" ({t1 - t0:.0f} s) · started " + esc(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t0))))
    name = os.path.basename(os.path.abspath(out))
    title = esc(f"Event-driven team {name}")
    state = "run" if not stop else ("ok" if scores else "bad")
    data = {"t0": t0, "t1": t1, "finished": bool(stop), "planner": planner, "members": members, "live": bool(live),
            "paused": paused, "turns": budget, "time_limit": limit,
            "wakes": [{"wake": w.get("wake"), "attempt": w.get("attempt"), "reason": w.get("reason"),
                       "s": w.get("start") or (w.get("t", t0) - (w.get("seconds") or 0)), "e": w.get("end") or w.get("t", t0),
                       "kind": "plan" if w.get("state") == "idle" and not w.get("problems") else "planback",
                       "added": w.get("added") or [], "dropped": w.get("dropped") or [], "done": bool(w.get("done")),
                       "problems": w.get("problems") or [], "why": w.get("why")} for w in wakes],
            "turns": [{"member": t.get("member"), "turn": t.get("turn"), "todo": t.get("todo"), "s": t.get("start") or t.get("t", t0),
                       "e": t.get("end") or (t.get("t", t0) + (t.get("seconds") or 0)), "kind": t.get("kind"),
                       "status": t.get("status"), "score": t.get("score"), "state": t.get("state"), "entry": t.get("entry"),
                       "problem": t.get("problem"),
                       "tools": [[c["tool"], c["what"]] for c in calls_in(t, tools, t0)][:40]} for t in turns],
            "todos": [{"id": t["id"], "text": t.get("text"), "added": t.get("added") or 0, "taken": t.get("taken"),
                       "ended": t.get("ended"), "state": t["state"]} for t in todos]}
    blob = json.dumps(data, ensure_ascii=True).replace("</", "<\\/")
    reads = artifact_reads(tools)
    skills = skills_list(made, reads)
    return PAGE.format(title=title, name=esc(name), state=state, when=when, chips="".join(chips), data=blob, script=SCRIPT,
                       controls='<div class="controls" id="controls" hidden></div>' if live else "",
                       sent=control_list(controls, t0),
                       agents=agents_panel(wakes, turns, todos, summary, members, planner, bool(stop), made, reads),
                       division=division_svg(t0, t1, todos, made, members), knowledge=knowledge_svg(made, reads, members, t0),
                       health=health_panel(start, wakes, turns, summary),
                       loop=loop_svg(start, wakes, turns, todos, summary, members, planner),
                       timeline=timeline_svg(t0, t1, wakes, turns, members, stop.get("why") if stop else None, tools),
                       lineage=lineage_svg(made), skills=f'<section class="panel">{skills}</section>' if skills else "",
                       gather=gather_list(turns, tools, t0),
                       best=best_svg(t0, t1, turns), todos=todo_list(todos, t0), wakes=wake_list(wakes, t0))


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):  # ThreadingHTTPServer is Python 3.7+
    daemon_threads = True


def serve(out, host="127.0.0.1", port=8780, token=None, ready=None):
    """Serve the run's page (drawn again for every request) and take a person's commands for it (POST /control with
    "Authorization: Bearer <token>"). ready(server, token) is called once it listens (tests stop it with shutdown())."""
    from .engine import send_control  # engine imports this module; this one needs engine only here
    token = token or secrets.token_urlsafe(18)

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, code, body, kind="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if urllib.parse.urlparse(self.path).path not in ("/", "/index.html"):
                return self.send(404, {"error": "not found"})
            return self.send(200, render(out, live=True).encode("utf-8"), "text/html; charset=utf-8")

        def do_POST(self):
            if urllib.parse.urlparse(self.path).path != "/control":
                return self.send(404, {"error": "not found"})
            if not hmac.compare_digest(self.headers.get("Authorization") or "", "Bearer " + token):
                return self.send(403, {"error": "this needs the token from the link the server printed"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > 4096:
                return self.send(413, {"error": "too long"})
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                rec = send_control(out, body.get("command"), body.get("value"), by="web")
            except (ValueError, AttributeError) as exc:
                return self.send(400, {"error": str(exc)})
            return self.send(200, {"ok": True, "sent": rec})

    server = _Server((host, port), Handler)
    if ready:
        ready(server, token)
    else:
        print(f"serving {os.path.abspath(out)} at http://{host}:{server.server_address[1]}/#token={token}  (Ctrl-C stops)", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()


def save(out):
    page = render(out)
    tmp = os.path.join(out, "view.html.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(page)
    os.replace(tmp, os.path.join(out, "view.html"))  # a reader never sees half a page
    return os.path.join(out, "view.html")


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="python3 -m herdr_py.engineview", description="Draw (or serve) a run's page.")
    ap.add_argument("run", metavar="RUN_FOLDER")
    ap.add_argument("--serve", action="store_true", help="serve the page with a person's buttons instead of writing view.html")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8780)
    a = ap.parse_args(argv)
    if not os.path.isfile(os.path.join(a.run, "engine.jsonl")):
        print(f"herdr-py engineview: {a.run} holds no run (engine.jsonl)", file=sys.stderr)
        return 2
    if a.serve:
        try:
            serve(a.run, host=a.host, port=a.port)
        except KeyboardInterrupt:
            pass
        return 0
    print(save(a.run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
