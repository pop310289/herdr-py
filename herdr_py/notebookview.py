"""The notebook (herdr_py/notebook.py) as an app: a rail on the left with every task (a page of the notebook) and a +
for a new one; the task you open fills the rest, in tabs: its team drawn as a tree (who plans, who works, who
judges, which skills each member wrote and used), its runs with each run's replay, what went wrong, its skills, its
knowledge drawn as a graph, every version of what it made, its notes and its history.

    python3 -m herdr_py.notebook NOTEBOOK view --out DIR [--runs DIR]...      pages to put on any web server
    python3 -m herdr_py.notebook NOTEBOOK serve [--port 8790] [--runs DIR]... the same pages, live, with buttons

The layout is for a computer's screen; a notebook can add its own style sheet (notebook.json "style": a CSS file,
relative to the notebook's folder), included after the built-in one in every page, for example a layout for phones.
The pages are complete without JavaScript: the tabs are links to sections of one page and every run's replay is a
page of its own. With JavaScript a tab shows its section alone, the replay switches between runs, and the buttons
work: served by `serve` a button sends its command (open a task, pick the current version, write a note, accept,
hold, reopen, approve, exclude or include an entry) with the token printed after # in the link, and the command is
refused without it; as plain files a button copies what to tell Claude instead. `serve` listens on this machine
only unless --host says otherwise, and serves the teams' own files in a sandbox (their scripts cannot reach the
token). Data is escaped, never written into a page as markup.
"""
import collections
import hmac
import http.server
import json
import math
import os
import re
import secrets
import socketserver
import sys
import time
import urllib.parse

from . import coopview, dagview, engineview
from .engineview import esc, fit
from .members import MemberError, parse_member
from .notebook import (LOOKED, S, Notebook, NotebookError, add_note, approve, command, day_of, find_entry, local,
                       need_text, one_line, pick, run_facts, say, score_text, span_text, tilde, tokens_text)
from .teamkb import TAG
from .viewstyle import TOKENS

V = {  # (English, 繁體中文); the shared words are notebook.S
    "home": ("Waiting for you", "待你處理"),
    "new_task": ("New task", "開新的 task"),
    "tabs": ("Overview|Team|Replay|Debug|Skills|Knowledge|Outputs|Notes", "總覽|架構|回放|除錯|skill|知識庫|成果|批註與歷史"),
    "menu": ("all tasks", "所有 task"),
    "stat_runs": ("runs", "次執行"), "stat_turns": ("member turns", "成員回合"), "stat_passed": ("entries passed", "條通過"),
    "stat_wait": ("waiting for you", "待你處理"),
    "agents": ("Agents", "Agent 清單"), "agents_hint": ("tap an agent for its role, todos, skills, output and every turn",
                                                    "點一個 Agent 看它的角色、待辦、skill、產出與每個回合"),
    "ag_working": ("working", "執行中"), "ag_waiting": ("waiting", "等待中"), "ag_passed": ("passed", "通過"),
    "ag_failed": ("did not pass", "沒過"), "ag_broke": ("broke", "出錯"), "ag_could_not": ("could not", "做不到"),
    "ag_no_turn": ("no turn", "沒輪到"), "ag_no_answer": ("no answer", "沒交答案"), "ag_planning": ("planning", "規劃中"),
    "ag_todos": ("{d}/{n} todos done", "待辦 {d}/{n}"),
    "ag_last": ("last: {text}", "最後：{text}"), "ag_now": ("now: {text}", "正在做：{text}"),
    "ag_planner": ("woken {n} times, sent back {b}", "喚醒 {n} 次，退回 {b} 次"),
    "ag_role": ("Role", "角色"), "ag_run": ("In run {n}", "第 {n} 次執行"),
    "ag_todos_h": ("Todos in run {n}", "第 {n} 次的待辦"), "ag_skills_h": ("Skills", "skill"),
    "ag_wrote": ("wrote: {x}", "寫了：{x}"), "ag_used": ("used: {x}", "用了：{x}"), "ag_none": ("none", "沒有"),
    "ag_out_h": ("What it made", "產出"), "ag_turns_h": ("Every turn", "每個回合"), "ag_wakes_h": ("Every wake", "每次喚醒"),
    "ag_wake_line": ("wake {w}: {a} todos added, {d} dropped", "第 {w} 次：加 {a} 個待辦、刪 {d} 個"),
    "ag_turn_line": ("run {r}, turn {k}: {what}", "第 {r} 次 · 回合 {k}：{what}"),
    "close": ("close", "關閉"),
    "refs": ("Reference material from other tasks", "參考資料（來自其他 task）"),
    "kinds_all": ("every verified entry", "全部通過的條目"),
    "raw_file": ("the file itself (UTF-8 plain text)", "原始檔（UTF-8 純文字）"),
    "refs_note": ("Not this task's verified results; their old scores do not apply here.", "不算這個 task 已驗證的成果，舊分數不適用。"),
    "refs_line": ("reference material: {items}", "參考資料：{items}"),
    "refs_item": ("{title}, run {n}: {k}", "{title} 第 {n} 次 {k} 條"),
    "picks_line": ("went on from the current versions: {ids}", "帶入現行版：{ids}"),
    "from_run": ("from {title}, run {n}", "來自 {title} 第 {n} 次"),
    "bring_h": ("Bring from earlier tasks (optional)", "從舊 task 帶過來（可不選）"),
    "bring_skills": ("skills", "skill"), "bring_knowledge": ("knowledge", "知識"), "bring_current": ("current versions", "現行版"),
    "bring_note": ("skills: that task's skills; knowledge: every verified entry of it (skills too); current versions: the ones "
                   "picked there. They come as reference material: the members can read them, but they are not the new task's results "
                   "and keep no score.",
                   "skill：那個 task 的 skill；知識：它全部通過的條目（含 skill）；現行版：那裡選定的版本。帶過去的只當參考資料："
                   "成員讀得到，但不算新 task 的成果，也不沿用舊分數。"),
    "kb_search": ("search the knowledge…", "搜尋知識庫…"), "kb_all": ("all", "全部"), "kb_graph": ("Knowledge graph", "知識圖"),
    "config": ("Run settings and the raw commands", "執行設定與原始指令"),
    "outputs_now": ("Outputs", "成果"), "details": ("Every agent's details", "每個 Agent 的詳情"),
    "requests": ("New tasks waiting for Claude to draft", "等 Claude 起草的新 task"),
    "request_line": ("{id}: {goal}", "{id}：{goal}"),
    "by_day": ("By day", "依日期"),
    "planner_role": ("hands out todos", "分派待辦"),
    "judge_role": ("judges every answer", "評分：通過的才進知識庫"), "judge_title": ("judge · {f}", "評分 · {f}"),
    "skill_line": ("skills: wrote {w} · used {u}", "skill：寫 {w} · 用 {u}"),
    "kb_box": ("{n} entries passed", "通過 {n} 條"),
    "team_runs": ("The team of each run", "每次執行的團隊"),
    "replay_of": ("Replay of run {n}", "第 {n} 次的回放"),
    "open_full": ("open it on its own", "全螢幕開啟"),
    "no_runs": ("No run yet.", "還沒有執行。"),
    "debug_none": ("Nothing went wrong in this run.", "這次沒有出錯。"),
    "sent_back": ("the planner's reply was sent back once|the planner's reply was sent back {n} times", "planner 的回覆被退回 {n} 次"),
    "wake_n": ("wake {w}", "第 {w} 次喚醒"),
    "bad_turns": ("1 member turn gave no answer|{n} member turns gave no answer", "{n} 個成員回合沒交出答案"),
    "turn_of": ("{m}, turn {k} ({state})", "{m} 第 {k} 回合（{state}）"),
    "invalid_n": ("1 answer did not pass the judge|{n} answers did not pass the judge", "{n} 個答案沒通過評分"),
    "failure_n": ("once a member said it could not|{n} times a member said it could not", "成員回報做不到 {n} 次"),
    "broken": ("the setup broke", "設定壞了"),
    "idle": ("free with nothing to take", "沒事做的時間"),
    "steps_bad": ("1 step failed or was blocked|{n} steps failed or were blocked", "{n} 步失敗或被擋下"),
    "step_cut": ("{s}: cut off (the run has no end)", "{s}：中斷（沒有結束紀錄）"),
    "retried": ("{s}: {k} attempts", "{s}：試了 {k} 次"),
    "skills_none": ("The teams of this task wrote no skill yet.", "這個 task 的團隊還沒寫 skill。"),
    "skill_made": ("written in run {n} by {m}", "第 {n} 次由 {m} 寫"),
    "skill_used": ("used by {who}", "{who} 用過"),
    "skill_unused": ("nobody used it yet", "還沒人用過"),
    "skill_carried": ("carried into runs {ns}", "帶入第 {ns} 次"),
    "graph_none": ("No knowledge yet.", "還沒有知識。"),
    "graph_tap": ("tap a circle: what it came from and what built on it light up", "點一個圓：它引用了誰、誰引用它，會亮起來"),
    "graph_legend": ("the number: the run that made it|passed|did not pass|not judged|current version|a line: built on (from the upper to the lower)",
                     "數字：第幾次執行做的|通過|沒通過|沒判定|現行版|線：引用（下面的引用上面的）"),
    "outputs_none": ("No output yet.", "還沒有成果。"),
    "act_pick": ("make it current", "設為現行版"),
    "act_accept": ("accept", "驗收"),
    "act_hold": ("put this task on hold", "擱置這個 task"),
    "act_reopen": ("reopen this task", "重新打開這個 task"),
    "act_approve": ("approve", "核准"),
    "act_exclude": ("leave out next time", "下次不帶"),
    "act_include": ("carry again", "取消排除"),
    "act_note": ("add the note", "加批註"),
    "act_new": ("send", "送出"),
    "note_ph": ("what the next run should do differently", "下一次要怎麼改"),
    "new_title": ("title (optional)", "標題（可空白）"),
    "new_goal": ("the goal, in a line", "一句話的目標"),
    "new_how": ("Claude drafts the task (team, judge, budget) from your goal; it runs only after you approve it.",
                "Claude 依你的目標起草（團隊、評分、預算），你核准後才會執行。"),
    "copied": ("copied: paste it to Claude", "已複製，貼到跟 Claude 的對話"),
    "sent": ("done", "完成"),
    "refused": ("refused: ", "沒做成："),
    "no_server": ("The server did not answer.", "伺服器沒有回應。"),
    "say_pick": ("{title}: make {entry} the current {kind}", "「{title}」：{kind} 選 {entry}"),
    "say_accept": ("{title}: accepted, nothing to change", "「{title}」：驗收，不用改"),
    "say_hold": ("{title}: put it on hold", "「{title}」：先擱置"),
    "say_reopen": ("{title}: reopen it", "「{title}」：重新打開"),
    "say_approve": ("{title}: approve version {d}", "「{title}」：核准版本 {d}"),
    "say_note": ("note for {title}: ", "「{title}」的批註："),
    "say_exclude": ("{title}: leave {entry} out next time", "「{title}」：下次不要帶 {entry}"),
    "say_include": ("{title}: carry {entry} again", "「{title}」：{entry} 取消排除"),
    "say_new": ("New task: ", "開新的 task："),
    "cli": ("or run", "或執行"),
    "static_note": ("These pages are files: a button copies what to tell Claude. `notebook serve` on the Mac gives pages whose buttons act.",
                    "這些頁面是檔案：按鈕會複製要跟 Claude 說的話；在 Mac 上用 notebook serve 打開的頁面，按鈕會直接執行。"),
    "life": ("you: a goal in a line|Claude drafts task, judge, team|you approve|the team runs (run n)|"
             "you review: pick, note, exclude|run n+1: your picks + what passed",
             "你：一句話的目標|Claude 起草：任務、評分、團隊、預算|你核准|團隊執行（第 n 次）|"
             "你驗收：選現行版、批註、排除|下次從現行版改起，帶入通過的條目與批註"),
}


def t(lang, key, **kw):
    pair = V.get(key)
    if not pair:
        return say(lang, key, **kw)
    text = pair[1 if lang == "zh-TW" else 0]
    if "|" in text and "{" in text:  # "one|many": the first when n is 1
        one, many = text.split("|", 1)
        text = one if kw.get("n") == 1 else many
    return text.format(**kw)


