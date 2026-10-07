// herdr-py team dashboard: plain JavaScript, no build step, no external scripts.
// Everything shown comes from the run folder or the daemon, i.e. from model output: it enters the page only through
// textContent, createElement / createElementNS and setAttribute, never parsed as markup.
"use strict";

const TOKEN_KEY = "herdr-py-dashboard-token";
const THEME_KEY = "herdr-py-dashboard-theme";
const SVG_NS = "http://www.w3.org/2000/svg";
const KINDS = ["message", "check", "control", "end"];
const GROUP = { starting: "working", working: "working", retry: "working", blocked: "blocked", idle: "idle",
  aborted: "stopped", stopped: "stopped", error: "error" };
const LABEL = { blocked: "needs an answer", aborted: "stopped", retry: "retrying" };
const MAX_LINES = 500;  // conversation lines kept in the page at once (the newest)
const THEMES = ["auto", "light", "dark"];

const $ = (sel) => document.querySelector(sel);

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function svgEl(tag, attrs, text) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const name of Object.keys(attrs || {})) node.setAttribute(name, String(attrs[name]));
  if (text !== undefined) node.textContent = String(text);
  return node;
}

function readToken() {
  const match = location.hash.match(/token=([^&]+)/);
  if (match) {
    const token = decodeURIComponent(match[1]);
    try { sessionStorage.setItem(TOKEN_KEY, token); } catch (e) { /* storage blocked */ }
    history.replaceState(null, "", location.pathname);  // keep the token out of the address bar
    return token;
  }
  try { return sessionStorage.getItem(TOKEN_KEY) || ""; } catch (e) { return ""; }
}

const app = {
  token: readToken(),
  state: null,          // the latest state from the server
  chat: [],             // every conversation line so far
  offset: 0,            // the server's clock minus ours, in seconds
  source: null,
  timeline: { count: 0, filter: null, shown: 0 },  // lines rendered so far and the filter they were rendered with
  keys: {},             // what each section last rendered (skip sections that did not change)
  step: null,           // the picture slider's position
  follow: true,         // the slider sits on the newest picture and moves with it
  member: "",
  kinds: new Set(KINDS),
};

// ------------------------------------------------------------------ formatting
const fmt3 = (v) => (typeof v === "number" ? v.toFixed(3) : v === "inf" ? "∞" : "–");
const fmtInt = (v) => (typeof v === "number" ? v.toLocaleString("en-US") : "–");
const pad2 = (n) => String(n).padStart(2, "0");

function fmtPsnr(v) {
  if (v === "inf") return "∞ dB";
  return typeof v === "number" ? v.toFixed(1) + " dB" : "–";
}

function fmtTime(t) {
  if (typeof t !== "number") return "--:--:--";
  const d = new Date(t * 1000);
  return pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds());
}

function fmtDuration(s) {
  s = Math.max(0, Math.round(s));
  if (s < 60) return s + "s";
  const m = Math.floor(s / 60);
  if (m < 60) return m + "m " + pad2(s % 60) + "s";
  return Math.floor(m / 60) + "h " + pad2(m % 60) + "m";
}

function tail(text, chars, lines) {
  let out = String(text || "").replace(/\s+$/, "");
  const parts = out.split("\n");
  let cut = false;
  if (parts.length > lines) { out = parts.slice(-lines).join("\n"); cut = true; }
  if (out.length > chars) { out = out.slice(-chars); cut = true; }
  return (cut ? "…" : "") + out;
}

function changed(name, value) {
  const key = JSON.stringify(value);
  if (app.keys[name] === key) return false;
  app.keys[name] = key;
  return true;
}

const serverNow = () => Date.now() / 1000 + app.offset;

// ------------------------------------------------------------------ connection
function setConn(state, text) {
  const node = $("#conn");
  node.dataset.state = state;
  node.textContent = text;
}

function connect() {
  if (!app.token) { setConn("bad", "no token: open the link the dashboard printed"); return; }
  const source = new EventSource("/api/events?token=" + encodeURIComponent(app.token));
  app.source = source;
  source.onopen = () => setConn("ok", "live");
  source.onerror = () => {
    if (source.readyState === EventSource.CLOSED) setConn("bad", "refused or gone: open the link the dashboard printed");
    else setConn("bad", "reconnecting…");
  };
  source.onmessage = (msg) => {
    let data;
    try { data = JSON.parse(msg.data); } catch (e) { return; }
    apply(data);
  };
}

function reconnect() {
  if (app.source) app.source.close();
  connect();
}

