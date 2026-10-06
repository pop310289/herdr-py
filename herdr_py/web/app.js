// herdr-py web UI: plain JavaScript, no build step, no external scripts.
// Agent output is untrusted text: it only ever goes into the page through textContent.
"use strict";

const TOKEN_KEY = "herdr-py-token";

function readToken() {
  const match = location.hash.match(/token=([^&]+)/);
  if (match) {
    try { sessionStorage.setItem(TOKEN_KEY, decodeURIComponent(match[1])); } catch (e) { /* storage blocked */ }
    history.replaceState(null, "", location.pathname);  // keep the token out of the address bar
    return decodeURIComponent(match[1]);
  }
  try { return sessionStorage.getItem(TOKEN_KEY) || ""; } catch (e) { return ""; }
}

const token = readToken();
const $ = (sel) => document.querySelector(sel);
const STATE_LABEL = { starting: "starting", working: "working", retry: "retry", blocked: "needs you", idle: "idle", aborted: "aborted", error: "error" };
let agents = [];
let refreshTimer = null;
const openForms = new Set();

async function api(method, path, body) {
  const resp = await fetch(path, {
    method,
    headers: { "Authorization": "Bearer " + token, "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || ("HTTP " + resp.status));
  return data;
}

function toast(text) {
  const el = $("#toast");
  el.textContent = text;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.textContent = ""; }, 4000);
}

function scheduleRefresh() {
  if (refreshTimer) return;
  refreshTimer = setTimeout(async () => {
    refreshTimer = null;
    try { agents = (await api("GET", "/api/agents")).agents; render(); } catch (e) { setConn(false, e.message); }
  }, 250);
}

function setConn(ok, detail) {
  const el = $("#conn");
  el.textContent = ok ? "live" : ("offline" + (detail ? ": " + detail : ""));
  el.className = "conn " + (ok ? "ok" : "bad");
}

function fmtSeconds(s) {
  s = Math.round(s);
  return s < 60 ? s + "s" : Math.floor(s / 60) + "m" + String(s % 60).padStart(2, "0") + "s";
}

async function reply(id, answer) {
  try {
    await api("POST", "/api/permissions/" + encodeURIComponent(id), answer === "reject"
      ? { reply: "reject", message: "Rejected from the herdr-py web UI." } : { reply: answer });
    toast(answer === "reject" ? "Rejected" : "Allowed");
  } catch (e) { toast("Failed: " + e.message); }
  scheduleRefresh();
}

function renderNeeds() {
  const list = $("#needs-list");
  list.replaceChildren();
  let count = 0;
  for (const a of agents) {
    for (const p of a.pending) {
      count += 1;
      const li = document.createElement("li");
      const who = document.createElement("strong");
      who.textContent = a.name + (p.child ? " (subagent)" : "");
      const what = document.createElement("p");
      what.textContent = p.description;
      const row = document.createElement("div");
      row.className = "actions";
      const kinds = p.kind === "question" ? [["reject", "Dismiss", "danger"]]
        : [["once", "Allow once", "primary"], ["always", "Always", ""], ["reject", "Reject", "danger"]];
      for (const [answer, label, cls] of kinds) {
        const b = document.createElement("button");
        b.type = "button";
        b.textContent = label;
        if (cls) b.className = cls;
        b.addEventListener("click", () => { b.disabled = true; reply(p.id, answer); });
        row.append(b);
      }
      li.append(who, what, row);
      list.append(li);
    }
  }
  $("#needs").hidden = count === 0;
  $("#needs-count").textContent = count ? String(count) : "";
  document.title = count ? "(" + count + ") herdr-py" : "herdr-py";
}

function renderAgents() {
  const list = $("#agents");
  list.replaceChildren();
  $("#empty").hidden = agents.length > 0;
  for (const a of agents) {
    const li = $("#agent-tpl").content.firstElementChild.cloneNode(true);
    li.dataset.state = a.state;
    li.querySelector(".name").textContent = a.name;
    const chip = li.querySelector(".chip");
    chip.textContent = STATE_LABEL[a.state] || a.state;
    chip.dataset.state = a.state;
    li.querySelector(".meta").textContent = fmtSeconds(a.seconds_in_state) + " · " + a.tokens.toLocaleString() + " tok · turn " + a.turns
      + (a.followups_left ? " · " + a.followups_left + " follow-up(s) queued" : "") + (a.budget_s ? " · budget " + a.budget_s + "s" : "");
    const activity = li.querySelector(".activity");
    for (const item of a.activity.slice(-4)) {
      const row = document.createElement("li");
      row.textContent = item.text;
      if (item.tone) row.dataset.tone = item.tone;
      activity.append(row);
    }
    const stream = li.querySelector(".stream");
    stream.textContent = a.stream.text ? (a.stream.kind || "output") + ": …" + a.stream.text.slice(-220) : "";
    stream.hidden = !a.stream.text;
    const form = li.querySelector(".prompt-form");
    form.hidden = !openForms.has(a.name);
    li.querySelector(".act-prompt").addEventListener("click", () => {
      if (openForms.has(a.name)) openForms.delete(a.name); else openForms.add(a.name);
      form.hidden = !openForms.has(a.name);
      if (!form.hidden) form.querySelector("textarea").focus();
    });
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const text = form.querySelector("textarea").value.trim();
      if (!text) return;
      try { await api("POST", "/api/agents/" + encodeURIComponent(a.name) + "/prompt", { text }); openForms.delete(a.name); toast("Sent to " + a.name); }
      catch (e) { toast("Failed: " + e.message); }
      scheduleRefresh();
    });
    const abort = li.querySelector(".act-abort");
    abort.disabled = !["starting", "working", "retry", "blocked"].includes(a.state);
    abort.addEventListener("click", async () => {
      if (!confirm("Abort " + a.name + "?")) return;
      try { await api("POST", "/api/agents/" + encodeURIComponent(a.name) + "/abort", {}); toast("Aborted " + a.name); }
      catch (e) { toast("Failed: " + e.message); }
      scheduleRefresh();
    });
    list.append(li);
  }
}