STYLE = TOKENS + """:root { --fs-1:.75rem; --fs-2:.85rem; --fs-3:.95rem; --fs-4:1.05rem; --fs-5:1.3rem; --r1:8px; --r2:12px; --tb:0px; }
* { box-sizing:border-box; }
html, body { margin:0; background:var(--bg); color:var(--ink); }
body { font:15px/1.55 -apple-system,"SF Pro Text","PingFang TC","Noto Sans TC",sans-serif; -webkit-font-smoothing:antialiased; }
a { color:var(--ice); text-decoration:none; } a:hover { text-decoration:underline; }
.app { display:grid; grid-template-columns:252px minmax(0,1fr); min-height:100vh; }
.rail { display:flex; position:sticky; top:0; align-self:start; height:100vh; overflow-y:auto; background:var(--inset); border-right:1px solid var(--line);
  padding:calc(12px + env(safe-area-inset-top, 0px)) 10px calc(16px + env(safe-area-inset-bottom, 0px)); flex-direction:column; gap:6px; }
.topbar { position:sticky; top:0; z-index:6; display:none; align-items:center; gap:10px; min-height:var(--tb);
  padding:calc(8px + env(safe-area-inset-top, 0px)) 12px 8px; background:rgba(9,10,15,.96); border-bottom:1px solid var(--line); }
.menu-btn { flex:none; width:38px; height:38px; display:grid; place-items:center; border:1px solid var(--line); border-radius:var(--r1);
  color:var(--ink); background:var(--panel); font-size:18px; }
.menu-btn:hover { text-decoration:none; }
.tb-title { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-weight:600; font-size:var(--fs-3); }
.ri { display:flex; align-items:center; gap:10px; color:var(--ink); border-radius:12px; min-width:0; }
.ri:hover { text-decoration:none; background:var(--panel); }
.av { position:relative; flex:none; width:40px; height:40px; border-radius:11px; display:grid; place-items:center; background:var(--panel);
  border:1px solid var(--line); font-weight:650; font-size:16px; color:var(--ink); box-shadow:inset 0 1px 0 var(--hi); }
.ri.on .av { border-color:var(--ice); background:var(--ice-bg); }
.av .dot { position:absolute; top:-3px; right:-3px; width:10px; height:10px; border-radius:50%; border:2px solid var(--inset); background:var(--faint); }
.dot.warn { background:var(--amber); } .dot.ok { background:var(--ok); } .dot.bad { background:var(--bad); }
.dot.run { background:var(--ice); animation:breathe 1.6s ease-in-out infinite; }
.av .cnt { position:absolute; bottom:-5px; right:-6px; min-width:18px; height:18px; padding:0 4px; border-radius:9px; background:var(--amber);
  color:var(--chip-ink); font-size:10.5px; font-weight:700; display:grid; place-items:center; border:2px solid var(--inset); }
.ri.new .av { border-style:dashed; color:var(--muted); font-size:22px; font-weight:300; box-shadow:none; }
.rd { font-size:9.5px; color:var(--faint); text-align:center; margin-top:6px; font-variant-numeric:tabular-nums; }
.rl { display:block; min-width:0; }
.rl b { display:block; font-weight:600; font-size:var(--fs-2); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.rl small { display:block; color:var(--muted); font-size:var(--fs-1); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.rd { text-align:left; padding-left:4px; font-size:10.5px; }
dialog { border:1px solid var(--line); background:var(--panel); color:var(--ink); padding:0; box-shadow:0 10px 40px rgba(0,0,0,.7); }
dialog::backdrop { background:rgba(0,0,0,.55); }
.sheet { position:fixed; inset:0 0 0 auto; margin:0; height:100%; max-height:100%; width:440px; max-width:100%; border-radius:16px 0 0 16px; overflow:auto; }
.drawer { position:fixed; inset:0 auto 0 0; margin:0; height:100%; max-height:100%; width:min(300px, 86vw); border-radius:0 16px 16px 0;
  overflow:auto; background:var(--inset); }
.drawer .list { display:flex; flex-direction:column; gap:6px; padding:calc(14px + env(safe-area-inset-top, 0px)) 12px 20px; }
.sheet-head { position:sticky; top:0; display:flex; justify-content:flex-end; padding:8px 10px 0; background:var(--panel); }
.close { font:inherit; font-size:var(--fs-2); background:none; border:1px solid var(--line); color:var(--muted); border-radius:999px; padding:3px 12px; cursor:pointer; }
.sheet-body { padding:4px 16px 20px; }
.stats { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:8px; }
.stat { background:var(--inset); border:1px solid var(--line); border-radius:var(--r2); padding:10px 12px; min-width:0; }
.stat b { display:block; font-size:var(--fs-5); font-weight:650; font-variant-numeric:tabular-nums; } .stat span { font-size:var(--fs-1); color:var(--muted); }
.stat.warn b { color:#E9C27A; } .stat.good b { color:#7FD8B6; }
.agents { list-style:none; margin:0; padding:0; display:flex; flex-direction:column; gap:8px; }
.agent { display:grid; grid-template-columns:40px minmax(0,1fr) auto; gap:2px 12px; align-items:center; padding:10px 12px; background:var(--inset);
  border:1px solid var(--line); border-radius:var(--r2); color:var(--ink); min-width:0; }
.agent:hover { text-decoration:none; border-color:var(--wire); }
.ag-main { min-width:0; display:flex; flex-direction:column; gap:1px; }
.ag-main b { font-size:var(--fs-3); } .ag-main small { color:var(--muted); font-size:var(--fs-1); font-weight:400; margin-left:6px; }
.ag-sub, .ag-now { font-size:var(--fs-2); color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.ag-now { color:var(--faint); font-size:var(--fs-1); }
.ag-side { display:flex; flex-direction:column; align-items:flex-end; gap:4px; }
.badge { font-size:var(--fs-1); border-radius:999px; padding:1px 9px; border:1px solid var(--line); color:var(--muted); white-space:nowrap; }
.badge::before { content:""; display:inline-block; width:6px; height:6px; border-radius:50%; background:currentColor; margin-right:6px; vertical-align:1px; }
.badge.ok { color:#7FD8B6; border-color:rgba(16,185,129,.35); } .badge.bad { color:#F3A6A6; border-color:rgba(239,68,68,.35); }
.badge.warn { color:#E9C27A; border-color:rgba(212,162,76,.4); } .badge.run { color:var(--ice); border-color:rgba(147,197,253,.35); }
.ag-count { font-size:var(--fs-1); color:var(--faint); font-variant-numeric:tabular-nums; white-space:nowrap; }
.js .agent-details { display:none; }
.agent-detail { padding-top:6px; }
.ag-head { display:flex; align-items:center; gap:12px; } .ag-head h3 { font-size:var(--fs-4); }
.agent-detail h4, .sheet-body h4 { font-size:var(--fs-1); color:var(--muted); letter-spacing:.04em; margin:16px 0 6px; font-weight:600; }
.agent-detail p, .sheet-body p { margin:0; font-size:var(--fs-3); overflow-wrap:anywhere; }
.kb-tools { display:none; flex-direction:column; gap:8px; margin-bottom:12px; } .js .kb-tools { display:flex; }
.kb-search { font:inherit; font-size:16px; background:var(--bg); color:var(--ink); border:1px solid var(--line); border-radius:var(--r1); padding:8px 10px; width:100%; }
.kb-kinds { display:flex; gap:6px; overflow-x:auto; scrollbar-width:none; }
.kb-kinds button { flex:none; font:inherit; font-size:var(--fs-2); color:var(--muted); background:var(--inset); border:1px solid var(--line);
  border-radius:var(--r1); padding:4px 10px; cursor:pointer; font-variant-numeric:tabular-nums; }
.kb-kinds button.on { color:var(--ink); border-color:var(--ice); background:var(--ice-bg); }
.kb-list { list-style:none; margin:0; padding:0; display:flex; flex-direction:column; gap:8px; }
.kb-list li { background:var(--inset); border:1px solid var(--line); border-radius:var(--r2); padding:10px 12px; display:flex; flex-direction:column; gap:4px; min-width:0; overflow-wrap:anywhere; }
.kb-list li[hidden] { display:none; }
.kbadge { font-size:var(--fs-1); color:var(--ice); border:1px solid rgba(147,197,253,.35); border-radius:6px; padding:0 6px; }
.main { min-width:0; padding:20px 28px 48px; display:flex; flex-direction:column; gap:12px; max-width:1120px; }
.panel { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:14px; min-width:0;
  box-shadow:inset 0 1px 0 var(--hi), 0 10px 30px rgba(0,0,0,.6); scroll-margin-top:calc(var(--tb) + 56px); }
.top { display:flex; flex-direction:column; gap:6px; }
.brand { font-size:.72rem; letter-spacing:.08em; text-transform:uppercase; color:var(--faint); }
h1 { font-size:1.25rem; margin:0; overflow-wrap:anywhere; font-weight:650; text-wrap:balance; }
h2 { font-size:var(--fs-1); margin:0 0 10px; letter-spacing:.06em; color:var(--muted); font-weight:600; }
h3 { font-size:var(--fs-3); margin:0; font-weight:620; overflow-wrap:anywhere; }
.muted, .state { color:var(--muted); font-size:var(--fs-2); overflow-wrap:anywhere; }
.faint { color:var(--muted); opacity:.8; font-size:var(--fs-1); overflow-wrap:anywhere; }
.chips { display:flex; flex-wrap:wrap; gap:6px; }
.chip { font-size:.76rem; color:var(--muted); background:var(--inset); border:1px solid var(--line); border-radius:999px;
  padding:1px 9px; overflow-wrap:anywhere; font-variant-numeric:tabular-nums; }
.chip.good { color:#7FD8B6; border-color:rgba(16,185,129,.35); } .chip.bad { color:#F3A6A6; border-color:rgba(239,68,68,.35); }
.chip.warn { color:#E9C27A; border-color:rgba(212,162,76,.4); } .chip.ice { color:var(--ice); border-color:rgba(147,197,253,.35); }
a.chip { color:var(--ice); }
@keyframes breathe { 50% { opacity:.3; } }
.inbox { border-color:rgba(212,162,76,.35); }
.items { list-style:none; margin:0; padding:0; display:flex; flex-direction:column; gap:10px; min-width:0; }
.items > li { background:var(--inset); border:1px solid var(--line); border-radius:10px; padding:10px 12px; overflow-wrap:anywhere;
  display:flex; flex-direction:column; gap:5px; min-width:0; }
.items > li > * { min-width:0; max-width:100%; }
.row { display:flex; flex-wrap:wrap; align-items:baseline; gap:6px 10px; }
.cmd { font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace; color:var(--muted); background:var(--bg); border:1px solid var(--line);
  border-radius:6px; padding:5px 8px; white-space:pre-wrap; overflow-wrap:anywhere; max-width:100%; margin:4px 0 0; }
.tabs { position:sticky; top:var(--tb); z-index:3; display:flex; gap:4px; overflow-x:auto; padding-block:8px; background:rgba(9,10,15,.95);
  border-bottom:1px solid var(--line); scrollbar-width:none; margin-inline:-2px; }
.tabs::-webkit-scrollbar { display:none; }
.tabs a { flex:none; font-size:.88rem; color:var(--muted); padding:5px 12px; border-radius:999px; border:1px solid transparent; }
.tabs a:hover { text-decoration:none; color:var(--ink); }
.tabs a.on { color:var(--ink); border-color:var(--line); background:var(--panel); }
.js .tab { display:none; } .js .tab.on { display:block; }
.roles { list-style:none; margin:10px 0 0; padding:0; font-size:.84rem; display:flex; flex-direction:column; gap:4px; }
.roles li { overflow-wrap:anywhere; } .roles b { font-weight:600; }
.kv { display:grid; grid-template-columns:minmax(5.5em,max-content) minmax(0,1fr); gap:4px 12px; margin:0; font-size:.88rem; }
.kv dt { color:var(--faint); } .kv dd { margin:0; overflow-wrap:anywhere; }
pre.task { font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace; color:var(--muted); background:var(--inset);
  border:1px solid var(--line); border-radius:8px; padding:8px 10px; max-height:16em; overflow:auto; white-space:pre-wrap; margin:8px 0 0; }
.grid2 { display:grid; gap:12px; }
@media (min-width:1000px) { .grid2 { grid-template-columns:minmax(0,1fr) minmax(0,1fr); align-items:start; } }
.fig { overflow-x:auto; } .fig svg { display:block; margin:0 auto; width:100%; height:auto; } .fig.alt { display:none; }
.box { fill:var(--inset); stroke:var(--wire); stroke-width:1; } .box.me { fill:var(--amber-bg); stroke:var(--amber); }
.box.team { fill:var(--ice-bg); stroke:var(--ice); } .box.ok { fill:var(--ok-bg); stroke:var(--ok); }
.box.bad { fill:var(--bad-bg); stroke:var(--bad); } .box.dash { stroke-dasharray:4 3; }
.tx { fill:var(--ink); font-size:12px; } .tx.b { font-weight:600; } .tx.s { fill:var(--muted); font-size:10.5px; }
.tx.k { fill:var(--amber); font-size:10px; }
svg a { cursor:pointer; } svg a:hover rect.box { stroke:var(--ice); }
.ar { fill:none; stroke:var(--wire); stroke-width:1.3; } .ar.back { stroke-dasharray:5 4; } .ar.carry { stroke:var(--ok); }
.ah { fill:var(--wire); } .ah.carry { fill:var(--ok); } .al { fill:var(--muted); font-size:10.5px; }
.kg .e { fill:none; stroke:var(--wire); stroke-width:1; opacity:.55; }
.kg .n { stroke-width:1.2; } .kg .n.pass { fill:var(--ok-bg); stroke:var(--ok); } .kg .n.fail { fill:var(--bad-bg); stroke:var(--bad); }
.kg .n.none { fill:var(--inset); stroke:var(--faint); stroke-dasharray:2 2; } .kg .ring { fill:none; stroke:var(--amber); stroke-width:2; }
.kg .num { fill:var(--ink); font-size:9px; text-anchor:middle; pointer-events:none; } .kg .kl { fill:var(--muted); font-size:10.5px; }
.kg .g { cursor:pointer; } .kg.lit .e { opacity:.08; } .kg.lit .g { opacity:.25; } .kg.lit .e.on { opacity:1; stroke:var(--ice); stroke-width:1.6; }
.kg.lit .g.on { opacity:1; } .kg.lit .g.me .n { stroke:var(--ice); stroke-width:2.4; }
.tip { margin-top:8px; font-size:.84rem; color:var(--muted); overflow-wrap:anywhere; min-height:1.2em; }
.legend { display:flex; flex-wrap:wrap; gap:4px 14px; font-size:.76rem; color:var(--muted); margin-top:8px; }
.legend i { display:inline-block; width:12px; height:9px; border-radius:3px; border:1px solid var(--faint); margin-right:5px; vertical-align:-1px; }
.legend i.me { background:var(--amber-bg); border-color:var(--amber); } .legend i.team { background:var(--ice-bg); border-color:var(--ice); }
.legend i.ok { background:var(--ok-bg); border-color:var(--ok); } .legend i.bad { background:var(--bad-bg); border-color:var(--bad); }
.legend i.none { border-style:dashed; } .legend i.ring { border:2px solid var(--amber); border-radius:50%; }
.legend i.carry { border:0; border-top:2px solid var(--ok); height:0; border-radius:0; vertical-align:3px; }
.vers { list-style:none; margin:6px 0 0; padding:0; display:flex; flex-direction:column; gap:4px; font-size:.86rem; }
.vers li { display:flex; flex-wrap:wrap; gap:4px 10px; align-items:baseline; padding:5px 8px; border-radius:8px; border:1px solid transparent; }
.vers li.cur { border-color:rgba(16,185,129,.4); background:var(--ok-bg); }
.mono { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.8rem; }
.hist { list-style:none; margin:0; padding:0; font-size:.84rem; }
.hist li { padding:5px 0; border-top:1px solid var(--line); overflow-wrap:anywhere; } .hist li:first-child { border-top:0; }
.bad-line { color:#F3A6A6; } .warn-line { color:#E9C27A; }
details summary { cursor:pointer; color:var(--muted); font-size:.86rem; overflow-wrap:anywhere; }
iframe.replay { width:100%; height:78vh; min-height:520px; border:1px solid var(--line); border-radius:10px; background:var(--bg); display:block; }
.act { display:none; font:inherit; font-size:.8rem; padding:3px 11px; border-radius:999px; border:1px solid var(--line); background:var(--panel);
  color:var(--ink); box-shadow:inset 0 1px 0 var(--hi); cursor:pointer; }
.js .act { display:inline-block; } .js-only { display:none; } .js .js-only { display:inline; } .act.go { border-color:rgba(16,185,129,.5); color:#7FD8B6; } .act.warn { border-color:rgba(212,162,76,.5); color:#E9C27A; }
.said { font-size:.8rem; color:var(--muted); }
.form { display:flex; flex-direction:column; gap:10px; }
.form label { display:flex; flex-direction:column; gap:4px; font-size:.84rem; color:var(--muted); }
.form input, .form textarea { font:inherit; font-size:16px; background:var(--bg); color:var(--ink); border:1px solid var(--line);
  border-radius:8px; padding:8px 10px; width:100%; }
.form textarea { min-height:5em; resize:vertical; }
.bring { border:1px solid var(--line); border-radius:var(--r2); padding:8px 12px; margin:0; min-width:0; }
.bring legend { font-size:var(--fs-2); color:var(--muted); padding:0 6px; }
.bring li { gap:4px 18px; align-items:center; } .bring li b { flex-basis:100%; }
.bring label { display:inline-flex; flex-direction:row; align-items:center; gap:6px; min-height:32px; }
.bring input[type=checkbox] { width:auto; accent-color:var(--ice); }
.dag svg { width:100% !important; max-width:100% !important; height:auto !important; }
.dag .box { fill:none; stroke:none; } .dag .box rect { fill:var(--inset); stroke:var(--wire); stroke-width:1; }
.dag .box.passed rect { fill:var(--ok-bg); stroke:var(--ok); } .dag .box.failed rect { fill:var(--bad-bg); stroke:var(--bad); }
.dag .box.running rect, .dag .box.judging rect { fill:var(--ice-bg); stroke:var(--ice); } .dag .box.blocked rect { stroke-dasharray:5 4; }
.dag .box text { fill:var(--ink); font-size:13px; } .dag .box text.id { font-weight:600; font-size:14px; }
.dag .box text.sub { font-size:11px; fill:var(--muted); } .dag .edge { fill:none; stroke:var(--wire); stroke-width:1.2; stroke-dasharray:4 4; }
.dag .edge.done { stroke:var(--ok); stroke-dasharray:none; } .dag .head { fill:var(--muted); }
@media (prefers-reduced-motion:reduce) { .dot.run { animation:none; } }
"""

APP = """<!doctype html>
<html lang="{lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{title}</title>
<style>
{style}</style>{extra}</head>
<body><div class="app">
{rail}
<div class="col"><header class="topbar"><a class="menu-btn" href="{root}index.html" data-menu aria-label="{menu}">☰</a>
<span class="tb-title">{title}</span></header>
<main class="main">
{main}
</main></div></div>
<dialog class="drawer" id="menu-panel" aria-label="{menu}"><div class="list">{items}</div></dialog>
<dialog class="sheet" id="agent-panel"><div class="sheet-head"><button type="button" class="close" data-close>{close}</button></div>
<div class="sheet-body"></div></dialog>
<script type="application/json" id="nb-data">{data}</script>
<script>{script}</script>
</body></html>
"""