function apply(data) {
  if (typeof data.now === "number") app.offset = data.now - Date.now() / 1000;
  if (data.type === "snapshot") {
    app.chat = data.chat.slice();
    app.timeline = { count: 0, filter: null, shown: 0 };
  } else if (data.type === "update") {
    if (data.chat_from !== app.chat.length) { reconnect(); return; }  // out of step: a new connection sends a snapshot
    for (const line of data.chat) app.chat.push(line);
  } else {
    return;
  }
  app.state = data;
  setConn("ok", "live");
  render();
}

function render() {
  renderStatus();
  renderMembers();
  renderTimeline();
  renderScores();
  renderPictures();
  renderLessons();
}

// ------------------------------------------------------------------ status and final numbers
function tile(item) {
  const li = el("li", "tile");
  li.append(el("span", "label", item[0]), el("span", "value", item[1]));
  if (item[2]) li.append(el("span", "sub", item[2]));
  return li;
}

function renderStatus() {
  const s = app.state;
  const run = s.run;
  $("#run-name").textContent = run.name;
  document.title = run.name + " · team run";
  const p = $("#progress");
  p.replaceChildren();
  if (!run.has_chat) {
    p.append(el("strong", "", "Waiting"), " for the run to start: there is no chat.jsonl yet.");
  } else {
    if (run.ended) {
      p.append(el("strong", "", "Finished"));
    } else if (run.progress) {
      const pr = run.progress;
      p.append(el("strong", "", "Running"), " · round " + pr.round + " · row " + pr.row + " · " + pr.member
        + (pr.kind === "draft" ? " drafting" : " revising"));
    } else {
      p.append(el("strong", "", "Running"));
    }
    let times = " · started " + fmtTime(run.started) + " · last line " + fmtTime(run.last);
    if (typeof run.started === "number" && typeof run.last === "number") times += " (" + fmtDuration(run.last - run.started) + ")";
    p.append(times);
    if (run.bad_lines) p.append(" · " + run.bad_lines + " unreadable line(s) skipped");
    if (run.earlier_lines) p.append(" · scores and pictures are from the latest run in this folder");
  }
  const tiles = [];  // [label, value, note]
  const f = s.final;
  if (f) {
    const missing = Array.isArray(f.missing) ? f.missing : null;
    tiles.push(["Whole-slide match", fmt3(f.match), "1 = identical"]);
    if (typeof f.strict === "number") {  // scoring.py: match plus borders and text colour
      tiles.push(["Strict score", fmt3(f.strict), f.score_mode === "strict" ? "decided which revisions were kept" : "logged; match decided"]);
    }
    tiles.push(["PSNR", fmtPsnr(f.psnr), "higher is closer"],
      ["Missing labels", missing ? String(missing.length) : "–",
        missing ? (missing.length ? missing.join(", ") : "every required label is there") : ""]);
  }
  for (const r of s.rows || []) {
    const draft = r.points.find((pt) => pt.kind === "draft");
    tiles.push(["Row " + r.row + " kept", fmt3(r.best),
      draft ? (draft.valid ? "draft " + fmt3(draft.match) : draft.pending ? "draft in progress" : "draft could not be drawn") : ""]);
  }
  if (changed("tiles", tiles)) $("#tiles").replaceChildren(...tiles.map(tile));
}

// ------------------------------------------------------------------ members
function renderMembers() {
  const s = app.state;
  const note = $("#members-note");
  let text = "";
  if (s.run.members_error) text = "The daemon did not answer (" + s.run.members_error + "); showing what it said last.";
  else if (!s.members.length && s.run.members_from === null) {
    text = "No member states: Codex members write codex/agents.json; for OpenCode members start the dashboard with --socket.";
  } else if (!s.members.length) text = "No members yet.";
  note.textContent = text;
  note.hidden = !text;
  if (!changed("members", s.members)) return;
  $("#members").replaceChildren(...s.members.map(memberCard));
  tick();
}

