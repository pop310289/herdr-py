"""One HTML page for an event-driven team run (engine.py): the loop drawn with this run's numbers, a timeline from the
first event to the end (the planner and every member in lanes, every turn a bar, every result that woke the planner
a dashed line), every todo from added to ended, every planner wake, and the best score over time. Built from
engine.jsonl, run.jsonl, kb/events.jsonl and summary.json only, so it can be opened while the run goes on (the
engine rewrites it after every event).

    python3 -m herdr_py.engineview runs/e1        # writes runs/e1/view.html

The page is complete without JavaScript (a phone app's preview runs none): it shows the run's final state. With
JavaScript a player is added on top: replay the run from the first event to the end, drag the time, tap a bar for its
details; the loop's boxes light up while that stage works and its counts follow the time. While the run goes on the
page reloads itself every few seconds. Data is escaped, never written into the page as markup.
"""
import html
import json
import os
import sys
import time


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
    summary = None
    path = os.path.join(out, "summary.json")
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as handle:
                summary = json.load(handle)
        except ValueError:
            summary = None
    return engine, turns, list(todos.values()), summary


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
    nodes = [("events", "an answer judged, a todo ended, the start"),
             (f"planner: {planner}", f"{len(wakes)} turns" + (f", {sum(planner_tokens)} tokens" if planner_tokens else "")),
             ("shared todo list", " · ".join(f"{n} {k}" for k, n in sorted(states.items())) or "empty"),
             (f"members: {', '.join(members)}", read_note),
             ("judge (a program)", f"{valid} valid, {invalid} invalid; {best}")]
    arrows = [f"woke the planner {times(len({w.get('wake') for w in wakes}))}" + (f" ({sent_back} sent back)" if sent_back else ""),
              f"{added} todos added, {dropped} dropped",
              f"{sum(1 for t in todos if t.get('member'))} todos taken",
              f"{answers} answers" + (f", {other} without one" if other else "")]
    box_x, box_w, box_h, gap = 14, 236, 48, 52
    parts = []
    keys = ("events", "planner", "todos", "members", "judge")
    arrow_keys = ("wake", "todo", "take", "answer")
    for i, (title, sub) in enumerate(nodes):
        y = 8 + i * (box_h + gap)
        parts.append(f'<g class="node" id="n-{keys[i]}"><rect x="{box_x}" y="{y}" width="{box_w}" height="{box_h}" rx="8"/>'
                     f'<text x="{box_x + 12}" y="{y + 20}" class="t">{esc(title)}</text>'
                     f'<text x="{box_x + 12}" y="{y + 38}" class="s" id="s-{keys[i]}">{esc(sub)}</text></g>')
        if i < len(arrows):
            y1, y2 = y + box_h, y + box_h + gap
            parts.append(f'<line class="flow" x1="{box_x + 40}" y1="{y1 + 2}" x2="{box_x + 40}" y2="{y2 - 8}"/>'
                         f'<polygon class="head" points="{box_x + 34},{y2 - 9} {box_x + 46},{y2 - 9} {box_x + 40},{y2 - 1}"/>'
                         f'<text x="{box_x + 52}" y="{y1 + gap / 2 + 4}" class="a" id="a-{arrow_keys[i]}">{esc(arrows[i])}</text>')
    top, bottom = 8 + box_h / 2, 8 + 4 * (box_h + gap) + box_h / 2
    rx = box_x + box_w + 30
    parts.append(f'<path class="flow back" d="M {box_x + box_w} {bottom} H {rx} V {top} H {box_x + box_w + 9}"/>'
                 f'<polygon class="head" points="{box_x + box_w + 9},{top - 6} {box_x + box_w + 9},{top + 6} {box_x + box_w + 1},{top}"/>'
                 f'<text class="a" transform="translate({rx + 14},{(top + bottom) / 2}) rotate(-90)" text-anchor="middle">'
                 f'verdicts go to the team knowledge base: the next event</text>')
    height = 8 + 5 * box_h + 4 * gap + 8
    return (f'<svg viewBox="0 0 330 {height}" width="330" height="{height}" role="img" '
            f'aria-label="the loop: events wake the planner, the planner keeps the todo list, members take todos, '
            f'a program judges, verdicts are the next events">{"".join(parts)}</svg>')