SCRIPT = r"""(function () {
  "use strict";
  document.documentElement.classList.add("js");
  var D = {};
  try { D = JSON.parse(document.getElementById("nb-data").textContent); } catch (e) { D = {}; }
  var token = null;
  try {
    var m = (location.hash || "").match(/token=([A-Za-z0-9_-]+)/);
    if (m) { sessionStorage.setItem("nb-token", m[1]); history.replaceState(null, "", location.pathname + location.search); }
    token = sessionStorage.getItem("nb-token");
  } catch (e) { token = null; }
  function $(s, r) { return (r || document).querySelector(s); }
  function all(s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); }
  var tabs = all(".tabs a[data-tab]"), sections = all("section.tab");
  function show(id) {
    if (!sections.some(function (s) { return s.id === id; })) { id = sections.length ? sections[0].id : null; }
    sections.forEach(function (s) { s.classList.toggle("on", s.id === id); });
    tabs.forEach(function (a) { a.classList.toggle("on", a.getAttribute("data-tab") === id); });
  }
  if (sections.length) {
    show((location.hash || "").replace("#", ""));
    tabs.forEach(function (a) {
      a.addEventListener("click", function (ev) {
        ev.preventDefault();
        var id = a.getAttribute("data-tab");
        show(id);
        try { history.replaceState(null, "", "#" + id); } catch (e) { /* a sandboxed view */ }
        window.scrollTo(0, 0);
      });
    });
    window.addEventListener("hashchange", function () { show(location.hash.replace("#", "")); });
  }
  var frame = $("iframe.replay");
  all("a[data-run]").forEach(function (a) {
    a.addEventListener("click", function (ev) {
      if (!frame) { return; }
      ev.preventDefault();
      frame.src = a.getAttribute("href");
      all("a[data-run]").forEach(function (b) { b.classList.toggle("ice", b === a); });
      var cap = $("#replay-of"); if (cap) { cap.textContent = a.getAttribute("data-label") || ""; }
    });
  });
  all("svg.kg").forEach(function (svg) {
    var nodes = all(".g", svg), edges = all(".e", svg), tip = svg.parentNode.parentNode.querySelector(".tip");
    var parents = {}, children = {};
    nodes.forEach(function (g) { var id = g.getAttribute("data-id"); parents[id] = (g.getAttribute("data-p") || "").split(" ").filter(Boolean); });
    Object.keys(parents).forEach(function (id) { parents[id].forEach(function (p) { (children[p] = children[p] || []).push(id); }); });
    function walk(id, next, seen) { (next[id] || []).forEach(function (x) { if (!seen[x]) { seen[x] = 1; walk(x, next, seen); } }); return seen; }
    var current = null;
    nodes.forEach(function (g) {
      g.addEventListener("click", function () {
        var id = g.getAttribute("data-id");
        if (current === id) { current = null; svg.classList.remove("lit"); if (tip) { tip.textContent = ""; } return; }
        current = id;
        var lit = walk(id, parents, {}); var down = walk(id, children, {}); lit[id] = 1;
        Object.keys(down).forEach(function (k) { lit[k] = 1; });
        svg.classList.add("lit");
        nodes.forEach(function (n) { var k = n.getAttribute("data-id"); n.classList.toggle("on", !!lit[k]); n.classList.toggle("me", k === id); });
        edges.forEach(function (e) { e.classList.toggle("on", !!lit[e.getAttribute("data-a")] && !!lit[e.getAttribute("data-b")]); });
        if (tip) { tip.textContent = g.getAttribute("data-tip"); }
      });
    });
  });
  function modal(d) { if (d && d.showModal) { try { d.showModal(); return true; } catch (e) { return false; } } return false; }
  all("dialog").forEach(function (d) {
    d.addEventListener("click", function (ev) { if (ev.target === d || (ev.target.closest && ev.target.closest("[data-close]"))) { d.close(); } });
  });
  var menu = $("#menu-panel");
  all("[data-menu]").forEach(function (a) {
    a.addEventListener("click", function (ev) { if (modal(menu)) { ev.preventDefault(); } });
  });
  var panel = $("#agent-panel");
  document.addEventListener("click", function (ev) {
    var a = ev.target.closest ? ev.target.closest("[data-agent]") : null;
    if (!a || !panel) { return; }
    var src = document.getElementById("agent-" + a.getAttribute("data-agent"));
    if (!src) { return; }
    ev.preventDefault();
    panel.querySelector(".sheet-body").innerHTML = src.innerHTML;
    if (!modal(panel)) { src.scrollIntoView(); }
  });
  all(".kb").forEach(function (box) {
    var input = box.querySelector(".kb-search"), kinds = all(".kb-kinds button", box), items = all(".kb-list li", box), kind = "";
    function filter() {
      var q = (input && input.value || "").trim().toLowerCase();
      items.forEach(function (li) {
        var ok = (!kind || li.getAttribute("data-kind") === kind) && (!q || (li.getAttribute("data-text") || "").indexOf(q) >= 0);
        li.hidden = !ok;
      });
    }
    if (input) { input.addEventListener("input", filter); }
    kinds.forEach(function (b) {
      b.addEventListener("click", function () {
        kind = b.getAttribute("data-kind") || "";
        kinds.forEach(function (x) { x.classList.toggle("on", x === b); });
        filter();
      });
    });
  });
  function tell(el, text) {
    var said = el.closest("[data-said]") ? el.closest("[data-said]").querySelector(".said") : null;
    said = said || (el.parentNode && el.parentNode.querySelector(".said"));
    if (said) { said.textContent = text; }
  }
  function copy(el, text) {
    function done() { tell(el, D.words.copied + " · " + text); }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () { tell(el, text); });
    } else { tell(el, text); }
  }
  function send(el, path, body) {
    tell(el, "…");
    fetch(path, { method: "POST", headers: { "Content-Type": "application/json", "Authorization": "Bearer " + token },
                  body: JSON.stringify(body) })
      .then(function (r) { return r.json().then(function (j) {
        if (r.ok) { tell(el, D.words.sent); window.setTimeout(function () { location.reload(); }, 400); }
        else { tell(el, D.words.refused + (j.error || r.status)); } }); })
      .catch(function () { tell(el, D.words.no_server); });
  }
  function field(el, name) {
    var form = el.closest("form");
    var f = form ? form.querySelector("[name=" + name + "]") : null;
    return f ? f.value.trim() : "";
  }
  document.addEventListener("click", function (ev) {
    var el = ev.target.closest ? ev.target.closest("button.act") : null;
    if (!el) { return; }
    ev.preventDefault();
    var act = el.getAttribute("data-act"), body = { command: act, page: el.getAttribute("data-page") || D.page,
      entry: el.getAttribute("data-entry") || null, digest: el.getAttribute("data-digest") || null };
    var phrase = el.getAttribute("data-say") || "";
    if (act === "note") { body.text = field(el, "text"); if (!body.text) { return; } phrase += body.text; }
    if (act === "new") { body.goal = field(el, "goal"); body.title = field(el, "title"); if (!body.goal) { return; }
      var form = el.closest("form"), picked = {};
      all("input[name=bring]:checked", form || document).forEach(function (c) {
        var v = c.value.split(":"); (picked[v[0]] = picked[v[0]] || []).push(v[1]); });
      body.bring = Object.keys(picked).map(function (k) { return { page: k, bring: picked[k] }; });
      var said = body.bring.map(function (b) { return b.page + ": " + b.bring.join(", "); }).join("; ");
      phrase += body.goal + (body.title ? " (" + body.title + ")" : "") + (said ? (D.words.bring || " ") + said : ""); }
    if (D.live && token) { send(el, act === "new" ? "api/new" : "api/act", body); } else { copy(el, phrase); }
  });
})();"""

STATE_DOT = {"draft": "warn", "review": "warn", "running": "run", "stuck": "bad", "approved": "ok", "reviewed": "ok", "done": "ok",
             "hold": ""}
STATE_CHIP = {"draft": "warn", "review": "warn", "running": "ice", "stuck": "bad", "approved": "good", "reviewed": "good",
              "done": "good", "hold": ""}


# ---- small helpers
def day_label(day, lang):
    tm = time.strptime(day, "%Y-%m-%d")
    wd = say(lang, "weekdays").split()[tm.tm_wday]
    return f"{day}（{wd}）" if lang == "zh-TW" else f"{wd} {day}"


def letter(page):
    """What a task shows on the rail: its icon (page.json "icon"), or the first letter of its title (digits and marks
    skipped, in upper case; a CJK character as it is)."""
    if page.d.get("icon"):
        return str(page.d["icon"])[:3]
    title = str(page.d.get("title") or page.id)
    return next((c for c in title if c.isalpha()), title[:1] or "?").upper()


def who(spec, lang):
    """A member as people read it: its name and backend (a program, for command members), and its model."""
    try:
        m = parse_member(str(spec))
    except MemberError:
        return str(spec)
    backend = ("程式" if lang == "zh-TW" else "program") if m["backend"] == "command" else m["backend"]
    return f"{m['name']} · {backend}" + (f":{m['model']}" if m.get("model") else "")


def kinds_text(entries, lang):
    c = collections.Counter(e["kind"] for e in entries)
    sep = "、" if lang == "zh-TW" else ", "
    return sep.join(f"{k} {n}" for k, n in sorted(c.items(), key=lambda kv: (-kv[1], kv[0])))


def steps_text(lang, f):
    """A DAG run's steps in order with their states; a step left working by a run that has no end was cut off."""
    out = []
    for x in f.get("steps") or []:
        st = (f.get("step_states") or {}).get(x, "?")
        if st in ("running", "judging") and f["state"] != "running":
            st = "cut"
        out.append(f"{x} ({say(lang, 'step_' + st) if 'step_' + st in S else st})")
    return " → ".join(out)


def cost_text(lang, tokens, turns, seconds, ended=True):
    """What runs cost, saying only what was recorded: no tokens when nobody counted them, no time without an end."""
    parts = [say(lang, "tokens", n=tokens_text(tokens))] if tokens else []
    parts.append(say(lang, "turns", n=turns))
    parts.append(span_text(seconds, lang) if ended and seconds else say(lang, "no_end") if not ended else "")
    return " · ".join(x for x in parts if x)


def best_of(versions):
    return max(versions, key=lambda e: (e["score"], e.get("t") or 0), default=None)


def made_by_kind(page, facts):
    """[(kind, the best verified entry of that kind)] a run made, for the page's outputs (or every kind it made)."""
    made = [e for e in facts.get("made") or [] if e["status"] == "valid"]
    kinds = page.d.get("outputs") or sorted({e["kind"] for e in made})
    return [(k, b) for k, b in ((k, best_of([e for e in made if e["kind"] == k])) for k in kinds) if b]


def made_text(page, facts_list, lang, who=True):
    best = {}
    for f in facts_list:
        for k, e in made_by_kind(page, f):
            if k not in best or (e["score"], e.get("t") or 0) > (best[k]["score"], best[k].get("t") or 0):
                best[k] = e
    sep = "、" if lang == "zh-TW" else ", "
    return sep.join(f"{k} {score_text(e['score'])}" + (f" ({e['member']})" if who else "") for k, e in best.items())


def outputs_of(page):
    """{kind: [versions, newest first]} for the page's output kinds (or every kind of result made, when none is set)."""
    entries = [e for e in page.entries().values() if e["status"] == "valid"]
    kinds = page.d.get("outputs") or sorted({e["kind"] for e in entries})
    return {k: sorted((e for e in entries if e["kind"] == k), key=lambda e: -(e.get("t") or 0)) for k in kinds}


def artifact_body(entry):
    """(text, extension) of an entry's artifact for people to open: its kind line taken off, HTML kept as a page."""
    src = os.path.join(entry["folder"], "kb", entry["artifact"]) if entry.get("artifact") else None
    if not src or not os.path.isfile(src):
        return None, None
    with open(src, "rb") as handle:
        text = handle.read().decode("utf-8", "replace")
    first, _, rest = text.partition("\n")
    if TAG.match(first):
        text = rest
    head = text.lstrip()[:200].lower()
    ext = ".html" if head.startswith(("<!doctype html", "<html")) else (".svg" if head.startswith("<svg") else ".txt")
    return text, ext


def open_name(eid, ext):
    """The file a person opens for an entry. A text file opens as a page that says it is UTF-8: a static server such as
    python -m http.server sends .txt as text/plain with no charset, and Safari on a phone set to Traditional Chinese
    then read a skill as Big5."""
    if not ext:
        return None
    return eid + (".html" if ext == ".txt" else ext)


READER = TOKENS + """html, body { margin:0; background:var(--bg); color:var(--ink); }
body { font:15px/1.7 -apple-system, BlinkMacSystemFont, "PingFang TC", "Noto Sans TC", "Segoe UI", sans-serif;
  padding:calc(14px + env(safe-area-inset-top, 0px)) 16px calc(24px + env(safe-area-inset-bottom, 0px)); }
main { max-width:78ch; margin:0 auto; display:flex; flex-direction:column; gap:12px; }
a { color:var(--ice); text-decoration:none; } a:hover { text-decoration:underline; }
.top, .foot { font-size:.85rem; color:var(--muted); }
h1 { margin:0; font-size:1.2rem; font-weight:650; line-height:1.4; text-wrap:balance; overflow-wrap:anywhere; }
.meta { display:flex; flex-wrap:wrap; align-items:center; gap:6px 10px; font-size:.8rem; color:var(--muted); }
.kbadge { color:var(--ice); border:1px solid rgba(147,197,253,.35); border-radius:6px; padding:0 6px; }
.mono { font-family:ui-monospace, SFMono-Regular, Menlo, monospace; }
pre { margin:0; padding:14px 16px; background:var(--panel); border:1px solid var(--line); border-radius:12px;
  white-space:pre-wrap; overflow-wrap:anywhere; font:inherit; }
pre.code { font:13px/1.6 ui-monospace, SFMono-Regular, Menlo, monospace; }
"""


def reader_page(page, entry, text, lang):
    """A text file as a page to read on a phone: it says it is UTF-8 whatever the server sends, wraps its lines, and
    links back to its task and to the file itself."""
    eid, title = entry["id"], page.d.get("title") or page.id
    name = entry.get("name") or one_line(entry.get("summary") or eid, 80)
    cls = ' class="code"' if text.lstrip()[:1] in ("{", "[") else ""  # data reads better in a fixed-width font
    return ('<!doctype html>\n<html lang="' + esc(lang) + '"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
            f'<title>{esc(name)} · {esc(title)}</title><style>{READER}</style></head><body><main>'
            f'<div class="top"><a href="../index.html">← {esc(title)}</a></div><h1>{esc(name)}</h1>'
            f'<div class="meta"><span class="kbadge">{esc(entry["kind"])}</span><span>{esc(entry.get("member") or "")}</span>'
            f'<span>{esc(say(lang, "run_n", n=entry.get("run")))}</span><span class="mono">{esc(eid)}</span></div>'
            f'<pre{cls}>{esc(text)}</pre>'
            f'<div class="foot"><a href="{esc(eid)}.txt">{esc(t(lang, "raw_file"))}</a></div></main></body></html>\n')


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(path + ".tmp", path)


def find_runs(roots):
    """Run folders under these folders (a child, or a child's run/ folder): engine, DAG and coop runs."""
    out = []
    for root in roots:
        root = os.path.abspath(os.path.expanduser(root))
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            for folder in (os.path.join(root, name), os.path.join(root, name, "run")):
                if run_facts(folder)["type"] != "other":
                    out.append(folder)
                    break
    return out


def last_active(page):
    times = [page.facts(r["n"]).get("end") or r["t"] for r in page.runs()]
    times += [e.get("t") or 0 for e in page.history]
    return max(times or [0])


# ---- drawings
def arrow(x1, y1, x2, y2, cls="", label=None, lx=None, ly=None, anchor="start"):
    """A straight arrow with its head at (x2, y2), and an optional label."""
    a = math.atan2(y2 - y1, x2 - x1)
    hx, hy = x2 - 7 * math.cos(a), y2 - 7 * math.sin(a)
    px, py = 3.5 * math.sin(a), -3.5 * math.cos(a)
    out = (f'<line class="ar {cls}" x1="{x1:.1f}" y1="{y1:.1f}" x2="{hx:.1f}" y2="{hy:.1f}"/>'
           f'<polygon class="ah {cls}" points="{x2:.1f},{y2:.1f} {hx + px:.1f},{hy + py:.1f} {hx - px:.1f},{hy - py:.1f}"/>')
    if label:
        out += f'<text class="al" x="{lx:.1f}" y="{ly:.1f}" text-anchor="{anchor}">{esc(label)}</text>'
    return out