function memberCard(m) {
  const li = $("#member-tpl").content.firstElementChild.cloneNode(true);
  const group = GROUP[m.state] || "unknown";
  li.dataset.group = group;
  li.querySelector(".m-name").textContent = m.name;
  const chip = li.querySelector(".chip");
  chip.textContent = LABEL[m.state] || m.state;
  chip.dataset.group = group;
  const since = li.querySelector(".m-since");
  if (typeof m.since === "number") since.dataset.since = String(m.since);
  li.querySelector(".m-tokens").textContent = fmtInt(m.tokens);
  li.querySelector(".m-turns").textContent = fmtInt(m.turns);
  li.querySelector(".m-sessions").textContent = typeof m.sessions === "number" ? m.sessions + (m.sessions_more ? "+" : "") : "–";
  const counters = li.querySelector(".m-counters");
  for (const c of m.counters || []) {
    const box = el("div");
    box.append(el("dt", "", c.label), el("dd", "", fmtInt(c.value)));
    counters.append(box);
  }
  counters.hidden = !(m.counters && m.counters.length);
  const pending = li.querySelector(".m-pending");
  if (m.pending && m.pending.length) {
    pending.textContent = "Waiting for an answer: " + m.pending.join("; ") + " (answer it in herdr-py tui or the daemon's web page)";
    pending.hidden = false;
  }
  const kind = m.words_kind;
  li.querySelector(".m-words-label").textContent = kind === "said" ? "Last line in the conversation"
    : kind === "reasoning" ? "Thinking (latest)" : "Last words";
  li.querySelector(".m-words").textContent = m.words ? tail(m.words, 240, 5) : "nothing yet";
  return li;
}

function tick() {
  const now = serverNow();
  for (const node of document.querySelectorAll(".m-since[data-since]")) {
    node.textContent = "for " + fmtDuration(now - Number(node.dataset.since));
  }
}

// ------------------------------------------------------------------ conversation
const kindOf = (line) => (KINDS.indexOf(line.kind) >= 0 ? line.kind : "message");
const matches = (line) => app.kinds.has(kindOf(line)) && (!app.member || line.from === app.member || line.to === app.member);
const filterKey = () => app.member + "|" + Array.from(app.kinds).sort().join(",");

function lineItem(line) {
  const li = el("li", "line");
  li.dataset.kind = kindOf(line);
  const head = el("div", "line-head");
  const time = el("time", "", fmtTime(line.t));
  if (typeof line.t === "number") time.setAttribute("datetime", new Date(line.t * 1000).toISOString());
  const arrow = el("span", "", " → ");
  arrow.setAttribute("aria-hidden", "true");
  head.append(time, el("strong", "from", line.from), arrow, el("span", "sr-only", " to "), el("span", "to", line.to));
  if (kindOf(line) !== "message") head.append(el("span", "kind", line.kind));
  li.append(head, el("p", "text", line.text));
  return li;
}

function updateMemberOptions() {
  const select = $("#f-member");
  const known = new Set(Array.from(select.options).map((o) => o.value));
  for (const line of app.chat) {
    for (const name of [line.from, line.to]) {
      if (name && !known.has(name)) {
        known.add(name);
        const option = el("option", "", name);
        option.value = name;
        select.append(option);
      }
    }
  }
}

function renderTimeline() {
  updateMemberOptions();
  const box = $("#timeline");
  const key = filterKey();
  const t = app.timeline;
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 24;
  if (t.filter !== key || t.count > app.chat.length) {
    const shown = app.chat.filter(matches);
    box.setAttribute("aria-busy", "true");
    box.replaceChildren(...shown.slice(-MAX_LINES).map(lineItem));
    box.removeAttribute("aria-busy");
    app.timeline = { count: app.chat.length, filter: key, shown: shown.length };
    box.scrollTop = box.scrollHeight;
    $("#latest").hidden = true;
  } else if (t.count < app.chat.length) {
    const fresh = app.chat.slice(t.count).filter(matches);
    for (const line of fresh) box.append(lineItem(line));
    while (box.childElementCount > MAX_LINES) box.firstElementChild.remove();
    t.count = app.chat.length;
    t.shown += fresh.length;
    if (fresh.length) {
      if (atBottom) box.scrollTop = box.scrollHeight;
      else $("#latest").hidden = false;
    }
  }
  const total = app.chat.length;
  const shown = app.timeline.shown;
  $("#talk-count").textContent = total === 0 ? "No lines yet."
    : (shown === total ? total + " lines" : shown + " of " + total + " lines match the filter")
      + (shown > MAX_LINES ? " (the newest " + MAX_LINES + " are shown)" : "") + ", oldest first.";
}