def timeline_svg(t0, t1, wakes, turns, members, stop_why):
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
            parts.append(f'<line class="event" data-t="{e}" x1="{col_x[t["member"]] + 4:.1f}" y1="{ey:.1f}" '
                         f'x2="{col_x["planner"] + col_w - 4:.1f}" y2="{ey:.1f}"/>')
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


SCRIPT = '(function () {\n  "use strict";\n  var el = document.getElementById("run-data"), svg = document.getElementById("timeline");\n  if (!el || !svg) { return; }\n  var D;\n  try { D = JSON.parse(el.textContent); } catch (err) { return; }\n  var span = Math.max(D.t1 - D.t0, 1e-6), TOP = +svg.getAttribute("data-top"), PH = +svg.getAttribute("data-h");\n  function Y(t) { return TOP + (t - D.t0) / span * PH; }\n  function $(id) { return document.getElementById(id); }\n  function fmt(v) { return String(Math.round(v * 1e6) / 1e6); }\n  function all(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }\n  var texts = ["s-events", "s-planner", "s-todos", "s-members", "s-judge", "a-wake", "a-todo", "a-take", "a-answer"];\n  var original = {};\n  texts.forEach(function (id) { if ($(id)) { original[id] = $(id).textContent; } });\n  var bars = all(".bar", svg), lines = all(".event", svg), now = $("now");\n  var todoCards = all("#todos li[data-added]"), wakeCards = all("#wakes li[data-s]");\n  bars.forEach(function (g) {\n    var r = g.querySelector("rect");\n    g._y = +r.getAttribute("y"); g._h = +r.getAttribute("height");\n    g._s = +g.getAttribute("data-s"); g._e = +g.getAttribute("data-e");\n    g.style.cursor = "pointer";\n    g.addEventListener("click", function () { show(g.getAttribute("data-i")); });\n  });\n  var slot = $("player");\n  slot.innerHTML = \'<button type="button" id="play">▶ Replay</button>\' +\n    \'<input type="range" id="scrub" min="0" max="1000" step="1" value="1000" aria-label="time in the run">\' +\n    \'<span id="clock" class="muted"></span>\';\n  var play = $("play"), scrub = $("scrub"), clock = $("clock"), detail = $("detail");\n  var playing = false, t = D.t1, last = 0, rate = Math.max(1, span / 24);\n  function setText(id, text) { var n = $(id); if (n) { n.textContent = text; } }\n  function setOn(id, on) { var n = $(id); if (n) { n.classList.toggle("on", !!on); } }\n  function count(list, test) { var n = 0; list.forEach(function (x) { if (test(x)) { n += 1; } }); return n; }\n  function at(time) {\n    t = time;\n    bars.forEach(function (g) {\n      var r = g.querySelector("rect");\n      if (time < g._s) { g.style.opacity = "0"; return; }\n      g.style.opacity = "1";\n      r.setAttribute("height", (time >= g._e ? g._h : Math.max(3, Y(time) - g._y)).toFixed(1));\n      g.classList.toggle("live", time < g._e);\n    });\n    lines.forEach(function (l) { l.style.opacity = +l.getAttribute("data-t") <= time ? "1" : "0"; });\n    now.setAttribute("y1", Y(time).toFixed(1)); now.setAttribute("y2", Y(time).toFixed(1));\n    now.style.opacity = "1";\n    var flash = Math.max(0.6, span / 60);\n    var planning = D.wakes.some(function (w) { return w.s <= time && time < w.e; });\n    var working = D.turns.filter(function (m) { return m.s <= time && time < m.e; });\n    var ended = D.turns.filter(function (m) { return m.e <= time; });\n    var judged = ended.some(function (m) { return time - m.e < flash; });\n    setOn("n-planner", planning); setOn("n-members", working.length > 0); setOn("n-judge", judged);\n    setOn("n-events", judged || D.wakes.some(function (w) { return w.s <= time && time - w.s < flash; }));\n    var woke = {};\n    D.wakes.forEach(function (w) { if (w.s <= time) { woke[w.wake] = 1; } });\n    var back = count(D.wakes, function (w) { return w.e <= time && w.kind === "planback"; });\n    var added = count(D.todos, function (x) { return x.added <= time; });\n    var dropped = count(D.todos, function (x) { return x.state === "dropped" && x.ended <= time; });\n    var taken = count(D.todos, function (x) { return x.taken && x.taken <= time; });\n    var done = count(D.todos, function (x) { return x.state === "done" && x.ended <= time; });\n    var failed = count(D.todos, function (x) { return x.state === "failed" && x.ended <= time; });\n    var answers = count(ended, function (m) { return m.kind === "result"; });\n    var valid = ended.filter(function (m) { return m.status === "valid"; });\n    var invalid = count(ended, function (m) { return m.status === "invalid"; });\n    var best = null;\n    valid.forEach(function (m) { if (best === null || m.score > best) { best = m.score; } });\n    var nw = Object.keys(woke).length;\n    setText("a-wake", "woke the planner " + nw + (nw === 1 ? " time" : " times") + (back ? " (" + back + " sent back)" : ""));\n    setText("a-todo", added + " todos added, " + dropped + " dropped");\n    setText("a-take", taken + " todos taken");\n    setText("a-answer", answers + " answers" + (ended.length > answers ? ", " + (ended.length - answers) + " without one" : ""));\n    setText("s-todos", (added - taken - dropped) + " open · " + (taken - done - failed) + " taken · " + done + " done · " + failed + " failed");\n    setText("s-members", working.length ? "working now: " + working.map(function (m) { return m.member + " on " + m.todo; }).join(", ") : "waiting for a todo");\n    setText("s-planner", planning ? "planning now" : original["s-planner"]);\n    setText("s-judge", valid.length + " valid, " + invalid + " invalid; " + (best === null ? "no valid answer yet" : "best " + fmt(best)));\n    todoCards.forEach(function (li) { li.classList.toggle("future", +li.getAttribute("data-added") > time); });\n    wakeCards.forEach(function (li) { li.classList.toggle("future", +li.getAttribute("data-s") > time); });\n    clock.textContent = (time - D.t0).toFixed(1) + " s of " + span.toFixed(0) + " s";\n    scrub.value = String(Math.round((time - D.t0) / span * 1000));\n  }\n  function rest() {\n    bars.forEach(function (g) { g.style.opacity = "1"; g.classList.remove("live"); g.querySelector("rect").setAttribute("height", g._h.toFixed(1)); });\n    lines.forEach(function (l) { l.style.opacity = "1"; });\n    now.style.opacity = "0";\n    ["n-events", "n-planner", "n-todos", "n-members", "n-judge"].forEach(function (id) { setOn(id, false); });\n    Object.keys(original).forEach(function (id) { setText(id, original[id]); });\n    todoCards.concat(wakeCards).forEach(function (li) { li.classList.remove("future"); });\n    clock.textContent = span.toFixed(0) + " s in all" + (D.finished ? "" : " so far");\n    scrub.value = "1000";\n    t = D.t1;\n  }\n  function stop() { playing = false; play.textContent = "▶ Replay"; }\n  function frame(ts) {\n    if (!playing) { return; }\n    var next = t + (last ? (ts - last) / 1000 : 0) * rate;\n    last = ts;\n    if (next >= D.t1) { stop(); rest(); return; }\n    at(next);\n    window.requestAnimationFrame(frame);\n  }\n  play.addEventListener("click", function () {\n    if (playing) { stop(); return; }\n    playing = true; last = 0; play.textContent = "❚❚ Pause";\n    if (t >= D.t1) { at(D.t0); }\n    window.requestAnimationFrame(frame);\n  });\n  scrub.addEventListener("input", function () {\n    stop();\n    var v = +scrub.value;\n    if (v >= 1000) { rest(); } else { at(D.t0 + v / 1000 * span); }\n  });\n  function show(key) {\n    var item = key.charAt(0) === "w" ? D.wakes[+key.slice(1)] : D.turns[+key.slice(1)];\n    if (!item) { return; }\n    var out = [];\n    if (key.charAt(0) === "w") {\n      out.push("planner wake " + item.wake + ", attempt " + item.attempt + " · " + (item.e - item.s).toFixed(1) + " s");\n      out.push("woken because: " + item.reason);\n      if (item.added && item.added.length) { out.push("added " + item.added.join(", ")); }\n      if (item.dropped && item.dropped.length) { out.push("dropped " + item.dropped.join(", ")); }\n      if (item.problems && item.problems.length) { out.push("sent back: " + item.problems.join("; ")); }\n      if (item.done) { out.push("said the task is done"); }\n      if (item.why) { out.push(item.why); }\n    } else {\n      var todo = D.todos.filter(function (x) { return x.id === item.todo; })[0] || {};\n      out.push(item.member + ", turn " + item.turn + " · " + (item.e - item.s).toFixed(1) + " s");\n      out.push("todo " + item.todo + ": " + (todo.text || ""));\n      out.push((item.status || item.state || "") + (typeof item.score === "number" ? " · score " + fmt(item.score) : "") + (item.entry ? " · entry " + item.entry : ""));\n      if (item.problem) { out.push(item.problem); }\n    }\n    detail.textContent = "";\n    out.forEach(function (line) { var d = document.createElement("div"); d.textContent = line; detail.appendChild(d); });\n    detail.hidden = false;\n  }\n  rest();\n  if (!D.finished) {\n    window.setTimeout(function again() {\n      if (!playing && +scrub.value >= 1000) { window.location.reload(); } else { window.setTimeout(again, 4000); }\n    }, 5000);\n  }\n})();'


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
:root {{ --bg:#F3F5F4; --surface:#FFFFFF; --ink:#16201C; --muted:#5A6661; --rule:#D4DBD8; --accent:#1D5C70;
  --pass:#2B7448; --pass-bg:#E2F1E7; --fail:#A93636; --fail-bg:#F6E0DF; --wait:#94600E; --wait-bg:#F7EBD6; --run-bg:#E1EEF2; }}
@media (prefers-color-scheme: dark) {{ :root {{ color-scheme:dark; --bg:#101514; --surface:#171E1C; --ink:#E0E7E4;
  --muted:#96A29D; --rule:#2A3431; --accent:#6DB3C8; --pass:#79C995; --pass-bg:#1A3123; --fail:#E68B88; --fail-bg:#3A1D1C;
  --wait:#E1AE5C; --wait-bg:#33280F; --run-bg:#16303A; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 -apple-system,"PingFang TC","Noto Sans TC",sans-serif;
  padding-inline:16px; padding-block:16px 40px; }}
main {{ max-width:1100px; margin:0 auto; display:flex; flex-direction:column; gap:14px; }}
h1 {{ font-size:1.25rem; margin:0; overflow-wrap:anywhere; }} h2 {{ font-size:1rem; margin:8px 0 0; }}
.muted {{ color:var(--muted); font-size:.85rem; overflow-wrap:anywhere; }}
.chips {{ display:flex; flex-wrap:wrap; gap:6px; }} .chip {{ font-size:.78rem; border:1px solid var(--rule); border-radius:999px; padding:1px 9px; white-space:nowrap; }}
.chip.bad {{ color:var(--fail); background:var(--fail-bg); border-color:transparent; }}
.chip.good {{ color:var(--pass); background:var(--pass-bg); border-color:transparent; }}
.fig {{ overflow-x:auto; background:var(--surface); border:1px solid var(--rule); border-radius:8px; padding:6px; }}
.fig svg {{ display:block; }}
.node rect {{ fill:var(--surface); stroke:var(--accent); stroke-width:1.5; }}
.node .t {{ fill:var(--ink); font-size:13px; font-weight:600; }} .node .s, .a, .ax, .ln {{ fill:var(--muted); font-size:11px; }}
.ln {{ fill:var(--ink); font-size:12px; }}
.flow {{ fill:none; stroke:var(--accent); stroke-width:1.5; }} .flow.back {{ stroke-dasharray:5 4; }} .head {{ fill:var(--accent); }}
.lane, .tick {{ stroke:var(--rule); stroke-width:1; }}
.bar rect {{ stroke-width:1.5; }} .bar text {{ font-size:10px; fill:var(--ink); }}
.bar.plan rect {{ fill:var(--run-bg); stroke:var(--accent); }} .bar.planback rect {{ fill:var(--wait-bg); stroke:var(--wait); }}
.bar.pass rect {{ fill:var(--pass-bg); stroke:var(--pass); }} .bar.fail rect {{ fill:var(--fail-bg); stroke:var(--fail); }}
.bar.bad rect {{ fill:var(--fail); stroke:var(--fail); }} .bar.none rect {{ fill:var(--surface); stroke:var(--muted); stroke-dasharray:3 2; }}
.event {{ stroke:var(--muted); stroke-width:1; stroke-dasharray:2 3; }} .stop {{ stroke:var(--fail); stroke-width:1.5; }}
.best {{ fill:none; stroke:var(--pass); stroke-width:2; }} .dots circle {{ fill:var(--pass); }}
.legend {{ display:flex; flex-wrap:wrap; gap:4px 14px; font-size:.78rem; color:var(--muted); }}
.legend i {{ display:inline-block; width:12px; height:10px; border-radius:3px; border:1.5px solid var(--muted); margin-right:5px; vertical-align:-1px; }}
.legend i.plan {{ background:var(--run-bg); border-color:var(--accent); }} .legend i.planback {{ background:var(--wait-bg); border-color:var(--wait); }}
.legend i.pass {{ background:var(--pass-bg); border-color:var(--pass); }} .legend i.fail {{ background:var(--fail-bg); border-color:var(--fail); }}
.legend i.bad {{ background:var(--fail); border-color:var(--fail); }} .legend i.none {{ border-style:dashed; }}
.cards {{ list-style:none; margin:0; padding:0; display:flex; flex-direction:column; gap:8px; font-size:.86rem; }}
.cards li {{ background:var(--surface); border:1px solid var(--rule); border-radius:8px; padding:8px 10px; overflow-wrap:anywhere; }}
.st {{ font-size:.75rem; border-radius:999px; padding:0 7px; border:1px solid var(--rule); }}
.st.done {{ color:var(--pass); background:var(--pass-bg); border-color:transparent; }}
.st.failed {{ color:var(--fail); background:var(--fail-bg); border-color:transparent; }}
.st.taken {{ color:var(--accent); background:var(--run-bg); border-color:transparent; }}
.said {{ font-size:.8rem; color:var(--muted); }}
.player {{ position:sticky; top:env(safe-area-inset-top, 0px); z-index:2; display:flex; flex-wrap:wrap; align-items:center;
  gap:8px; background:var(--bg); padding-block:6px; border-bottom:1px solid var(--rule); }}
.player:empty {{ display:none; }}
.player button {{ font:inherit; font-size:.9rem; padding:6px 14px; border-radius:999px; border:1px solid var(--accent);
  background:var(--surface); color:var(--accent); }}
.player input {{ flex:1 1 150px; min-width:0; accent-color:var(--accent); }}
.node.on rect {{ fill:var(--run-bg); stroke-width:3.5; }} .bar.live rect {{ stroke-width:2.5; }}
.now {{ stroke:var(--accent); stroke-width:2; }} .future {{ opacity:.3; }}
.detail {{ background:var(--surface); border:1px solid var(--rule); border-radius:8px; padding:8px 10px; font-size:.86rem; overflow-wrap:anywhere; }}
</style></head>
<body><main>
<h1>{title}</h1>
<div class="muted">{when}</div>
<div class="chips">{chips}</div>
<div class="player" id="player"></div>
<h2>The loop, with this run's numbers</h2>
<div class="fig">{loop}</div>
<h2>From the first event to the end</h2>
<div class="fig">{timeline}</div>
<div class="legend"><span><i class="plan"></i>planner turn</span><span><i class="planback"></i>planner reply sent back</span>
<span><i class="pass"></i>valid answer</span><span><i class="fail"></i>invalid answer or failure</span><span><i class="bad"></i>backend broke</span>
<span><i class="none"></i>no answer or still running</span><span>dotted line: a result that woke the planner</span></div>
<div class="detail" id="detail" hidden></div>
<h2>Best verified score over time</h2>
<div class="fig">{best}</div>
<h2>Every todo</h2>
{todos}
<h2>Every planner turn</h2>
{wakes}
</main>
<script type="application/json" id="run-data">{data}</script>
<script>{script}</script>
</body></html>
"""


def render(out):
    engine, turns, todos, summary = load(out)
    start = next((e for e in engine if e.get("kind") == "start"), {})
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
            chips.append(f'<span class="chip">{esc(summary["tokens"])} tokens'
                         + (f", planner {share:.0%}" if isinstance(share, (int, float)) else "") + "</span>")
    if stop:
        chips.append(f'<span class="chip bad">stopped: {esc(stop.get("why"))}</span>')
    when = ("started " + esc(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t0)))
            + (f" · {t1 - t0:.0f} s · finished" if stop else " · still running (written after every event)"))
    title = esc(f"Event-driven team {os.path.basename(os.path.abspath(out))}")
    data = {"t0": t0, "t1": t1, "finished": bool(stop), "planner": planner, "members": members,
            "wakes": [{"wake": w.get("wake"), "attempt": w.get("attempt"), "reason": w.get("reason"),
                       "s": w.get("start") or (w.get("t", t0) - (w.get("seconds") or 0)), "e": w.get("end") or w.get("t", t0),
                       "kind": "plan" if w.get("state") == "idle" and not w.get("problems") else "planback",
                       "added": w.get("added") or [], "dropped": w.get("dropped") or [], "done": bool(w.get("done")),
                       "problems": w.get("problems") or [], "why": w.get("why")} for w in wakes],
            "turns": [{"member": t.get("member"), "turn": t.get("turn"), "todo": t.get("todo"), "s": t.get("start") or t.get("t", t0),
                       "e": t.get("end") or (t.get("t", t0) + (t.get("seconds") or 0)), "kind": t.get("kind"),
                       "status": t.get("status"), "score": t.get("score"), "state": t.get("state"), "entry": t.get("entry"),
                       "problem": t.get("problem")} for t in turns],
            "todos": [{"id": t["id"], "text": t.get("text"), "added": t.get("added") or 0, "taken": t.get("taken"),
                       "ended": t.get("ended"), "state": t["state"]} for t in todos]}
    blob = json.dumps(data, ensure_ascii=True).replace("</", "<\\/")
    return PAGE.format(title=title, when=when, chips="".join(chips), data=blob, script=SCRIPT,
                       loop=loop_svg(start, wakes, turns, todos, summary, members, planner),
                       timeline=timeline_svg(t0, t1, wakes, turns, members, stop.get("why") if stop else None),
                       best=best_svg(t0, t1, turns), todos=todo_list(todos, t0), wakes=wake_list(wakes, t0))


def save(out):
    page = render(out)
    tmp = os.path.join(out, "view.html.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(page)
    os.replace(tmp, os.path.join(out, "view.html"))  # a reader never sees half a page
    return os.path.join(out, "view.html")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python3 -m herdr_py.engineview RUN_FOLDER")
    print(save(sys.argv[1]))