function render() {
  // Keep typed-but-unsent prompts across re-renders.
  const drafts = {};
  document.querySelectorAll("#agents .agent").forEach((li) => {
    const name = li.querySelector(".name").textContent;
    const ta = li.querySelector(".prompt-form textarea");
    if (ta && ta.value) drafts[name] = ta.value;
  });
  renderNeeds();
  renderAgents();
  document.querySelectorAll("#agents .agent").forEach((li) => {
    const name = li.querySelector(".name").textContent;
    if (drafts[name]) li.querySelector(".prompt-form textarea").value = drafts[name];
  });
}

function connect() {
  if (!token) { setConn(false, "open the link printed by herdr-py serve (it carries the token)"); return; }
  const source = new EventSource("/api/events?token=" + encodeURIComponent(token));
  source.onopen = () => setConn(true);
  source.onerror = () => setConn(false, "reconnecting");
  source.onmessage = (msg) => {
    let event;
    try { event = JSON.parse(msg.data); } catch (e) { return; }
    if (event.type === "snapshot") { agents = event.agents; render(); setConn(true); return; }
    if (event.type === "events_lost") { scheduleRefresh(); return; }
    scheduleRefresh();
  };
}

$("#new-agent").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = new FormData(ev.target);
  const body = { name: f.get("name").trim(), prompt: f.get("prompt").trim() };
  if (f.get("budget_s")) body.budget_s = Number(f.get("budget_s"));
  const followups = String(f.get("followups") || "").split("\n").map((s) => s.trim()).filter(Boolean);
  if (followups.length) body.followups = followups;
  try { await api("POST", "/api/agents", body); ev.target.reset(); toast("Started " + body.name); }
  catch (e) { toast("Failed: " + e.message); }
  scheduleRefresh();
});

setInterval(() => { if (agents.length) render(); }, 5000);  // keep the "seconds in state" counters moving
connect();