def box(x, y, w, h, cls, title, sub=None, third=None):
    """A box with a bold line, and up to two smaller lines under it (the third in amber: skills)."""
    lines = [x for x in (sub, third) if x]
    out = f'<rect class="box {cls}" x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="7"/>'
    ty = y + h / 2 + 4 - 7.5 * len(lines)
    out += f'<text class="tx b" x="{x + 9:.1f}" y="{ty:.1f}">{esc(fit(title, w - 18, 12))}</text>'
    for i, text in enumerate(lines):
        cls2 = "k" if text is third else "s"
        out += f'<text class="tx {cls2}" x="{x + 9:.1f}" y="{ty + 15 * (i + 1):.1f}">{esc(fit(text, w - 18, 10.5))}</text>'
    return out


def life_svg(lang):
    """How a page works: who does each step, and the loop from a review to the next run."""
    steps = t(lang, "life").split("|")
    who = ["me", "", "me", "team", "me", "team"]
    W, x, w, h, gap = 320, 8, 262, 34, 16
    parts, y = [], 8
    for i, (text, cls) in enumerate(zip(steps, who)):
        parts.append(box(x, y, w, h, cls, text))
        if i < len(steps) - 1:
            parts.append(arrow(x + w / 2, y + h, x + w / 2, y + h + gap))
        y += h + gap
    top = 8 + 3 * (h + gap) + h / 2
    bottom = 8 + 5 * (h + gap) + h / 2
    parts.append(f'<path class="ar back" d="M{x + w},{bottom:.1f} H{x + w + 30} V{top:.1f} H{x + w + 7}"/>')
    parts.append(f'<polygon class="ah" points="{x + w},{top:.1f} {x + w + 7},{top - 3.5:.1f} {x + w + 7},{top + 3.5:.1f}"/>')
    parts.append(f'<text class="al" x="{x + w + 36}" y="{(top + bottom) / 2:.1f}" transform="rotate(90 {x + w + 36} {(top + bottom) / 2:.1f})" '
                 f'text-anchor="middle">n + 1</text>')
    H = y - gap + 8
    return (f'<svg viewBox="0 0 {W} {H:.0f}" role="img" aria-label="{esc(" → ".join(steps))}" style="max-width:{W + 40}px">'
            + "".join(parts) + "</svg>")


def skill_use(page):
    """{member: {"wrote": [skill names], "used": [skill names]}} over every run: a member used a skill when its entry
    built on it or it opened its file."""
    entries = page.entries()
    skills = {e["id"]: (e.get("name") or e["id"]) for e in entries.values() if e["kind"] == "skill" and e["status"] == "valid"}
    out = collections.defaultdict(lambda: {"wrote": [], "used": []})
    for e in entries.values():
        if e["id"] in skills:
            out[e["member"]]["wrote"].append(skills[e["id"]])
        for p in e.get("parents") or []:
            if p in skills and skills[p] not in out[e["member"]]["used"] and entries[p]["member"] != e["member"]:
                out[e["member"]]["used"].append(skills[p])
    for r in page.runs():
        for agent, eid in page.facts(r["n"]).get("reads") or []:
            if eid in skills and skills[eid] not in out[agent]["used"] and entries[eid]["member"] != agent:
                out[agent]["used"].append(skills[eid])
    return out


def team_tree_svg(page, lang, shape="wide"):
    """The team as a tree: the planner hands out todos to the members, every answer goes to the judge, and what
    passes is the page's knowledge, which the planner sees next (the dashed line); each member's box says how many
    skills it wrote and used, and every agent in it opens its panel. shape: "wide" (the members side by side, for a
    computer's screen) or "tall" (one under another). Lines are drawn under the boxes and never cross one."""
    team = page.d.get("team") or {}
    try:
        planner = parse_member(str(team.get("planner", "")))
        members = [parse_member(str(m)) for m in team.get("members") or []]
    except MemberError:
        return ""
    if not members:
        return ""
    about, use = team.get("about") or {}, skill_use(page)
    name = {m["name"]: who(spec, lang) for m, spec in zip([planner] + members, [team.get("planner")] + list(team.get("members") or []))}
    if shape == "tall":
        return tall_tree_svg(page, lang, planner, members, name, about, use)
    return wide_tree_svg(page, lang, planner, members, name, about, use)


def judge_name(page, lang):
    words = page.d.get("judge") or []
    script = next((w for w in words if w.endswith((".py", ".sh", ".js", ".rb", ".pl"))), None)  # name the judge by its program
    return os.path.basename(script) if script else (os.path.basename(words[0]) if words else "judge")


def wide_tree_svg(page, lang, planner, members, name, about, use):
    """Sideways: the planner on top, the members in rows of up to six under it, the judge and the knowledge below."""
    per = min(6, len(members))
    rows = [members[i:i + per] for i in range(0, len(members), per)]
    W, gap, mh, pw = 760, 12, 64, 240
    mw = min(230, (W - 40 - gap * (per - 1)) / per)  # a few members: boxes of a readable width, centred
    px = (W - pw) / 2
    lines = []
    boxes = [f'<a href="#agent-{esc(planner["name"])}" data-agent="{esc(planner["name"])}">'
             + box(px, 8, pw, 44, "team", name[planner["name"]], t(lang, "planner_role")) + "</a>"]
    y = 8 + 44 + 40
    bottoms = []
    for k, row in enumerate(rows):
        x0 = (W - (len(row) * mw + gap * (len(row) - 1))) / 2
        bus = y - 18
        xs = [x0 + i * (mw + gap) + mw / 2 for i in range(len(row))]
        lines.append(f'<path class="ar" d="M{W / 2:.1f},{52 if k == 0 else bus - 12:.1f} V{bus:.1f}"/>' if k == 0 else "")
        lines.append(f'<line class="ar" x1="{min(xs + [W / 2]):.1f}" y1="{bus:.1f}" x2="{max(xs + [W / 2]):.1f}" y2="{bus:.1f}"/>')
        for i, (m, cx) in enumerate(zip(row, xs)):
            lines.append(arrow(cx, bus, cx, y))
            u = use.get(m["name"]) or {"wrote": [], "used": []}
            third = t(lang, "skill_line", w=len(u["wrote"]), u=len(u["used"])) if (u["wrote"] or u["used"]) else None
            boxes.append(f'<a href="#agent-{esc(m["name"])}" data-agent="{esc(m["name"])}">'
                         + box(cx - mw / 2, y, mw, mh, "", name[m["name"]], about.get(m["name"]) or say(lang, "t_members"), third) + "</a>")
            bottoms.append((cx, y + mh))
        if k + 1 < len(rows):  # the todos go on down to the next row, between the boxes
            lines.append(f'<line class="ar" x1="{W / 2:.1f}" y1="{bus:.1f}" x2="{W / 2:.1f}" y2="{y + mh + 22:.1f}"/>')
        y += mh + 40
    lines.append(f'<text class="al" x="{W / 2 + 8:.1f}" y="{8 + 44 + 14}">{esc(say(lang, "t_todos"))}</text>')
    jbus = y - 22
    jy = jbus + 26
    lines.append(f'<line class="ar" x1="{min(c for c, _ in bottoms):.1f}" y1="{jbus:.1f}" x2="{max(c for c, _ in bottoms):.1f}" y2="{jbus:.1f}"/>')
    for cx, bottom in bottoms:
        lines.append(f'<line class="ar" x1="{cx:.1f}" y1="{bottom:.1f}" x2="{cx:.1f}" y2="{jbus:.1f}"/>')
    lines.append(arrow(W / 2, jbus, W / 2, jy, "", say(lang, "t_answers"), W / 2 + 8, jbus + 16))
    boxes.append(box(px, jy, pw, 44, "", t(lang, "judge_title", f=judge_name(page, lang)), t(lang, "judge_role")))
    ky = jy + 44 + 34
    passed = [e for e in page.entries().values() if e["status"] == "valid"]
    carry = ", ".join(page.d.get("carry") or []) or say(lang, "carry_all")
    lines.append(arrow(W / 2, jy + 44, W / 2, ky, "carry", say(lang, "t_verified"), W / 2 + 8, jy + 44 + 20))
    boxes.append(box(px - 30, ky, pw + 60, 44, "ok", f"{say(lang, 'page_kb')} · {t(lang, 'kb_box', n=len(passed))}",
                     f"{say(lang, 'carry')}: {carry}"))
    lines.append(f'<path class="ar back" d="M{px - 30:.1f},{ky + 22:.1f} H8 V30 H{px - 7:.1f}"/>'
                 f'<polygon class="ah" points="{px:.1f},30 {px - 7:.1f},26.5 {px - 7:.1f},33.5"/>')
    H = ky + 44 + 8
    label = f"{name[planner['name']]} → " + ", ".join(m["name"] for m in members) + f" → {judge_name(page, lang)} → {say(lang, 'page_kb')}"
    return (f'<svg viewBox="0 0 {W} {H:.0f}" role="img" aria-label="{esc(label)}" style="max-width:{W}px">'
            + "".join(lines) + "".join(boxes) + "</svg>")


def tall_tree_svg(page, lang, planner, members, name, about, use):
    """Top down, one member under another (for a narrow screen): todos come down the trunk on the left, answers go
    down the trunk on the right."""
    W, pw, mx, mw, mh, gap = 320, 190, 34, 252, 50, 8
    px, tx, rx = (W - pw) / 2, 18, W - 14
    lines, boxes = [], [f'<a href="#agent-{esc(planner["name"])}" data-agent="{esc(planner["name"])}">'
                        + box(px, 8, pw, 40, "team", name[planner["name"]], t(lang, "planner_role")) + "</a>"]
    top = 8 + 40 + 30
    ys = [top + i * (mh + gap) for i in range(len(members))]
    for m, y in zip(members, ys):
        u = use.get(m["name"]) or {"wrote": [], "used": []}
        third = t(lang, "skill_line", w=len(u["wrote"]), u=len(u["used"])) if (u["wrote"] or u["used"]) else None
        boxes.append(f'<a href="#agent-{esc(m["name"])}" data-agent="{esc(m["name"])}">'
                     + box(mx, y, mw, mh, "", name[m["name"]], about.get(m["name"]) or say(lang, "t_members"), third) + "</a>")
    lines.append(f'<path class="ar" d="M{px + 26:.1f},48 V{top - 12} H{tx} V{ys[-1] + mh / 2:.1f}"/>')
    lines.append(f'<text class="al" x="{px + 32:.1f}" y="{top - 16}">{esc(say(lang, "t_todos"))}</text>')
    for y in ys:
        lines.append(arrow(tx, y + mh / 2, mx, y + mh / 2))
        lines.append(f'<line class="ar" x1="{mx + mw}" y1="{y + mh / 2:.1f}" x2="{rx}" y2="{y + mh / 2:.1f}"/>')
    jy = ys[-1] + mh + 30
    lines.append(f'<path class="ar" d="M{rx},{ys[0] + mh / 2:.1f} V{jy + 20} H{px + pw + 7:.1f}"/>'
                 f'<polygon class="ah" points="{px + pw:.1f},{jy + 20} {px + pw + 7:.1f},{jy + 16.5} {px + pw + 7:.1f},{jy + 23.5}"/>')
    lines.append(f'<text class="al" x="{rx - 4}" y="{jy + 12}" text-anchor="end">{esc(say(lang, "t_answers"))}</text>')
    judge = judge_name(page, lang)
    boxes.append(box(px, jy, pw, 40, "", t(lang, "judge_title", f=judge), t(lang, "judge_role")))
    ky = jy + 40 + 30
    passed = [e for e in page.entries().values() if e["status"] == "valid"]
    carry = ", ".join(page.d.get("carry") or []) or say(lang, "carry_all")
    lines.append(arrow(W / 2, jy + 40, W / 2, ky, "carry", say(lang, "t_verified"), W / 2 + 6, jy + 40 + 18))
    boxes.append(box(px - 20, ky, pw + 40, 40, "ok", f"{say(lang, 'page_kb')} · {t(lang, 'kb_box', n=len(passed))}",
                     f"{say(lang, 'carry')}: {carry}"))
    lines.append(f'<path class="ar back" d="M{px - 20:.1f},{ky + 20:.1f} H5 V28 H{px - 7:.1f}"/>'
                 f'<polygon class="ah" points="{px:.1f},28 {px - 7:.1f},24.5 {px - 7:.1f},31.5"/>')
    H = ky + 40 + 8
    label = f"{name[planner['name']]} → " + ", ".join(m["name"] for m in members) + f" → {judge} → {say(lang, 'page_kb')}"
    return (f'<svg viewBox="0 0 {W} {H:.0f}" role="img" aria-label="{esc(label)}" style="max-width:560px">'
            + "".join(lines) + "".join(boxes) + "</svg>")




def chain_svg(page, lang):
    """Every run as a box, top to bottom; a green arrow from the run each one went on from, with how many entries it carried."""
    runs = page.runs()
    if not runs:
        return ""
    W, x, w, h, gap = 320, 52, 262, 46, 20
    pos, parts = {}, []
    for i, r in enumerate(runs):
        y = 8 + i * (h + gap)
        pos[r["n"]] = y
        f = page.facts(r["n"])
        first = made_by_kind(page, f)[:1]
        sub = " · ".join(v for v in (
            (span_text(f.get("seconds"), lang) if f.get("seconds") else "") if f["state"] == "ended" else say(lang, "no_end"),
            f"{tokens_text(f.get('tokens'))} tokens" if f.get("tokens") else "",
            f"{first[0][0]} {score_text(first[0][1]['score'])}" if first else "") if v)
        cls = {"ended": "", "running": "team", "stuck": "bad"}.get(f["state"], "dash")
        title = say(lang, "run_n", n=r["n"]) + f" · {local(f.get('start') or r['t'])}" + (f" · {f['type']}" if f["type"] != "engine" else "")
        parts.append(box(x, y, w, h, cls, title, sub))
    for i, r in enumerate(runs):
        src = r.get("from")
        if src not in pos:
            continue
        y1, y2 = pos[src] + h / 2, pos[r["n"]] + h / 2
        bend = x - 12 - 10 * (i % 3)
        parts.append(f'<path class="ar carry" d="M{x},{y1:.1f} H{bend} V{y2:.1f} H{x - 7}"/>')
        parts.append(f'<polygon class="ah carry" points="{x},{y2:.1f} {x - 7},{y2 - 3.5:.1f} {x - 7},{y2 + 3.5:.1f}"/>')
        n = len(page.facts(r["n"]).get("carried") or [])
        if n:
            parts.append(f'<text class="al" x="{bend - 3}" y="{y2 - 5:.1f}" text-anchor="end">{n}</text>')
    H = 8 + len(runs) * (h + gap) - gap + 8
    label = "; ".join(say(lang, "run_n", n=r["n"]) + (f" ← {r['from']}" if r.get("from") else "") for r in runs)
    return f'<svg viewBox="0 0 {W} {H:.0f}" role="img" aria-label="{esc(label)}" style="max-width:480px">' + "".join(parts) + "</svg>"