// ------------------------------------------------------------------ scores per row
function domain(rows) {
  const values = [];
  for (const r of rows) for (const p of r.points) if (typeof p.match === "number") values.push(p.match);
  if (!values.length) return [0, 1];
  const step = 0.05;
  let lo = Math.max(0, Math.floor((Math.min(...values) - 0.02) / step) * step);
  let hi = Math.min(1, Math.ceil((Math.max(...values) + 0.02) / step) * step);
  if (hi - lo < 0.1) { if (hi + 0.1 <= 1) hi = lo + 0.1; else lo = hi - 0.1; }
  return [Math.round(lo * 100) / 100, Math.round(hi * 100) / 100];
}

function ticks(lo, hi) {
  const span = hi - lo;
  const step = span <= 0.3 ? 0.05 : span <= 0.6 ? 0.1 : 0.2;
  const out = [];
  for (let v = Math.ceil(lo / step - 1e-9) * step; v <= hi + 1e-9; v += step) out.push(Math.round(v * 100) / 100);
  return out;
}

function verdict(p) {
  if (p.pending) return "in progress";
  if (!p.valid) return "could not be drawn";
  return p.accepted ? (p.kind === "draft" ? "kept (first version)" : "accepted: kept") : "rejected: the earlier version stays";
}

const turnName = (p, i, points) => (p.kind === "draft" ? "draft"
  : "rev " + points.slice(0, i + 1).filter((q) => q.kind !== "draft").length);

function chartCard(row, dom, width) {
  const card = el("article", "chart");
  const draft = row.points.find((p) => p.kind === "draft");
  const revisions = row.points.filter((p) => p.kind !== "draft" && !p.pending);
  const sub = "kept " + fmt3(row.best)
    + (draft ? " · draft " + (draft.valid ? fmt3(draft.match) : draft.pending ? "in progress" : "could not be drawn") : "")
    + " · revisions kept " + revisions.filter((p) => p.valid && p.accepted).length + " of " + revisions.length;
  card.append(el("h3", "", "Row " + row.row), el("p", "sub", sub));

  const W = Math.max(240, Math.round(width)), H = 176;
  const m = { l: 40, r: 50, t: 12, b: 44 };
  const n = Math.max(row.points.length, 1);
  const band = (W - m.l - m.r) / n;
  const x = (i) => m.l + (i + 0.5) * band;
  const y = (v) => m.t + ((dom[1] - v) / (dom[1] - dom[0])) * (H - m.t - m.b);
  const described = row.points.map((p, i) => turnName(p, i, row.points) + " by " + p.drawer + " "
    + (p.valid ? fmt3(p.match) + " " : "") + verdict(p)).join("; ");
  const chart = svgEl("svg", { viewBox: "0 0 " + W + " " + H, width: W, height: H, role: "img",
    "aria-label": "Row " + row.row + " picture match: " + described + ". Kept: " + fmt3(row.best) + "." });

  for (const v of ticks(dom[0], dom[1])) {
    chart.append(svgEl("line", { class: "grid-line", x1: m.l, x2: W - m.r + 8, y1: y(v), y2: y(v) }));
    chart.append(svgEl("text", { class: "axis-text", x: m.l - 6, y: y(v) + 4, "text-anchor": "end" }, v.toFixed(2)));
  }
  row.points.forEach((p, i) => {
    chart.append(svgEl("text", { class: "axis-text strong", x: x(i), y: H - m.b + 18, "text-anchor": "middle" },
      turnName(p, i, row.points)));
    chart.append(svgEl("text", { class: "axis-text", x: x(i), y: H - m.b + 33, "text-anchor": "middle" },
      p.pending ? p.drawer + " …" : p.drawer));
  });

  let d = "";
  let last = null;
  row.kept.forEach((k, i) => {
    if (typeof k !== "number") { last = null; return; }
    d += last === null ? "M" + x(i) + " " + y(k) : " H" + x(i) + " V" + y(k);
    last = k;
  });
  if (d) chart.append(svgEl("path", { class: "kept-line", d: d }));
  const lastKept = row.kept.length ? row.kept[row.kept.length - 1] : null;
  if (typeof lastKept === "number") {
    chart.append(svgEl("text", { class: "value-text", x: x(row.kept.length - 1) + 10, y: y(lastKept) + 4 }, fmt3(lastKept)));
  }

  row.points.forEach((p, i) => {
    if (p.pending) return;
    const cx = x(i);
    const cy = p.valid && typeof p.match === "number" ? y(p.match) : y(dom[0]);
    if (!p.valid || typeof p.match !== "number") {
      chart.append(svgEl("path", { class: "cross", d: "M" + (cx - 5) + " " + (cy - 5) + " L" + (cx + 5) + " " + (cy + 5)
        + " M" + (cx + 5) + " " + (cy - 5) + " L" + (cx - 5) + " " + (cy + 5) }));
    } else if (p.accepted) {
      chart.append(svgEl("circle", { class: "dot-accepted", cx: cx, cy: cy, r: 5 }));
    } else {
      chart.append(svgEl("circle", { class: "dot-rejected", cx: cx, cy: cy, r: 4.5 }));
    }
    const hit = svgEl("circle", { class: "hit", cx: cx, cy: cy, r: 14, tabindex: 0,
      "aria-label": "Row " + row.row + ", " + turnName(p, i, row.points) + " by " + p.drawer + ": "
        + (p.valid ? "match " + fmt3(p.match) + ", " : "") + verdict(p) });
    const show = () => showTip(hit, row, p, i);
    hit.addEventListener("pointerenter", show);
    hit.addEventListener("focus", show);
    hit.addEventListener("pointerleave", hideTip);
    hit.addEventListener("blur", hideTip);
    chart.append(hit);
  });
  card.append(chart, tableView(row));
  return card;
}