def graph_svg(page, lang):
    """The page's knowledge as a graph: a row per kind (skills first, the outputs last), a circle per entry (the
    number in it: the run that made it; green passed, red did not, dashed not judged; an amber ring: the current
    version), a line from every entry to each one that built on it."""
    entries = list(page.entries().values())
    if not entries:
        return ""
    outputs = page.d.get("outputs") or []
    first = {}
    for e in sorted(entries, key=lambda e: e.get("t") or 0):
        first.setdefault(e["kind"], e.get("t") or 0)
    order = sorted(first, key=lambda k: (k != "skill", k == "failure", k in outputs, first[k]))
    picks = {p.get("entry") for p in page.picks().values()}
    W, x0, x1, per, rh = 320, 76, 312, 11, 30
    pos, labels, y = {}, [], 22
    for kind in order:
        group = sorted((e for e in entries if e["kind"] == kind), key=lambda e: (e["run"], e.get("t") or 0))
        labels.append(f'<text class="kl" x="6" y="{y + 4}">{esc(fit(kind, 66, 10.5))}</text>'
                      f'<text class="kl" x="6" y="{y + 17}" style="font-size:9.5px;fill:var(--faint)">{len(group)}</text>')
        for i in range(0, len(group), per):
            chunk = group[i:i + per]
            step = (x1 - x0) / max(len(chunk), 1)
            for j, e in enumerate(chunk):
                pos[e["id"]] = (x0 + step * (j + 0.5), y)
            y += rh
        y += 12
    edges, nodes = [], []
    for e in entries:
        for p in e.get("parents") or []:
            if p in pos and e["id"] in pos:
                (ax, ay), (bx, by) = pos[p], pos[e["id"]]
                my = (ay + by) / 2 if ay != by else ay - 18
                edges.append(f'<path class="e" data-a="{p}" data-b="{e["id"]}" d="M{ax:.1f},{ay:.1f} C{ax:.1f},{my:.1f} {bx:.1f},{my:.1f} {bx:.1f},{by:.1f}"/>')
    for e in entries:
        cx, cy = pos[e["id"]]
        cls = {"valid": "pass", "invalid": "fail", "infra_error": "fail"}.get(e["status"], "none")
        tip = f"{e['id']} · {e['kind']} · {e['member']} · " + (score_text(e["score"]) if e["status"] == "valid" else e["status"]) \
            + f" · {say(lang, 'run_n', n=e['run'])}" + (f" · {e.get('name')}" if e.get("name") else "")
        ring = f'<circle class="ring" cx="{cx:.1f}" cy="{cy:.1f}" r="11.5"/>' if e["id"] in picks else ""
        nodes.append(f'<g class="g" data-id="{e["id"]}" data-p="{" ".join(p for p in e.get("parents") or [] if p in pos)}" '
                     f'data-tip="{esc(tip + (" · " + fit(e.get("summary") or "", 900, 13) if e.get("summary") else ""))}"><title>{esc(tip)}</title>{ring}'
                     f'<circle class="n {cls}" cx="{cx:.1f}" cy="{cy:.1f}" r="8"/>'
                     f'<text class="num" x="{cx:.1f}" y="{cy + 3:.1f}">{e["run"]}</text></g>')
    H = y
    label = ", ".join(f"{k} {sum(1 for e in entries if e['kind'] == k)}" for k in order)
    return (f'<svg class="kg" viewBox="0 0 {W} {H:.0f}" role="img" aria-label="{esc(label)}" style="max-width:640px">'
            + "".join(edges) + "".join(labels) + "".join(nodes) + "</svg>")


# ---- buttons: served live they act; as files they copy what to tell Claude
def act_button(lang, page, act, label=None, entry=None, cls="", kind=None):
    title = page.d.get("title") or page.id
    phrase = {"pick": lambda: t(lang, "say_pick", title=title, entry=entry, kind=kind),
              "accept": lambda: t(lang, "say_accept", title=title), "hold": lambda: t(lang, "say_hold", title=title),
              "reopen": lambda: t(lang, "say_reopen", title=title),
              "approve": lambda: t(lang, "say_approve", title=title, d=page.digest()),
              "note": lambda: t(lang, "say_note", title=title),
              "exclude": lambda: t(lang, "say_exclude", title=title, entry=entry),
              "include": lambda: t(lang, "say_include", title=title, entry=entry)}[act]()
    attrs = f' data-act="{act}" data-page="{esc(page.id)}" data-say="{esc(phrase)}"'
    if entry:
        attrs += f' data-entry="{esc(entry)}"'
    if act == "approve":
        attrs += f' data-digest="{esc(page.digest())}"'
    return f'<button type="button" class="act {cls}"{attrs}>{esc(label or t(lang, "act_" + act))}</button>'


def cmd_html(nb, *words):
    return f'<div class="cmd">{esc(command(nb, *words))}</div>'


# ---- the rail
def rail_items(nb, pages, lang, root, current, waiting):
    items = [f'<a class="ri{" on" if current == "home" else ""}" href="{root}index.html" title="{esc(t(lang, "home"))}">'
             f'<span class="av">⌂' + (f'<b class="cnt">{waiting}</b>' if waiting else "") + '</span>'
             f'<span class="rl"><b>{esc(t(lang, "home"))}</b><small>{esc(nb.title)}</small></span></a>']
    day = None
    for p in sorted(pages, key=last_active, reverse=True):
        d = day_of(last_active(p)) if last_active(p) else p.d.get("day")
        if d != day:
            items.append(f'<div class="rd">{esc(d[5:] if d else "")}</div>')
            day = d
        state, _ = p.state()
        title = p.d.get("title") or p.id
        items.append(f'<a class="ri{" on" if current == p.id else ""}" href="{root}p/{esc(p.id)}/" title="{esc(title)}" '
                     f'aria-label="{esc(title)}: {esc(say(lang, "st_" + state))}">'
                     f'<span class="av">{esc(letter(p))}<i class="dot {STATE_DOT[state]}"></i></span>'
                     f'<span class="rl"><b>{esc(title)}</b><small>{esc(say(lang, "st_" + state))} · {esc(p.d.get("day") or "")}</small></span></a>')
    open_requests = [r for r in nb.requests() if r["state"] == "open"]
    items.append(f'<a class="ri new{" on" if current == "new" else ""}" href="{root}new.html" title="{esc(t(lang, "new_task"))}">'
                 f'<span class="av">＋' + (f'<b class="cnt">{len(open_requests)}</b>' if open_requests else "") + '</span>'
                 f'<span class="rl"><b>{esc(t(lang, "new_task"))}</b></span></a>')
    return "".join(items)


def rail_html(nb, pages, lang, root, current, waiting):
    return f'<nav class="rail" aria-label="tasks">{rail_items(nb, pages, lang, root, current, waiting)}</nav>'


def app(nb, pages, lang, root, current, title, main, live, page_id=None):
    waiting = sum(len(p.state()[1]) for p in pages) + sum(1 for r in nb.requests() if r["state"] == "open")
    words = {k: t(lang, k) for k in ("copied", "sent", "refused", "no_server")}
    words["bring"] = " Bring: " if lang != "zh-TW" else "（帶入）"
    data = json.dumps({"live": bool(live), "page": page_id, "words": words}, ensure_ascii=False).replace("</", "<\\/")
    extra = nb.extra_style()
    extra = f"\n<style>\n{extra}</style>" if extra else ""
    return APP.format(lang=esc(lang), title=esc(title), style=STYLE, extra=extra, rail=rail_html(nb, pages, lang, root, current, waiting),
                      main=main, data=data, script=SCRIPT, root=root, menu=esc(t(lang, "menu")), close=esc(t(lang, "close")),
                      items=rail_items(nb, pages, lang, root, current, waiting))


# ---- the home page: what waits, then the tasks by day
def needs_card(nb, page, lang, root, need, own=False):
    """A thing that waits for a person, with its buttons; own=True on the task's own page (no title, fewer words)."""
    title = page.d.get("title") or page.id
    chip = f'<span class="chip {"bad" if need[0] == "stuck" else "warn"}">{esc(need_text(lang, need, page))}</span>'
    out = [f'<div class="row">{chip}</div>' if own else f'<div class="row"><h3><a href="{root}p/{esc(page.id)}/">{esc(title)}</a></h3>{chip}</div>']
    if need[0] == "approve":
        out.append(f'<div class="muted">{esc(page.d.get("goal"))}</div>')
        team = page.d.get("team") or {}
        if team.get("members"):
            out.append(f'<div class="faint">{esc(say(lang, "team"))}: {esc(who(team.get("planner"), lang))} → '
                       f'{esc(", ".join(who(m, lang) for m in team["members"]))}</div>')
        b = page.d.get("budget") or {}
        if b:
            out.append(f'<div class="faint">{esc(say(lang, "budget"))}: {esc(", ".join(f"{k} {v:g}" for k, v in sorted(b.items())))}</div>')
        out.append(f'<div data-said>{act_button(lang, page, "approve", cls="go")} <span class="said"></span></div>')
        out.append(f'<details><summary>{esc(t(lang, "cli"))}</summary>{cmd_html(nb, "run", page.id, "--dry-run")}'
                   f'{cmd_html(nb, "approve", page.id)}</details>')
    elif need[0] == "review":
        f = page.facts(need[1])
        out.append(f'<div class="muted">{esc(cost_text(lang, f.get("tokens"), f.get("member_turns"), f.get("seconds"), f["state"] == "ended"))}</div>')
        out.append(f'<div class="faint">{esc(say(lang, "todo_review"))}</div>')
        picks = page.picks()
        for kind, versions in outputs_of(page).items():
            best = best_of([v for v in versions if v["run"] == need[1]]) or best_of(versions)
            if kind in picks or not best:
                continue
            link = f'{root}p/{esc(page.id)}/files/{esc(best["file"])}' if best.get("file") else None
            ident = f'<a href="{link}">{esc(best["id"])}</a>' if link else esc(best["id"])
            out.append(f'<div data-said class="muted">{esc(kind)}: {esc(say(lang, "no_pick"))}; {esc(say(lang, "suggest"))} {ident} '
                       f'({esc(score_text(best["score"]))}, {esc(best["member"])}, {esc(say(lang, "run_n", n=best["run"]))}) '
                       f'{act_button(lang, page, "pick", entry=best["id"], kind=kind, cls="go")} <span class="said"></span></div>')
        out.append(f'<div data-said>{act_button(lang, page, "accept")} <span class="said"></span></div>')
        out.append(f'<details><summary>{esc(t(lang, "cli"))}</summary>{cmd_html(nb, "note", page.id, "…")}{cmd_html(nb, "accept", page.id)}</details>')
    return "<li>" + "".join(out) + "</li>"


def home_main(nb, pages, lang, root, roots, live):
    items = [needs_card(nb, p, lang, root, need) for p in pages for need in p.state()[1]]
    requests = [r for r in nb.requests() if r["state"] == "open"]
    runs_n = sum(len(p.runs()) for p in pages)
    chips = [f'<span class="chip">{esc(say(lang, "n_pages", n=len(pages)))}</span>',
             f'<span class="chip">{esc(say(lang, "n_runs", n=runs_n))}</span>',
             f'<span class="chip {"warn" if items or requests else "good"}">{esc(say(lang, "n_wait", n=len(items) + len(requests)))}</span>']
    out = [f'<header class="top"><div class="brand">{esc(say(lang, "brand"))}</div><h1>{esc(nb.title)}</h1>'
           f'<div class="state">{esc(say(lang, "updated", when=local(time.time(), "%Y-%m-%d %H:%M")))}</div>'
           f'<div class="chips">{"".join(chips)}</div></header>']
    req = "".join(f'<li><div class="row"><h3>{esc(r.get("title") or r["goal"][:40])}</h3><span class="chip warn">{esc(t(lang, "requests"))}</span></div>'
                  f'<div class="muted">{esc(t(lang, "request_line", id=r["id"], goal=r["goal"]))}</div>'
                  f'<div class="faint">{esc(r.get("by"))} · {esc(local(r.get("t")))}</div></li>' for r in requests)
    out.append(f'<section class="panel inbox"><h2>{esc(t(lang, "home"))}</h2>'
               + (f'<ul class="items">{"".join(items)}{req}</ul>' if items or req else f'<div class="muted">{esc(say(lang, "inbox_none"))}</div>')
               + "</section>")
    days = collections.defaultdict(list)
    for p in pages:
        on = {p.d.get("day")} | {day_of(r["t"]) for r in p.runs()} | {day_of(e["t"]) for e in p.history if e.get("kind") in LOOKED}
        for day in on:
            if day:
                days[day].append(p)
    blocks = []
    for day in sorted(days, reverse=True):
        rows = []
        for p in sorted(days[day], key=lambda p: p.d.get("title") or ""):
            state, _ = p.state()
            runs = [r for r in p.runs() if day_of(r["t"]) == day]
            facts = [p.facts(r["n"]) for r in runs]
            line = ""
            if runs:
                made = made_text(p, facts, lang, who=False)
                line = (say(lang, "runs_that_day", ns="、".join(str(r["n"]) for r in runs) if lang == "zh-TW" else ", ".join(str(r["n"]) for r in runs))
                        + (" · " + say(lang, "outputs_line", items=made) if made else "") + " · "
                        + cost_text(lang, sum(f.get("tokens") or 0 for f in facts), sum(f.get("member_turns") or 0 for f in facts),
                                    sum(f.get("seconds") or 0 for f in facts), all(f["state"] == "ended" for f in facts)))
            rows.append(f'<li><div class="row"><h3><a href="{root}p/{esc(p.id)}/">{esc(p.d.get("title"))}</a></h3>'
                        f'<span class="chip {STATE_CHIP[state]}">{esc(say(lang, "st_" + state))}</span></div>'
                        f'<div class="muted">{esc(p.d.get("goal"))}</div>' + (f'<div class="faint">{esc(line)}</div>' if line else "") + "</li>")
        blocks.append(f'<h2 style="margin-top:12px">{esc(day_label(day, lang))}</h2><ul class="items">{"".join(rows)}</ul>')
    out.append(f'<div class="grid2"><section class="panel"><h2>{esc(t(lang, "by_day"))}</h2>{"".join(blocks)}</section>'
               f'<section class="panel"><h2>{esc(say(lang, "how"))}</h2><div class="fig">{life_svg(lang)}</div>'
               f'<div class="legend"><span><i class="me"></i>{esc("你" if lang == "zh-TW" else "you")}</span>'
               f'<span><i class="team"></i>{esc("團隊" if lang == "zh-TW" else "the team")}</span><span><i></i>Claude</span></div></section></div>')
    if roots:
        found = find_runs(roots)
        held = {os.path.realpath(r["folder"]) for p in pages for r in p.runs()}
        loose = [f for f in found if os.path.realpath(f) not in held]
        m = len(found) - len(loose)
        rate = f"{m / len(found):.0%}" if found else "?"
        rows = []
        for folder in loose:
            f = run_facts(folder)
            rows.append(f'<li><span class="mono">{esc(tilde(folder))}</span> <span class="faint">{esc(f["type"])} · '
                        f'{esc(local(f.get("start")))} · {esc(f["state"])}</span></li>')
        out.append(f'<section class="panel"><h2>{esc(say(lang, "unattached"))}</h2><details><summary>'
                   f'{esc(say(lang, "attach_rate", m=m, n=len(found), roots=", ".join(tilde(os.path.abspath(os.path.expanduser(r))) for r in roots), p=rate))}'
                   f'</summary><ul class="hist" style="margin-top:8px">{"".join(rows)}</ul></details></section>')
    if not live:
        out.append(f'<div class="faint">{esc(t(lang, "static_note"))}</div>')
    return "\n".join(out)


def new_main(nb, lang, live):
    requests = nb.requests()
    pages = nb.pages()
    bring = ""
    if pages:
        rows = "".join(f'<li class="row" data-bring="{esc(p.id)}"><b>{esc(p.d.get("title") or p.id)}</b>'
                       + "".join(f'<label class="faint"><input type="checkbox" name="bring" value="{esc(p.id)}:{w}"> {esc(t(lang, "bring_" + w))}</label>'
                                 for w in ("skills", "knowledge", "current")) + "</li>" for p in pages)
        bring = (f'<fieldset class="bring"><legend>{esc(t(lang, "bring_h"))}</legend><ul class="hist">{rows}</ul>'
                 f'<div class="faint">{esc(t(lang, "bring_note"))}</div></fieldset>')
    def wants(r):
        return "; ".join(f"{ref_title_nb(nb, b['page'])}: " + ", ".join(t(lang, "bring_" + w) for w in b["bring"]) for b in r.get("bring") or [])
    rows = "".join(f'<li><span class="mono">{esc(r["id"])}</span> <b>{esc(r.get("title") or "")}</b> {esc(r["goal"])} '
                   + (f'<span class="faint">({esc(t(lang, "refs"))}: {esc(wants(r))})</span> ' if r.get("bring") else "")
                   + f'<span class="faint">· {esc(r.get("by"))} · {esc(local(r.get("t")))} · {esc(r["state"])}'
                   + (f' → <a href="p/{esc(r["page"])}/">{esc(r["page"])}</a>' if r.get("page") else "") + "</span></li>"
                   for r in reversed(requests))
    phrase = t(lang, "say_new")
    form = (f'<form class="form panel" data-said onsubmit="return false"><h2>{esc(t(lang, "new_task"))}</h2>'
            f'<div class="muted">{esc(t(lang, "new_how"))}</div>'
            f'<label>{esc(t(lang, "new_goal"))}<textarea name="goal" maxlength="2000" required></textarea></label>'
            f'<label>{esc(t(lang, "new_title"))}<input name="title" maxlength="200"></label>{bring}'
            f'<div><button type="submit" class="act go" data-act="new" data-say="{esc(phrase)}">{esc(t(lang, "act_new"))}</button> '
            f'<span class="said"></span></div>'
            f'<details><summary>{esc(t(lang, "cli"))}</summary>{cmd_html(nb, "request", "…")}</details></form>')
    out = [f'<header class="top"><div class="brand">{esc(say(lang, "brand"))}</div><h1>{esc(t(lang, "new_task"))}</h1></header>', form]
    if rows:
        out.append(f'<section class="panel"><h2>{esc(t(lang, "requests"))}</h2><ul class="hist">{rows}</ul></section>')
    out.append(f'<section class="panel"><h2>{esc(say(lang, "how"))}</h2><div class="fig">{life_svg(lang)}</div></section>')
    if not live:
        out.append(f'<div class="faint">{esc(t(lang, "static_note"))}</div>')
    return "\n".join(out)


# ---- a task
def run_card(page, r, lang, rel):
    f = page.facts(r["n"])
    state = {"ended": "", "running": "ice", "stuck": "bad", "missing": "bad", "unknown": "bad"}[f["state"]]
    word = say(lang, "st_run_" + f["state"]) if f["state"] in ("ended", "running", "stuck", "missing") else f["state"]
    head = [f'<b>{esc(say(lang, "run_n", n=r["n"]))}</b>', f'<span class="muted">{esc(local(f.get("start") or r["t"], "%Y-%m-%d %H:%M"))}</span>',
            f'<span class="chip {state}">{esc(word)}</span>']
    lines = [say(lang, "from_n", n=r["from"]) if r.get("from") else say(lang, "fresh")]
    if r.get("attached"):
        lines.append(say(lang, "attached"))
    if f["type"] in ("engine", "coop"):
        lines.append(f"{say(lang, 'team')}: " + (f"{f['planner']} → " if f.get("planner") else "") + ", ".join(f.get("members") or []))
        lines.append(cost_text(lang, f.get("tokens"), f"{f['member_turns']}/{f['turn_budget']}" if f.get("turn_budget") else f["member_turns"],
                               f.get("seconds"), f["state"] == "ended"))
        carried = f.get("carried") or []
        lines.append(say(lang, "carried", n=len(carried), kinds=kinds_text(carried, lang), u=len(f.get("used") or []))
                     if carried else say(lang, "carried_none"))
        lines.append(say(lang, "made", n=len(f["made"]), v=f["valid"], i=f["invalid"]))
        made = made_text(page, [f], lang)
        if made:
            lines.append(say(lang, "outputs_line", items=made))
    elif f["type"] == "dag":
        lines.append(f"DAG {f.get('plan') or ''}: " + steps_text(lang, f))
    if f.get("stopped"):
        lines.append(say(lang, "stopped", why=f["stopped"]))
    if r.get("picks"):
        lines.append(t(lang, "picks_line", ids=", ".join(r["picks"])))
    if r.get("references"):
        lines.append(t(lang, "refs_line", items="; ".join(
            t(lang, "refs_item", title=ref_title(page, x["page"]), n=x["run"], k=len(x["entries"])) for x in r["references"])))
    if r.get("notes"):
        lines.append(say(lang, "notes_used", ids=", ".join(r["notes"])))
    if r.get("note"):
        lines.append(r["note"])
    link = f' · <a href="{esc(rel)}">{esc(say(lang, "replay"))}</a>' if rel else ""
    return f'<li><div class="row">{" ".join(head)}{link}</div>' + "".join(f'<div class="muted">{esc(x)}</div>' for x in lines) + "</li>"


def ref_title_nb(nb, pid):
    try:
        return nb.page(pid).d.get("title") or pid
    except NotebookError:
        return pid


def ref_title(page, pid):
    try:
        return page.notebook.page(pid).d.get("title") or pid
    except NotebookError:
        return pid


def references_html(page, lang):
    """The reference material the latest run brought from other pages, each linked to the file on its own page."""
    runs = [r for r in page.runs() if r.get("references")]
    if not runs:
        return ""
    rows = []
    for ref in runs[-1]["references"]:
        try:
            src = page.notebook.page(ref["page"])
        except NotebookError:
            continue
        entries = src.entries()
        for eid in ref["entries"]:
            e = entries.get(eid)
            if e is None:
                continue
            _, ext = artifact_body(e)
            link = f' <a href="../{esc(src.id)}/files/{esc(open_name(eid, ext))}">{esc(say(lang, "open"))}</a>' if ext else ""
            rows.append(f'<li><span class="kbadge">{esc(e["kind"])}</span> <b>{esc(fit(e.get("name") or e.get("summary") or eid, 520, 13))}</b> '
                        f'<span class="faint">{esc(t(lang, "from_run", title=src.d.get("title") or src.id, n=e["run"]))} · {esc(e["member"])}</span>{link}</li>')
    if not rows:
        return ""
    return (f'<h2 style="margin-top:16px">{esc(t(lang, "refs"))} · {esc(say(lang, "run_n", n=runs[-1]["n"]))}</h2>'
            f'<div class="faint">{esc(t(lang, "refs_note"))}</div><ul class="hist">{"".join(rows)}</ul>')


def debug_html(page, lang):
    cards = []
    for r in reversed(page.runs()):
        f = page.facts(r["n"])
        lines = []
        if f["type"] in ("engine", "coop"):
            if f.get("sent_back"):
                lines.append(("warn-line", t(lang, "sent_back", n=len(f["sent_back"]))))
                for s in f["sent_back"][:6]:
                    lines.append(("", f"{t(lang, 'wake_n', w=s['wake'])}: " + "; ".join(str(x) for x in s["problems"][:3])))
            if f.get("bad_turns"):
                lines.append(("warn-line", t(lang, "bad_turns", n=len(f["bad_turns"]))))
                for b in f["bad_turns"][:6]:
                    lines.append(("", t(lang, "turn_of", m=b["member"], k=b["turn"], state=b["state"]) + ": "
                                  + fit(b.get("problem") or b.get("tail") or "", 900, 13)))
            invalid = [e for e in f.get("made") or [] if e["status"] in ("invalid", "infra_error")]
            if invalid:
                lines.append(("bad-line", t(lang, "invalid_n", n=len(invalid))))
                for e in invalid[:6]:
                    lines.append(("", f"{e['id']} ({e['kind']}, {e['member']}): " + fit(e.get("detail") or "", 900, 13)))
            failures = [e for e in f.get("made") or [] if e["kind"] == "failure"]
            if failures:
                lines.append(("warn-line", t(lang, "failure_n", n=len(failures))))
                for e in failures[:6]:
                    lines.append(("", f"{e['member']}: " + fit(e.get("summary") or "", 900, 13)))
            for b in f.get("broken") or []:
                lines.append(("bad-line", f"{t(lang, 'broken')}: {b}"))
            idle = sorted((f.get("idle") or {}).items(), key=lambda kv: -kv[1])[:4]
            if idle and any(v for _, v in idle):
                lines.append(("", f"{t(lang, 'idle')}: " + ", ".join(f"{k} {span_text(v, lang)}" for k, v in idle)))
        elif f["type"] == "dag":
            bad = {k: v for k, v in (f.get("step_states") or {}).items() if v in ("failed", "blocked")}
            if bad:
                lines.append(("bad-line", t(lang, "steps_bad", n=len(bad))))
                for k in bad:
                    lines.append(("", f"{k}: {(f.get('why') or {}).get(k) or bad[k]}"))
            for k, v in (f.get("step_states") or {}).items():
                if v in ("running", "judging") and f["state"] != "running":
                    lines.append(("warn-line", t(lang, "step_cut", s=k)))
            for k, n in (f.get("attempts") or {}).items():
                if n > 1:
                    lines.append(("", t(lang, "retried", s=k, k=n)))
        if f.get("stopped"):
            lines.append(("", say(lang, "stopped", why=f["stopped"])))
        if not any(cls for cls, _ in lines):
            lines.insert(0, ("", t(lang, "debug_none")))
        cards.append(f'<li><b>{esc(say(lang, "run_n", n=r["n"]))}</b>'
                     + "".join(f'<div class="muted {cls}">{esc(text)}</div>' for cls, text in lines) + "</li>")
    return f'<ul class="items">{"".join(cards)}</ul>' if cards else f'<div class="muted">{esc(t(lang, "no_runs"))}</div>'


def skills_html(page, lang):
    entries = page.entries()
    skills = sorted((e for e in entries.values() if e["kind"] == "skill"), key=lambda e: e.get("t") or 0)
    if not skills:
        return f'<div class="muted">{esc(t(lang, "skills_none"))}</div>'
    readers = collections.defaultdict(set)
    for r in page.runs():
        for agent, eid in page.facts(r["n"]).get("reads") or []:
            readers[eid].add(agent)
    for e in entries.values():
        for p in e.get("parents") or []:
            readers[p].add(e["member"])
    excluded = page.excluded()
    rows = []
    for s in skills:
        users = sorted(readers.get(s["id"], set()) - {s["member"]})
        bits = [t(lang, "skill_made", n=s["run"], m=s["member"]),
                t(lang, "skill_used", who=", ".join(users)) if users else t(lang, "skill_unused")]
        if s["carried_into"]:
            bits.append(t(lang, "skill_carried", ns=", ".join(map(str, s["carried_into"]))))
        if s["id"] in excluded:
            bits.append(say(lang, "excluded", by=excluded[s["id"]].get("by"), why=excluded[s["id"]].get("why") or ""))
        link = f'<a href="files/{esc(s["file"])}">{esc(say(lang, "open"))}</a>' if s.get("file") else ""
        toggle = act_button(lang, page, "include" if s["id"] in excluded else "exclude", entry=s["id"])
        status = "" if s["status"] == "valid" else f' <span class="chip bad">{esc(s["status"])}</span>'
        rows.append(f'<li data-said><div class="row"><h3>{esc(s.get("name") or s["id"])}</h3>{status}<span class="mono faint">{esc(s["id"])}</span></div>'
                    f'<div class="muted">{esc(fit(s.get("summary") or "", 900, 13))}</div>'
                    f'<div class="faint">{esc(" · ".join(bits))}</div><div class="row">{link} {toggle} <span class="said"></span></div></li>')
    return f'<ul class="items">{"".join(rows)}</ul>'


def kb_lists_html(page, lang, outs):
    entries = [e for e in page.entries().values() if e["status"] == "valid"]
    if not entries:
        return ""
    nxt = {e["id"] for e in page.next_carry()}
    excluded = page.excluded()
    shown = set(k for k in outs if outs[k])
    kinds = collections.OrderedDict()
    for e in sorted(entries, key=lambda e: (e["kind"] != "skill", e["kind"], e.get("t") or 0)):
        if e["kind"] not in shown:
            kinds.setdefault(e["kind"], []).append(e)
    blocks = []
    for kind, group in kinds.items():
        rows = []
        for e in group[:20]:
            bits = [say(lang, "made_in", n=e["run"], member=e["member"])]
            if e["carried_into"]:
                bits.append(say(lang, "carried_into", ns=", ".join(map(str, e["carried_into"]))))
            if e["built_on"]:
                bits.append(say(lang, "built_on_by", n=e["built_on"]))
            if e["id"] in excluded:
                bits.append(say(lang, "excluded", by=excluded[e["id"]].get("by"), why=excluded[e["id"]].get("why") or ""))
            link = f' <a href="files/{esc(e["file"])}">{esc(say(lang, "open"))}</a>' if e.get("file") else ""
            rows.append(f'<li data-said><span class="mono">{esc(("→ " if e["id"] in nxt else "") + e["id"])}</span>'
                        f'<span>{esc(fit(e.get("name") or e.get("summary") or "", 520, 13))}</span>'
                        f'<span class="faint">{esc(" · ".join(bits))}</span>{link} '
                        f'{act_button(lang, page, "include" if e["id"] in excluded else "exclude", entry=e["id"])} <span class="said"></span></li>')
        more = f'<div class="faint">… {len(group) - 20}</div>' if len(group) > 20 else ""
        blocks.append(f'<div><b>{esc(kind)}</b> <span class="faint">{len(group)}</span><ul class="vers">{"".join(rows)}</ul>{more}</div>')
    line = say(lang, "kb_line", n=len(entries), kinds=kinds_text(entries, lang), m=len(nxt))
    above = sorted(shown)
    return (f'<div class="muted" style="margin-top:12px">{esc(line)}'
            f'{esc("（→ 標的是下一次會帶入的）" if lang == "zh-TW" else " (→ marks what the next run carries)")}</div>'
            + (f'<div class="faint">{esc(say(lang, "outputs_above", kinds=", ".join(above)))}</div>' if above else "")
            + f'<div class="items" style="gap:12px;margin-top:8px">{"".join(blocks)}</div>')


def versions_html(page, lang, outs):
    if not any(outs.values()):
        return f'<div class="muted">{esc(t(lang, "outputs_none"))}</div>'
    picks = page.picks()
    blocks = []
    for kind, versions in outs.items():
        if not versions:
            continue
        cur = picks.get(kind)
        rows = []
        for v in versions[:12]:
            is_cur = cur is not None and cur.get("entry") == v["id"]
            link = f'<a href="files/{esc(v["file"])}">{esc(say(lang, "open"))}</a>' if v.get("file") else ""
            tag = f'<span class="chip good">{esc(say(lang, "current"))}</span>' if is_cur else ""
            button = "" if is_cur else act_button(lang, page, "pick", entry=v["id"], kind=kind)
            rows.append(f'<li class="{"cur" if is_cur else ""}" data-said>{tag}<span class="mono">{esc(v["id"])}</span>'
                        f'<span>{esc(say(lang, "run_n", n=v["run"]))} · {esc(v["member"])} · {esc(score_text(v["score"]))}</span>'
                        f'<span class="faint">{esc(local(v.get("t")))}</span>{link} {button} <span class="said"></span></li>')
        note = say(lang, "picked", by=cur.get("by"), when=local(cur.get("t"))) if cur else say(lang, "no_pick")
        more = f'<div class="faint">… {len(versions) - 12}</div>' if len(versions) > 12 else ""
        blocks.append(f'<div><div class="row"><b>{esc(say(lang, "kind_versions", kind=kind, n=len(versions)))}</b>'
                      f'<span class="faint">{esc(note)}</span></div><ul class="vers">{"".join(rows)}</ul>{more}</div>')
    return f'<div class="items" style="gap:14px">{"".join(blocks)}</div>'


def history_html(page, lang):
    rows = []
    for e in sorted(page.history, key=lambda e: e.get("t") or 0):
        k, why = e.get("kind"), e.get("why") or ""
        text = {
            "create": lambda: say(lang, "ev_create"), "revise": lambda: say(lang, "ev_revise"),
            "approve": lambda: say(lang, "ev_approve", d=e.get("digest")),
            "run": lambda: (say(lang, "ev_attach", n=e.get("n"), dir=tilde(e.get("dir"))) if e.get("attached") else say(lang, "ev_run", n=e.get("n"))),
            "run_end": lambda: say(lang, "ev_run_end", n=e.get("n"), code=e.get("exit")),
            "note": lambda: say(lang, "ev_note", id=e.get("id"), text=e.get("text")),
            "pick": lambda: say(lang, "ev_pick", entry=e.get("entry"), kind=e.get("of")),
            "exclude": lambda: say(lang, "ev_exclude", entry=e.get("entry"), why=why),
            "include": lambda: say(lang, "ev_include", entry=e.get("entry")),
            "accept": lambda: say(lang, "ev_accept", why=why), "hold": lambda: say(lang, "ev_hold", why=why),
            "done": lambda: say(lang, "ev_done", why=why), "reopen": lambda: say(lang, "ev_reopen", why=why),
        }.get(k, lambda: str(k))()
        mark = f' <span class="chip">{esc(say(lang, "imported"))} {esc(local(e.get("recorded")))}</span>' if e.get("imported") else ""
        rows.append(f'<li><span class="faint">{esc(local(e.get("t"), "%Y-%m-%d %H:%M"))}</span> · <b>{esc(e.get("by"))}</b> · {esc(text)}{mark}</li>')
    return '<ul class="hist">' + "".join(rows) + "</ul>"