function tableView(row) {
  const details = el("details", "table-view");
  details.append(el("summary", "", "Values"));
  const wrap = el("div", "table-wrap");
  const table = el("table");
  const head = el("tr");
  for (const name of ["Turn", "By", "Kind", "Match", "Labels missing", "Result"]) {
    const th = el("th", "", name);
    th.setAttribute("scope", "col");
    head.append(th);
  }
  const thead = el("thead");
  thead.append(head);
  const body = el("tbody");
  row.points.forEach((p, i) => {
    const tr = el("tr");
    const missing = Array.isArray(p.missing) ? (p.missing.length ? p.missing.join(", ") : "0")
      : typeof p.missing_count === "number" ? String(p.missing_count) : "–";
    for (const value of [p.turn, p.drawer, turnName(p, i, row.points), p.valid ? fmt3(p.match) : "–", missing, verdict(p)]) {
      tr.append(el("td", "", value === null || value === undefined ? "–" : value));
    }
    body.append(tr);
  });
  table.append(thead, body);
  wrap.append(table);
  details.append(wrap);
  return details;
}

function showTip(target, row, p, i) {
  const tip = $("#tip");
  const lines = [el("strong", "", p.valid ? fmt3(p.match) : "no picture"),
    el("div", "", "Row " + row.row + " · turn " + p.turn + " · " + turnName(p, i, row.points) + " by " + p.drawer),
    el("div", "", verdict(p))];
  const missing = Array.isArray(p.missing) ? p.missing.length : p.missing_count;
  if (typeof missing === "number") lines.push(el("div", "", "labels missing: " + missing));
  tip.replaceChildren(...lines);
  tip.hidden = false;
  const section = $(".scores-sec").getBoundingClientRect();
  const box = target.getBoundingClientRect();
  const width = tip.offsetWidth, height = tip.offsetHeight;
  let left = box.left - section.left + box.width / 2 - width / 2;
  left = Math.max(0, Math.min(left, section.width - width));
  let top = box.top - section.top - height - 6;
  if (top < 0) top = box.bottom - section.top + 6;
  tip.style.left = left + "px";
  tip.style.top = top + "px";
}

function hideTip() { $("#tip").hidden = true; }

let chartWidth = 0;
function renderScores(force) {
  const rows = app.state.rows || [];
  if (!changed("rows", rows) && !force) return;
  $("#scores-empty").hidden = rows.length > 0;
  hideTip();
  chartWidth = $("#charts").clientWidth;
  const dom = domain(rows);
  $("#charts").replaceChildren(...rows.map((r) => chartCard(r, dom, chartWidth - 26)));
}

// ------------------------------------------------------------------ pictures
function fileUrl(path) {
  return "/files/" + path.split("/").map(encodeURIComponent).join("/") + "?token=" + encodeURIComponent(app.token);
}

function setImage(img, miss, file, alt, missing) {
  if (!file) {
    img.hidden = true;
    img.removeAttribute("src");
    miss.textContent = missing;
    miss.hidden = false;
    return;
  }
  const url = fileUrl(file.path);
  if (img.getAttribute("src") !== url) img.setAttribute("src", url);
  if (file.size) { img.width = file.size[0]; img.height = file.size[1]; }
  else { img.removeAttribute("width"); img.removeAttribute("height"); }
  img.alt = alt;
  img.hidden = false;
  miss.hidden = true;
}