def definition_html(page, lang):
    d = page.d
    kv = []
    approval = page.approval()
    kv.append(("", say(lang, "approved_as", d=page.digest(), by=approval.get("by"), when=local(approval.get("t"))) if approval
               else say(lang, "not_approved", d=page.digest())))
    if d.get("asked"):
        kv.append((say(lang, "asked"), d["asked"]))
    if d.get("task"):
        kv.append((say(lang, "task_file"), d["task"]))
    if d.get("judge"):
        kv.append((say(lang, "judge"), " ".join(d["judge"])))
    b = d.get("budget") or {}
    parts = [say(lang, "b_" + {"turns": "turns", "planner_wakes": "wakes", "time_limit": "time", "max_open": "open",
                               "turn_timeout": "turn_timeout"}[k], n=f"{b[k]:g}") for k in ("turns", "planner_wakes", "time_limit", "max_open", "turn_timeout") if k in b]
    if parts:
        kv.append((say(lang, "budget"), " · ".join(parts)))
    if d.get("kind", "engine") == "engine":
        kv.append((say(lang, "carry"), ", ".join(d.get("carry") or []) or say(lang, "carry_all")))
    if d.get("outputs"):
        kv.append((say(lang, "outputs"), ", ".join(d["outputs"])))
    for ref in d.get("from") or []:
        what = ", ".join(x for x in [", ".join(t(lang, "kinds_all") if k == "*" else k for k in ref.get("kinds") or []),
                                      ", ".join(ref.get("entries") or []),
                                      t(lang, "bring_current") if ref.get("current") else ""] if x)
        kv.append((t(lang, "refs"), f"{ref_title(page, ref.get('page'))}" + (f" ({say(lang, 'run_n', n=ref['run'])})" if ref.get("run") else "") + f": {what}"))
    task = ""
    if d.get("task"):
        try:
            with open(page.task_path(), encoding="utf-8") as handle:
                text = handle.read()
            task = f'<details><summary>{esc(say(lang, "task_file"))}</summary><pre class="task">{esc(text if len(text) < 6000 else text[:6000] + "…")}</pre></details>'
        except OSError:
            task = ""
    return '<dl class="kv">' + "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in kv) + "</dl>" + task


# ---- the agents of a task, as the records say (never made up: no progress or time a run did not record)
def latest_engine(page):
    for r in reversed(page.runs()):
        f = page.facts(r["n"])
        if f["type"] in ("engine", "coop"):
            return r, f
    return None, None


def agent_status(f, name, lang):
    """(class, word) for a member in a run: working or waiting while the run goes on; after it, how its last turn ended."""
    if f is None:
        return "", t(lang, "ag_no_turn")
    if f["state"] == "running":
        return ("run", t(lang, "ag_working")) if name in (f.get("working") or []) else ("", t(lang, "ag_waiting"))
    mine = [x for x in f.get("turn_list") or [] if x.get("member") == name]
    if not mine:
        return "", t(lang, "ag_no_turn")
    last = mine[-1]
    if last.get("state") not in (None, "idle"):
        return "bad", t(lang, "ag_broke")
    if last.get("kind") == "failure":
        return "warn", t(lang, "ag_could_not")
    if last.get("status") == "valid":
        return "ok", t(lang, "ag_passed")
    if last.get("status") in ("invalid", "infra_error"):
        return "bad", t(lang, "ag_failed")
    return "", t(lang, "ag_no_answer")


def agents_of(page, lang):
    """The planner and every member of the task's team (or of its latest run, when the page names no team), each with
    what the records say: its state, its todos in the latest run, the skills it wrote and used, what it made, its turns."""
    team = page.d.get("team") or {}
    r, f = latest_engine(page)
    specs = ([(team["planner"], "planner")] if team.get("planner") else []) + [(m, "member") for m in team.get("members") or []]
    if not specs and f:
        specs = ([(f["planner"], "planner")] if f.get("planner") else []) + [(m, "member") for m in f.get("members") or []]
    about, use, entries = team.get("about") or {}, skill_use(page), page.entries()
    out = []
    for spec, role in specs:
        name = str(spec).split("=")[0]
        a = {"name": name, "role": role, "who": who(spec, lang), "run": r["n"] if r else None,
             "about": about.get(name) or (t(lang, "planner_role") if role == "planner" else "")}
        if role == "planner":
            wakes = (f or {}).get("wake_list") or []
            a.update(wakes=wakes, status=("", t(lang, "ag_planner", n=len({w["wake"] for w in wakes}), b=sum(1 for w in wakes if w["problems"]))),
                     todos=(f or {}).get("todo_list") or [], now=None)
        else:
            todos = [x for x in (f or {}).get("todo_list") or [] if x.get("member") == name]
            working = [x for x in todos if x.get("state") == "taken"]
            now = (t(lang, "ag_now", text=working[-1]["text"]) if working and f["state"] == "running"
                   else t(lang, "ag_last", text=todos[-1]["text"]) if todos else None)
            turns = []
            for run in page.runs():
                for x in page.facts(run["n"]).get("turn_list") or []:
                    if x.get("member") == name:
                        turns.append((run["n"], x))
            u = use.get(name) or {"wrote": [], "used": []}
            a.update(status=agent_status(f, name, lang), todos=todos, now=now, skills=u, turns=turns,
                     made=sorted((e for e in entries.values() if e["member"] == name), key=lambda e: -(e.get("t") or 0)))
        out.append(a)
    return out


def agent_card(a, lang):
    cls, word = a["status"]
    count = ""
    if a["role"] == "member" and a["todos"]:
        count = t(lang, "ag_todos", d=sum(1 for x in a["todos"] if x.get("state") == "done"), n=len(a["todos"]))
    elif a["role"] == "planner" and a["todos"]:
        count = say(lang, "t_todos") + f" {len(a['todos'])}"
    return (f'<li><a class="agent" href="#agent-{esc(a["name"])}" data-agent="{esc(a["name"])}">'
            f'<span class="av">{esc(a["name"][:1].upper())}</span>'
            f'<span class="ag-main"><span><b>{esc(a["name"])}</b><small>{esc(a["who"].split(" · ", 1)[-1])}</small></span>'
            + (f'<span class="ag-sub">{esc(a["about"])}</span>' if a["about"] else "")
            + (f'<span class="ag-now">{esc(a["now"])}</span>' if a.get("now") else "")
            + f'</span><span class="ag-side"><span class="badge {cls}">{esc(word)}</span>'
            + (f'<span class="ag-count">{esc(count)}</span>' if count else "") + "</span></a></li>")


def agent_detail(page, a, lang):
    cls, word = a["status"]
    out = [f'<div class="ag-head"><span class="av">{esc(a["name"][:1].upper())}</span><div style="flex:1;min-width:0"><h3>{esc(a["name"])}</h3>'
           f'<div class="faint">{esc(a["who"].split(" · ", 1)[-1])}</div></div><span class="badge {cls}">{esc(word)}</span></div>']
    if a["about"]:
        out.append(f'<h4>{esc(t(lang, "ag_role"))}</h4><p>{esc(a["about"])}</p>')
    if a["role"] == "planner":
        rows = "".join(f'<li>{esc(t(lang, "ag_wake_line", w=w["wake"], a=w["added"], d=w["dropped"]))}'
                       + (f' <span class="warn-line">· {esc("; ".join(str(p) for p in w["problems"][:2]))}</span>' if w["problems"] else "")
                       + (f' <span class="faint">· {esc(span_text(w["seconds"], lang))}</span>' if w.get("seconds") else "") + "</li>"
                       for w in a["wakes"])
        if rows:
            out.append(f'<h4>{esc(t(lang, "ag_wakes_h"))} · {esc(say(lang, "run_n", n=a["run"]))}</h4><ul class="hist">{rows}</ul>')
        return "".join(out)
    mine = [x for n, x in a["turns"] if n == a["run"]]
    if mine:
        tokens = sum(x.get("tokens") or 0 for x in mine)
        out.append(f'<h4>{esc(t(lang, "ag_run", n=a["run"]))}</h4><p class="muted">'
                   f'{esc(cost_text(lang, tokens, len(mine), sum(x.get("seconds") or 0 for x in mine)))}</p>')
    if a["todos"]:
        rows = "".join(f'<li><span class="chip">{esc(x.get("state"))}</span> {esc(fit(x.get("text") or "", 900, 13))}'
                       + (f' <span class="faint">→ {esc(x["entry"])} · {esc(score_text(x.get("score")))}</span>' if x.get("entry") else "") + "</li>"
                       for x in a["todos"])
        out.append(f'<h4>{esc(t(lang, "ag_todos_h", n=a["run"]))}</h4><ul class="hist">{rows}</ul>')
    none = t(lang, "ag_none")
    out.append(f'<h4>{esc(t(lang, "ag_skills_h"))}</h4><p class="muted">{esc(t(lang, "ag_wrote", x=", ".join(a["skills"]["wrote"]) or none))}</p>'
               f'<p class="muted">{esc(t(lang, "ag_used", x=", ".join(a["skills"]["used"]) or none))}</p>')
    if a["made"]:
        rows = "".join(f'<li><span class="kbadge">{esc(e["kind"])}</span> <span class="mono">{esc(e["id"])}</span> · '
                       f'{esc(say(lang, "run_n", n=e["run"]))} · {esc(score_text(e["score"]) if e["status"] == "valid" else e["status"])}'
                       + (f' · <a href="files/{esc(e["file"])}">{esc(say(lang, "open"))}</a>' if e.get("file") else "") + "</li>"
                       for e in a["made"][:10])
        out.append(f'<h4>{esc(t(lang, "ag_out_h"))}</h4><ul class="hist">{rows}</ul>')
    if a["turns"]:
        rows = []
        for n, x in reversed(a["turns"][-20:]):
            what = (score_text(x.get("score")) + f" ({x.get('entry')})" if x.get("status") == "valid"
                    else (x.get("status") or x.get("kind") or x.get("state") or "?") + (f": {fit(x.get('problem') or '', 400, 13)}" if x.get("problem") else ""))
            rows.append(f'<li>{esc(t(lang, "ag_turn_line", r=n, k=x.get("turn"), what=what))}'
                        + (f' <span class="faint">· {esc(span_text(x.get("seconds"), lang))}</span>' if x.get("seconds") else "") + "</li>")
        out.append(f'<h4>{esc(t(lang, "ag_turns_h"))}</h4><ul class="hist">{"".join(rows)}</ul>')
    return "".join(out)


def overview_html(nb, page, lang, needs, agents, outs):
    runs = page.runs()
    passed = [e for e in page.entries().values() if e["status"] == "valid"]
    turns = sum(page.facts(r["n"]).get("member_turns") or 0 for r in runs)
    stats = [(len(runs), t(lang, "stat_runs"), ""), (turns, t(lang, "stat_turns"), ""), (len(passed), t(lang, "stat_passed"), "good" if passed else ""),
             (len(needs), t(lang, "stat_wait"), "warn" if needs else "")]
    out = ['<div class="stats">' + "".join(f'<div class="stat {c}"><b>{n}</b><span>{esc(label)}</span></div>' for n, label, c in stats) + "</div>"]
    if needs:
        out.append('<ul class="items" style="margin-top:12px">' + "".join(needs_card(nb, page, lang, "../../", need, own=True) for need in needs) + "</ul>")
    if agents:
        run_n = agents[0].get("run")
        head = t(lang, "agents") + (f" · {say(lang, 'run_n', n=run_n)}" if run_n else "")
        out.append(f'<h2 style="margin-top:16px">{esc(head)}</h2><ul class="agents">{"".join(agent_card(a, lang) for a in agents)}</ul>'
                   f'<div class="faint js-only" style="margin-top:6px">{esc(t(lang, "agents_hint"))}</div>')
    else:
        r, f = (page.runs()[-1], page.facts(page.runs()[-1]["n"])) if runs else (None, None)
        if f and f["type"] == "dag":
            out.append(f'<h2 style="margin-top:16px">{esc(say(lang, "steps"))}</h2><div class="muted">{esc(steps_text(lang, f))}</div>')
    picks = page.picks()
    rows = []
    for kind, versions in outs.items():
        if not versions:
            continue
        cur = next((v for v in versions if picks.get(kind, {}).get("entry") == v["id"]), None)
        v = cur or best_of(versions)
        tag = say(lang, "current") if cur else say(lang, "suggest")
        link = f' <a href="files/{esc(v["file"])}">{esc(say(lang, "open"))}</a>' if v.get("file") else ""
        rows.append(f'<li><span class="kbadge">{esc(kind)}</span> <b>{esc(tag)}</b> <span class="mono">{esc(v["id"])}</span> '
                    f'<span class="faint">{esc(say(lang, "run_n", n=v["run"]))} · {esc(v["member"])} · {esc(score_text(v["score"]))}</span>{link}</li>')
    if rows:
        out.append(f'<h2 style="margin-top:16px">{esc(t(lang, "outputs_now"))}</h2><ul class="hist">{"".join(rows)}</ul>')
    out.append(references_html(page, lang))
    if agents:
        out.append(f'<div class="agent-details"><h2 style="margin-top:16px">{esc(t(lang, "details"))}</h2>'
                   + "".join(f'<section class="agent-detail" id="agent-{esc(a["name"])}">{agent_detail(page, a, lang)}</section>' for a in agents)
                   + "</div>")
    return "".join(out)


def kb_html(page, lang):
    """The knowledge as a list to search and filter by kind (with JavaScript), and as a graph."""
    entries = sorted((e for e in page.entries().values() if e["status"] == "valid"),
                     key=lambda e: (e["kind"] != "skill", e["kind"], -(e.get("t") or 0)))
    out = []
    if entries:
        nxt, excluded = {e["id"] for e in page.next_carry()}, page.excluded()
        kinds = collections.Counter(e["kind"] for e in entries)
        buttons = [f'<button type="button" class="on" data-kind="">{esc(t(lang, "kb_all"))} {len(entries)}</button>']
        buttons += [f'<button type="button" data-kind="{esc(k)}">{esc(k)} {n}</button>' for k, n in sorted(kinds.items(), key=lambda kv: (kv[0] != "skill", -kv[1]))]
        out.append(f'<div class="kb-tools"><input type="search" class="kb-search" placeholder="{esc(t(lang, "kb_search"))}" aria-label="{esc(t(lang, "kb_search"))}">'
                   f'<div class="kb-kinds">{"".join(buttons)}</div></div>')
        rows = []
        for e in entries:
            bits = [say(lang, "made_in", n=e["run"], member=e["member"]), score_text(e["score"])]
            if e["carried_into"]:
                bits.append(say(lang, "carried_into", ns=", ".join(map(str, e["carried_into"]))))
            if e["built_on"]:
                bits.append(say(lang, "built_on_by", n=e["built_on"]))
            if e["id"] in excluded:
                bits.append(say(lang, "excluded", by=excluded[e["id"]].get("by"), why=excluded[e["id"]].get("why") or ""))
            title = e.get("name") or e.get("summary") or e["id"]
            text = " ".join(str(x) for x in (e["id"], e["kind"], e.get("name") or "", e.get("summary") or "", e["member"])).lower()
            link = f'<a href="files/{esc(e["file"])}">{esc(say(lang, "open"))}</a>' if e.get("file") else ""
            carry = f' <span class="chip good">{esc(say(lang, "t_next"))}</span>' if e["id"] in nxt else ""
            rows.append(f'<li data-kind="{esc(e["kind"])}" data-text="{esc(text)}" data-said><div class="row"><span class="kbadge">{esc(e["kind"])}</span>'
                        f'<b>{esc(fit(title, 520, 13))}</b>{carry}</div><div class="faint">{esc(" · ".join(bits))} · <span class="mono">{esc(e["id"])}</span></div>'
                        f'<div class="row">{link} {act_button(lang, page, "include" if e["id"] in excluded else "exclude", entry=e["id"])} <span class="said"></span></div></li>')
        out.append(f'<ul class="kb-list">{"".join(rows)}</ul>')
    graph = graph_svg(page, lang)
    legend = t(lang, "graph_legend").split("|")
    if graph:
        out.append(f'<h2 style="margin-top:18px">{esc(t(lang, "kb_graph"))}</h2><div class="fig">{graph}</div><div class="tip"></div>'
                   f'<div class="legend"><span>{esc(legend[0])}</span><span><i class="ok"></i>{esc(legend[1])}</span>'
                   f'<span><i class="bad"></i>{esc(legend[2])}</span><span><i class="none"></i>{esc(legend[3])}</span>'
                   f'<span><i class="ring"></i>{esc(legend[4])}</span><span>{esc(legend[5])}</span>'
                   f'<span class="js-only">{esc(t(lang, "graph_tap"))}</span></div>')
    return f'<div class="kb">{"".join(out)}</div>' if out else f'<div class="muted">{esc(t(lang, "graph_none"))}</div>'


def task_main(nb, page, lang, run_links, live):
    d = page.d
    state, needs = page.state()
    runs = page.runs()
    facts = [page.facts(r["n"]) for r in runs]
    chips = [f'<span class="chip {STATE_CHIP[state]}">{esc(say(lang, "st_" + state))}</span>',
             f'<span class="chip">{esc(say(lang, "opened"))} {esc(d.get("day"))}</span>']
    last = last_active(page)
    if last:
        chips.append(f'<span class="chip">{esc(say(lang, "updated", when=local(last)))}</span>')
    buttons = [act_button(lang, page, "reopen")] if state in ("hold", "done") else [act_button(lang, page, "hold", cls="warn")]
    out = [f'<header class="top"><div class="brand">{esc(say(lang, "brand"))} · {esc(nb.title)}</div>'
           f'<h1>{esc(d.get("title"))}</h1><div class="state">{esc(d.get("goal"))}</div><div class="chips">{"".join(chips)}</div>'
           f'<div data-said>{" ".join(buttons)} <span class="said"></span></div></header>']
    names = t(lang, "tabs").split("|")
    ids = ["overview", "arch", "replay", "debug", "skills", "kb", "outputs", "notes"]
    out.append('<nav class="tabs" aria-label="tabs">' + "".join(f'<a href="#{i}" data-tab="{i}">{esc(n)}</a>' for i, n in zip(ids, names)) + "</nav>")
    outs = outputs_of(page)
    agents = agents_of(page, lang)
    sections = {"overview": overview_html(nb, page, lang, needs, agents, outs)}
    # the team, drawn; every agent in it opens its panel
    if d.get("kind", "engine") == "dag" or (facts and facts[-1]["type"] == "dag" and not (d.get("team") or {}).get("members")):
        dag = next((f for f in reversed(facts) if f["type"] == "dag"), None)
        arch = ""
        if dag and dag.get("plan_doc"):
            try:
                st = {k: {"state": v, "attempts": dag["attempts"].get(k, 0), "score": None, "sha": None, "why": (dag.get("why") or {}).get(k)}
                      for k, v in dag["step_states"].items()}
                arch = f'<div class="fig dag">{dagview.graph_svg(dag["plan_doc"], st)}</div>'
            except Exception:  # noqa: BLE001 - the plan is drawn by dagview; a plan it cannot draw is listed instead
                arch = ""
        arch += f'<div class="muted">{esc(steps_text(lang, dag))}</div>' if dag else ""
    else:
        shape = nb.tree if nb.tree in ("wide", "tall", "both") else "wide"
        drawn = [(x, team_tree_svg(page, lang, x)) for x in (("wide", "tall") if shape == "both" else (shape,))]
        team = "".join(f'<div class="fig tree-{x}{" alt" if shape == "both" and x == "tall" else ""}">{svg}</div>' for x, svg in drawn if svg)
        arch = team + (
            f'<div class="legend"><span><i class="team"></i>planner</span><span><i class="ok"></i>{esc(say(lang, "page_kb"))}</span>'
            f'<span>{esc("虛線：通過的回到 planner" if lang == "zh-TW" else "dashed: what passed goes back to the planner")}</span>'
            f'<span class="js-only">{esc(t(lang, "agents_hint"))}</span></div>')
        per_run = [(r["n"], page.facts(r["n"]).get("members") or []) for r in runs if page.facts(r["n"]).get("members")]
        if len({tuple(m) for _, m in per_run}) > 1:
            arch += (f'<details style="margin-top:10px"><summary>{esc(t(lang, "team_runs"))}</summary><ul class="hist">'
                     + "".join(f'<li>{esc(say(lang, "run_n", n=n))}: {esc(", ".join(m))}</li>' for n, m in per_run) + "</ul></details>")
    if d.get("links"):
        arch += '<ul class="hist" style="margin-top:8px">' + "".join(f'<li><a href="{esc(x["url"])}">{esc(x["label"])}</a></li>' for x in d["links"]) + "</ul>"
    sections["arch"] = arch
    # the runs and their replays
    if runs:
        latest = runs[-1]["n"]
        chips = "".join(f'<a class="chip{" ice" if r["n"] == latest else ""}" href="{esc(run_links[r["n"]])}" data-run="{r["n"]}" '
                        f'data-label="{esc(t(lang, "replay_of", n=r["n"]))}">{esc(say(lang, "run_n", n=r["n"]))}</a>'
                        for r in runs if r["n"] in run_links)
        frame = (f'<div class="row" style="margin:10px 0 6px"><b id="replay-of">{esc(t(lang, "replay_of", n=latest))}</b>'
                 f'<a href="{esc(run_links[latest])}">{esc(t(lang, "open_full"))}</a></div>'
                 f'<iframe class="replay" src="{esc(run_links[latest])}" loading="lazy" title="{esc(t(lang, "replay_of", n=latest))}"></iframe>'
                 if latest in run_links else "")
        cards = "".join(run_card(page, r, lang, run_links.get(r["n"])) for r in reversed(runs))
        sections["replay"] = (f'<div class="chips">{chips}</div>{frame}<div class="grid2" style="margin-top:12px"><div><h2>{esc(say(lang, "chain"))}</h2>'
                              f'<div class="fig">{chain_svg(page, lang)}</div><div class="legend"><span><i class="carry"></i>'
                              f'{esc("延續（數字：帶入幾條）" if lang == "zh-TW" else "goes on from (number: entries carried)")}</span></div></div>'
                              f'<div><h2>{esc(say(lang, "runs"))}</h2><ul class="items">{cards}</ul></div></div>')
    else:
        sections["replay"] = f'<div class="muted">{esc(t(lang, "no_runs"))}</div>'
    # what went wrong, and the raw settings and commands
    sections["debug"] = (debug_html(page, lang) + f'<details style="margin-top:12px"><summary>{esc(t(lang, "config"))}</summary>'
                         f'{definition_html(page, lang)}</details>')
    sections["skills"] = skills_html(page, lang)
    sections["kb"] = kb_html(page, lang)
    sections["outputs"] = versions_html(page, lang, outs)
    notes = page.notes()
    rows = "".join(f'<li><span class="mono">{esc(x["id"])}</span> {esc(x["text"])} <span class="faint">· {esc(x.get("by"))} · {esc(local(x.get("t")))} · '
                   f'{esc(say(lang, "note_used", n=x["used_by"]) if x["used_by"] else say(lang, "note_next"))}</span></li>' for x in notes)
    sections["notes"] = ((f'<ul class="hist">{rows}</ul>' if rows else f'<div class="muted">{esc(say(lang, "notes_none"))}</div>')
                         + f'<form class="form" data-said onsubmit="return false" style="margin-top:12px"><textarea name="text" maxlength="4000" '
                         f'placeholder="{esc(t(lang, "note_ph"))}"></textarea><div>{act_button(lang, page, "note", cls="go")} <span class="said"></span></div>'
                         f'<details><summary>{esc(t(lang, "cli"))}</summary>{cmd_html(nb, "note", page.id, "…")}</details></form>'
                         f'<h2 style="margin-top:18px">{esc(say(lang, "history"))}</h2>{history_html(page, lang)}')
    for i, n in zip(ids, names):
        out.append(f'<section class="panel tab" id="{i}"><h2>{esc(n)}</h2>{sections[i]}</section>')
    if not live:
        out.append(f'<div class="faint">{esc(t(lang, "static_note"))}</div>')
    return "\n".join(out)


# ---- rendering a whole notebook
def run_page(f, folder):
    if f["type"] == "engine":
        return engineview.render(folder)
    if f["type"] == "dag":
        return dagview.render(folder)
    if f["type"] == "coop":
        return coopview.build(folder)
    return None


def render_task(nb, page, pages, live=False):
    run_links = {r["n"]: f"runs/{r['n']}.html" for r in page.runs() if page.facts(r["n"])["type"] in ("engine", "dag", "coop")}
    for e in page.entries().values():  # the file a version opens as, without writing it (serve makes it when asked)
        if e["status"] == "valid" and e.get("artifact") and e["id"] not in page.files:
            _, ext = artifact_body(e)
            page.files[e["id"]] = open_name(e["id"], ext)
    main = task_main(nb, page, nb.lang, run_links, live)
    return app(nb, pages, nb.lang, "../../", page.id, page.d.get("title") or page.id, main, live, page.id)


def view(nb, out, roots=()):
    """Write the notebook as pages under out: index.html, new.html, p/<id>/index.html, p/<id>/runs/<n>.html and
    p/<id>/files/ (every verified file, its kind line taken off). Returns the problems met (one run's page that could
    not be drawn never stops the rest)."""
    pages, problems = nb.pages(), []
    for page in pages:
        base = os.path.join(out, "p", page.id)
        for e in page.entries().values():
            if e["status"] == "valid":
                text, ext = artifact_body(e)
                if text is not None:
                    write(os.path.join(base, "files", e["id"] + ext), text)
                    if ext == ".txt":  # the page it opens as (open_name)
                        write(os.path.join(base, "files", e["id"] + ".html"), reader_page(page, e, text, nb.lang))
                    page.files[e["id"]] = open_name(e["id"], ext)
        for r in page.runs():
            try:
                html_ = run_page(page.facts(r["n"]), r["folder"])
                if html_ is not None:
                    write(os.path.join(base, "runs", f"{r['n']}.html"), html_)
            except Exception as exc:  # noqa: BLE001 - one run's page must not stop the notebook's
                problems.append(f"{page.id} run {r['n']}: {type(exc).__name__}: {exc}")
        write(os.path.join(base, "index.html"), render_task(nb, page, pages))
    write(os.path.join(out, "index.html"), app(nb, pages, nb.lang, "", "home", nb.title, home_main(nb, pages, nb.lang, "", roots, False), False))
    write(os.path.join(out, "new.html"), app(nb, pages, nb.lang, "", "new", t(nb.lang, "new_task"), new_main(nb, nb.lang, False), False))
    return problems


# ---- serving it live, with buttons
ACTS = ("pick", "note", "accept", "hold", "reopen", "done", "approve", "exclude", "include")
BY_WEB = "person (web)"


def act(nb, body):
    """Apply a person's command from a page; returns what was recorded. Raises NotebookError when it cannot be done."""
    command_ = body.get("command")
    if command_ not in ACTS:
        raise NotebookError(f"no command {command_!r}")
    page = nb.page(str(body.get("page") or ""))
    if command_ == "pick":
        return pick(page, str(body.get("entry") or ""), BY_WEB)
    if command_ == "note":
        return add_note(page, str(body.get("text") or ""), BY_WEB)
    if command_ == "approve":
        if body.get("digest") != page.digest():
            raise NotebookError("the definition changed after this page was drawn; open it again before approving")
        return approve(page, BY_WEB)
    if command_ in ("exclude", "include"):
        eid = str(body.get("entry") or "")
        find_entry(page, eid)
        if (eid in page.excluded()) == (command_ == "exclude"):
            raise NotebookError(f"{eid} is {'already' if command_ == 'exclude' else 'not'} excluded")
        return page.append(command_, BY_WEB, entry=eid, why=str(body.get("why") or ""))
    return page.append(command_, BY_WEB, why=str(body.get("why") or ""))


SAFE = re.compile(r"^/p/([a-z0-9][a-z0-9-]{0,63})/(?:(index\.html)?|runs/(\d+)\.html|files/(k[0-9a-f]{12})\.(html|svg|txt))$")


def route(folder, path, roots=(), live=True):
    """(status, body, content type, extra headers) for a GET of the live notebook."""
    nb = Notebook(folder)
    pages = nb.pages()
    if path in ("/", "/index.html"):
        return 200, app(nb, pages, nb.lang, "", "home", nb.title, home_main(nb, pages, nb.lang, "", roots, live), live), "text/html; charset=utf-8", {}
    if path == "/new.html":
        return 200, app(nb, pages, nb.lang, "", "new", t(nb.lang, "new_task"), new_main(nb, nb.lang, live), live), "text/html; charset=utf-8", {}
    m = SAFE.match(path)
    if not m:
        return 404, "not found", "text/plain; charset=utf-8", {}
    try:
        page = nb.page(m.group(1))
    except NotebookError:
        return 404, "not found", "text/plain; charset=utf-8", {}
    if m.group(3):
        run = next((r for r in page.runs() if r["n"] == int(m.group(3))), None)
        body = run_page(page.facts(run["n"]), run["folder"]) if run else None
        return (200, body, "text/html; charset=utf-8", {}) if body else (404, "not found", "text/plain; charset=utf-8", {})
    if m.group(4):
        entry = page.entries().get(m.group(4))
        text, ext = artifact_body(entry) if entry and entry["status"] == "valid" else (None, None)
        if text is not None and ext == ".txt" and m.group(5) == "html":  # the page a text file opens as
            return 200, reader_page(page, entry, text, nb.lang), "text/html; charset=utf-8", {"Content-Security-Policy": "sandbox allow-scripts"}
        if text is None or ext != "." + m.group(5):
            return 404, "not found", "text/plain; charset=utf-8", {}
        ctype = {".html": "text/html", ".svg": "image/svg+xml", ".txt": "text/plain"}[ext] + "; charset=utf-8"
        return 200, text, ctype, {"Content-Security-Policy": "sandbox allow-scripts"}  # a team's file runs in its own origin
    return 200, render_task(nb, page, pages, live), "text/html; charset=utf-8", {}


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):  # ThreadingHTTPServer is Python 3.7+
    daemon_threads = True
    allow_reuse_address = True


def serve(folder, host="127.0.0.1", port=8790, token=None, roots=(), ready=None):
    """Serve the notebook live: every GET draws the page from the records as they are; POST api/new and api/act take a
    person's command, with the token. ready(url) is called once it listens."""
    token = token or secrets.token_urlsafe(18)
    folder = os.path.abspath(os.path.expanduser(folder))

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code, body, ctype, headers=None):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            try:
                code, body, ctype, headers = route(folder, path, roots, True)
            except Exception as exc:  # noqa: BLE001 - a page that cannot be drawn is an error page, not a dead server
                code, body, ctype, headers = 500, f"{type(exc).__name__}: {exc}", "text/plain; charset=utf-8", {}
            self.reply(code, body, ctype, headers)

        def do_POST(self):
            def answer(code, obj):
                self.reply(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                return answer(403, {"error": "the token is missing or wrong"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > 20000:
                return answer(413, {"error": "too long"})
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except ValueError:
                return answer(400, {"error": "not JSON"})
            path = urllib.parse.urlparse(self.path).path
            nb = Notebook(folder)
            try:
                if path.endswith("/api/new"):
                    bring = body.get("bring") if isinstance(body.get("bring"), list) else []
                    event = nb.ask(str(body.get("goal") or ""), BY_WEB, title=str(body.get("title") or "") or None,
                                   bring=[b for b in bring if isinstance(b, dict)])
                elif path.endswith("/api/act"):
                    event = act(nb, body)
                else:
                    return answer(404, {"error": "no such command"})
            except NotebookError as exc:
                return answer(409, {"error": str(exc)})
            return answer(200, {"ok": True, "recorded": event})

    server = _Server((host, port), Handler)
    url = f"http://{host}:{server.server_address[1]}/#token={token}"
    if ready:
        ready(url)
    try:
        server.serve_forever()
    finally:
        server.server_close()