function showPicture() {
  const pics = app.state.pictures;
  const i = app.step;
  const p = pics[i];
  $("#step").value = String(i);
  $("#prev").disabled = i === 0;
  $("#next").disabled = i === pics.length - 1;
  const what = p.kind ? (p.kind === "draft" ? " · draft by " : " · revision by ") + p.drawer : "";
  $("#step-label").textContent = "Round " + (p.round === null ? "?" : p.round) + (p.row ? " · row " + p.row : "") + what
    + (typeof p.match === "number" ? " · match " + fmt3(p.match) : "") + " · picture " + (i + 1) + " of " + pics.length;
  setImage($("#pic-ours"), $("#miss-ours"), p.ours, "Our slide after round " + p.round, "Not in the run folder: " + p.name);
  $("#cap-ours").textContent = "Ours after round " + p.round;
  const whole = p.original && /(^|\/)reference-small\.png$/.test(p.original.path);
  const originalName = whole || !p.row ? "The original" : "The original, row " + p.row;
  setImage($("#pic-original"), $("#miss-original"), p.original, originalName,
    "No crop of the original for this row yet: it is written when the art director first looks at the row.");
  $("#cap-original").textContent = originalName + (whole || !p.original ? "" : " (the part the art director compared)");
}

function renderPictures() {
  const pics = app.state.pictures || [];
  $("#pictures-empty").hidden = pics.length > 0;
  $("#pictures").hidden = !pics.length;
  if (!pics.length || !changed("pictures", pics)) return;
  $("#step").max = String(pics.length - 1);
  if (app.follow || app.step === null || app.step > pics.length - 1) app.step = pics.length - 1;
  showPicture();
}

function moveTo(i) {
  const pics = app.state && app.state.pictures;
  if (!pics || !pics.length) return;
  app.step = Math.max(0, Math.min(pics.length - 1, i));
  app.follow = app.step === pics.length - 1;
  showPicture();
}

// ------------------------------------------------------------------ lessons
function renderLessons() {
  const lessons = app.state.lessons || [];
  if (!changed("lessons", lessons)) return;
  $("#lessons-empty").hidden = lessons.length > 0;
  $("#lessons").replaceChildren(...lessons.map((l) => {
    const li = el("li");
    if (typeof l.count === "number") {
      const times = el("span", "times", l.count + "×");
      times.setAttribute("aria-label", l.count + (l.count === 1 ? " time" : " times"));
      li.append(times);
    }
    li.append(el("span", "", l.text));
    return li;
  }));
}

// ------------------------------------------------------------------ controls
function applyTheme(theme) {
  if (theme === "auto") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
  $("#theme").textContent = "Theme: " + theme;
}

let theme = "auto";
try { theme = localStorage.getItem(THEME_KEY) || "auto"; } catch (e) { /* storage blocked */ }
if (THEMES.indexOf(theme) < 0) theme = "auto";
applyTheme(theme);
$("#theme").addEventListener("click", () => {
  theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
  try { localStorage.setItem(THEME_KEY, theme); } catch (e) { /* storage blocked */ }
  applyTheme(theme);
});

$("#f-member").addEventListener("change", (ev) => { app.member = ev.target.value; if (app.state) renderTimeline(); });
for (const box of document.querySelectorAll('input[name="kind"]')) {
  box.addEventListener("change", () => {
    if (box.checked) app.kinds.add(box.value); else app.kinds.delete(box.value);
    if (app.state) renderTimeline();
  });
}
$("#timeline").addEventListener("scroll", () => {
  const box = $("#timeline");
  if (box.scrollHeight - box.scrollTop - box.clientHeight < 24) $("#latest").hidden = true;
});
$("#latest").addEventListener("click", () => {
  const box = $("#timeline");
  box.scrollTop = box.scrollHeight;
  $("#latest").hidden = true;
  box.focus();
});
$("#step").addEventListener("input", (ev) => moveTo(Number(ev.target.value)));
$("#prev").addEventListener("click", () => moveTo(app.step - 1));
$("#next").addEventListener("click", () => moveTo(app.step + 1));
for (const id of ["#pic-ours", "#pic-original"]) {
  $(id).addEventListener("error", (ev) => {
    const miss = ev.target.parentElement.querySelector(".note");
    miss.textContent = "The picture could not be loaded.";
    miss.hidden = false;
  });
}
if (window.ResizeObserver) {
  new ResizeObserver(() => {
    if (app.state && Math.abs($("#charts").clientWidth - chartWidth) > 4) renderScores(true);
  }).observe($("#charts"));
}

setInterval(tick, 1000);
connect();
