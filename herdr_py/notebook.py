"""A notebook of pages: every piece of work a person hands to a team is a page. A page keeps its definition (goal, task,
judge, team, budget), every run, what its teams verified and the person's decisions, and the next run goes on from an
earlier one with what it verified, the skills its members wrote and the person's notes.

    python3 -m herdr_py.notebook NOTEBOOK draft PAGE.json      a new page, or a new version of one (a draft)
    python3 -m herdr_py.notebook NOTEBOOK approve PAGE         the draft, as it is now, may run
    python3 -m herdr_py.notebook NOTEBOOK run PAGE [--from N | --fresh] [--dry-run] [--detach]
    python3 -m herdr_py.notebook NOTEBOOK attach PAGE RUN_DIR [--from N]     a run made outside the notebook
    python3 -m herdr_py.notebook NOTEBOOK note PAGE TEXT        for the next run
    python3 -m herdr_py.notebook NOTEBOOK pick PAGE ENTRY       this version is the current one (of its kind)
    python3 -m herdr_py.notebook NOTEBOOK exclude|include PAGE ENTRY     what later runs may carry
    python3 -m herdr_py.notebook NOTEBOOK accept|hold|done|reopen PAGE
    python3 -m herdr_py.notebook NOTEBOOK status                what waits for a person, page by page
    python3 -m herdr_py.notebook NOTEBOOK view --out DIR [--runs DIR]...    the notebook as HTML pages

NOTEBOOK/notebook.json holds the title and the language of the pages (en or zh-TW). Each page is a folder
NOTEBOOK/pages/<id>/: page.json (its definition), history.jsonl (append-only: every draft, approval, run, note, pick,
exclusion and hold, with who and when) and runs/<n>/ (the engine's run folders; an attached run stays where it is and
is only read). The rules:
- a draft never runs: run refuses a page whose definition or task differs from what was last approved;
- a person's decisions are only appended (--by names who decided; --at writes an earlier time for a record made
  before the notebook existed, and marks the event imported);
- a run goes on from an earlier run of the page (the latest, unless --from or --fresh): the engine starts its
  knowledge base from that run's verified entries of the kinds the page carries, leaving out what a person excluded
  (engine --seed-from), and the notes no run has used yet are added to the task;
- every run is held to the page's budget (member turns, planner wakes, time limit); more needs an approved new version.
What waits for a person is worked out from these records, never stored: a draft to approve, a run that ended and that
nobody has looked at since (a note, a pick, an exclusion, accept, hold or done), and a run with no end that has written
nothing for 15 minutes.

The view is complete without JavaScript: a home page by day with what waits for a person on top; for every page its
team and its runs drawn, every version of what it made with the current one marked, its knowledge, notes and history;
every run's own page (engineview, dagview) and the files the teams made, copied next to them. --runs DIR lists the run
folders under DIR that no page holds.
Standard library only; Python 3.6+.
"""
import argparse
import collections
import hashlib
import json
import math
import os
import re
import shlex
import subprocess
import sys
import time

try:
    import fcntl
except ImportError:  # not POSIX: one writer at a time
    fcntl = None

from . import coopview, dagview, engineview
from .engineview import artifact_reads, esc, fit, read_jsonl
from .members import MemberError, parse_member
from .teamkb import TAG
from .viewstyle import TOKENS

ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
KINDS = ("engine", "dag", "other")
BUDGET = ("turns", "planner_wakes", "time_limit", "max_open", "turn_timeout")
SHOW = ("results", "failures", "answer_bytes")
LOOKED = ("approve", "note", "pick", "exclude", "include", "accept", "hold", "done")  # someone looked at the page
QUIET = 15 * 60  # a run with no end that has written nothing for this long is stuck
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # the folder herdr_py is in, for the engine's process

S = {  # (English, 繁體中文)
    "brand": ("herdr-py · notebook", "herdr-py · 筆記本"),
    "updated": ("updated {when}", "更新於 {when}"),
    "n_pages": ("{n} pages", "{n} 頁"),
    "n_runs": ("{n} runs", "{n} 次執行"),
    "n_wait": ("{n} waiting for you", "待你處理 {n} 件"),
    "inbox": ("Waiting for you", "待你處理"),
    "inbox_none": ("Nothing waits for you.", "沒有等你處理的事。"),
    "need_approve": ("approve it before it runs", "核准後才會執行"),
    "need_review": ("review run {n}", "驗收第 {n} 次執行"),
    "need_stuck": ("run {n} has written nothing for {m} min", "第 {n} 次執行已經 {m} 分鐘沒有動靜"),
    "st_draft": ("draft", "草稿"), "st_running": ("running", "執行中"), "st_stuck": ("stuck", "卡住"),
    "st_review": ("to review", "待驗收"), "st_approved": ("approved", "已核准"), "st_reviewed": ("reviewed", "已驗收"),
    "st_hold": ("on hold", "擱置"), "st_done": ("done", "完成"),
    "how": ("How a page works", "一頁怎麼運作"),
    "weekdays": ("Mon Tue Wed Thu Fri Sat Sun", "一 二 三 四 五 六 日"),
    "runs_that_day": ("runs {ns}", "第 {ns} 次執行"),
    "opened": ("opened", "開頁"),
    "no_run_yet": ("no run yet", "還沒執行"),
    "best": ("best {score}", "最高 {score}"),
    "tokens": ("{n} tokens", "{n} tokens"), "turns": ("{n} member turns", "{n} 個成員回合"),
    "no_end": ("no end recorded", "沒有結束紀錄"),
    "outputs_line": ("made: {items}", "成果：{items}"),
    "todo_review": ("Pick the current version of each output; to change something, write a note (the next run reads it); "
                    "if nothing needs to change, accept.",
                    "選定每種成果的現行版；要改的地方寫成批註（下一次執行會讀）；不用改就驗收。"),
    "todo_approve": ("Read the definition (the dry run shows the exact command and task), then approve it.",
                     "看過定義（試跑會列出確切的指令和任務）再核准。"),
    "outputs_above": ("{kinds}: see the versions above", "{kinds} 見上面的「成果：每一版」"),
    "steps": ("steps", "步驟"),
    "step_waiting": ("waiting", "等待"), "step_running": ("running", "執行中"), "step_judging": ("judging", "評分中"),
    "step_passed": ("passed", "通過"), "step_failed": ("failed", "失敗"), "step_blocked": ("blocked", "擋下"),
    "step_cut": ("cut off: the run has no end", "中斷：沒有結束紀錄"),
    "current": ("current", "現行版"),
    "no_pick": ("no current version picked", "還沒選現行版"),
    "suggest": ("highest so far", "目前最高"),
    "team": ("Team", "團隊"),
    "kb": ("Knowledge", "知識庫"),
    "kb_line": ("{n} verified entries ({kinds}); the next run carries {m}", "通過 {n} 條（{kinds}）；下一次會帶入 {m} 條"),
    "open_page": ("open the page", "打開這頁"),
    "back": ("← notebook", "← 筆記本"),
    "goal": ("Goal and definition", "目標與定義"),
    "asked": ("in your words", "你當初說的"),
    "task_file": ("task", "任務"), "judge": ("judge", "評分程式"), "budget": ("budget per run", "每次的預算"),
    "carry": ("carries on", "延續帶入"), "carry_all": ("every verified entry", "所有通過的條目"),
    "outputs": ("outputs", "成果"), "access": ("member access", "成員權限"), "folder": ("works in", "工作資料夾"),
    "approved_as": ("approved as {d} by {by} at {when}", "{when} 由 {by} 核准（版本 {d}）"),
    "not_approved": ("not approved as it is now (version {d})", "目前的版本 {d} 還沒核准"),
    "b_turns": ("{n} member turns", "成員回合 {n}"), "b_wakes": ("{n} planner wakes", "planner 喚醒 {n}"),
    "b_time": ("{n} s at most", "最多 {n} 秒"), "b_open": ("{n} open todos", "同時 {n} 個待辦"),
    "b_turn_timeout": ("{n} s a turn", "每回合 {n} 秒"),
    "chain": ("Runs, and what each carried from the one before", "每次執行，以及從前一次帶入什麼"),
    "runs": ("Every run", "每次執行"),
    "run_n": ("run {n}", "第 {n} 次"),
    "from_n": ("goes on from run {n}", "延續第 {n} 次"),
    "fresh": ("started fresh", "從頭開始"),
    "attached": ("attached (made outside the notebook)", "掛上（在筆記本外跑的）"),
    "carried": ("carried {n} ({kinds}); used {u} of them", "帶入 {n} 條（{kinds}）；用到 {u} 條"),
    "carried_none": ("carried nothing", "沒有帶入"),
    "made": ("made {n}: {v} valid, {i} invalid", "做了 {n} 條：{v} 通過、{i} 沒過"),
    "stopped": ("stopped: {why}", "停止原因：{why}"),
    "replay": ("replay", "回放"),
    "notes_used": ("notes used: {ids}", "用了批註：{ids}"),
    "st_run_ended": ("ended", "結束"), "st_run_running": ("running", "執行中"), "st_run_stuck": ("no end, quiet", "沒有結束紀錄、沒有動靜"),
    "st_run_missing": ("folder missing", "資料夾不見了"),
    "dag_steps": ("{passed} of {n} steps passed{failed}", "{n} 步通過 {passed} 步{failed}"),
    "dag_failed": (", {n} failed", "、失敗 {n} 步"),
    "versions": ("What it made: every version", "成果：每一版"),
    "kind_versions": ("{kind}: {n} versions", "{kind}：{n} 版"),
    "picked": ("picked by {by} at {when}", "{when} 由 {by} 選定"),
    "open": ("open", "打開"),
    "page_kb": ("The page's knowledge", "這頁的知識庫"),
    "skills": ("skills", "skill"),
    "made_in": ("made in run {n} by {member}", "第 {n} 次由 {member} 寫"),
    "carried_into": ("carried into runs {ns}", "帶入第 {ns} 次"),
    "built_on_by": ("built on {n} times", "被引用 {n} 次"),
    "excluded": ("excluded by {by}: {why}", "{by} 排除：{why}"),
    "notes": ("Notes for the next run", "批註（給下一次執行）"),
    "notes_none": ("No notes yet.", "還沒有批註。"),
    "note_used": ("used by run {n}", "第 {n} 次執行用了"),
    "note_next": ("goes into the next run", "下一次執行會帶入"),
    "history": ("History", "歷史"),
    "imported": ("recorded later", "補記"),
    "links": ("Elsewhere", "其他連結"),
    "say": ("Tell Claude, or run:", "跟 Claude 說，或執行："),
    "unattached": ("Team runs no page holds", "沒有歸屬到任何一頁的團隊執行"),
    "attach_rate": ("{m} of {n} team runs found under {roots} belong to a page ({p})",
                    "在 {roots} 找到 {n} 個團隊執行，{m} 個屬於某一頁（歸屬率 {p}）"),
    "ev_create": ("opened the page", "開頁"), "ev_revise": ("revised the definition", "改了定義"),
    "ev_approve": ("approved version {d}", "核准版本 {d}"), "ev_run": ("run {n} started", "第 {n} 次執行開始"),
    "ev_attach": ("attached run {n}: {dir}", "掛上第 {n} 次執行：{dir}"), "ev_run_end": ("run {n} ended (exit {code})", "第 {n} 次執行結束（結束碼 {code}）"),
    "ev_note": ("note {id}: {text}", "批註 {id}：{text}"), "ev_pick": ("picked {entry} as the current {kind}", "選 {entry} 當現行的 {kind}"),
    "ev_exclude": ("excluded {entry}: {why}", "排除 {entry}：{why}"), "ev_include": ("included {entry} again", "取消排除 {entry}"),
    "ev_accept": ("accepted: {why}", "驗收：{why}"), "ev_hold": ("put on hold: {why}", "擱置：{why}"),
    "ev_done": ("marked done: {why}", "完成：{why}"), "ev_reopen": ("reopened: {why}", "重新打開：{why}"),
    "notes_head": ("## Notes from the person this page is for (written after the earlier runs; follow them)",
                   "## 頁主人的批註（看過前面的執行後寫的，這次要照做）"),
    "life": ("you: a goal in a line|Claude drafts: task, judge, team, budget|you approve|the team runs (run n)|"
             "you review: pick, note, exclude|the next run carries what passed, the skills and your notes",
             "你：一句話的目標|Claude 起草：任務、評分、團隊、預算|你核准|團隊執行（第 n 次）|"
             "你驗收：選現行版、批註、排除|下一次帶入通過的條目、skill 與你的批註"),
    "t_planner": ("planner", "planner"), "t_todos": ("todos", "待辦"), "t_answers": ("answers", "答案"),
    "t_judge": ("judge", "評分"), "t_verified": ("what passed", "通過的"), "t_next": ("next run", "下一次"),
    "t_members": ("members", "成員"),
}


def say(lang, key, **kw):
    return S[key][1 if lang == "zh-TW" else 0].format(**kw)


class NotebookError(ValueError):
    pass


def now():
    return round(time.time(), 3)


def local(t, fmt="%m-%d %H:%M"):
    return time.strftime(fmt, time.localtime(t)) if t else "?"


def day_of(t):
    return time.strftime("%Y-%m-%d", time.localtime(t))


def tokens_text(n):
    if n is None:
        return "?"
    return f"{n / 1e6:.2f}M" if n >= 1e6 else (f"{n / 1e3:.1f}k" if n >= 1e3 else str(int(n)))


def span_text(seconds, lang):
    s = int(round(max(0, seconds or 0)))
    m, s = divmod(s, 60)
    if lang == "zh-TW":
        return f"{m} 分 {s:02d} 秒" if m else f"{s} 秒"
    return f"{m}m {s:02d}s" if m else f"{s}s"


def score_text(v):
    return "?" if v is None else f"{v:.6g}"


def load_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def check_definition(d):
    """The problems with a page definition (page.json), or []."""
    if not isinstance(d, dict):
        return ["page.json: a JSON object"]
    problems = []
    if not ID.match(str(d.get("id", ""))):
        problems.append("id: lower-case letters, digits and -, up to 64 (the page's folder name)")
    for key in ("title", "goal"):
        if not isinstance(d.get(key), str) or not d[key].strip():
            problems.append(f"{key}: say it in a line")
    if not re.match(r"^\d{4}-\d\d-\d\d$", str(d.get("day", ""))):
        problems.append("day: the notebook day the page was opened, YYYY-MM-DD")
    kind = d.get("kind", "engine")
    if kind not in KINDS:
        problems.append(f"kind: one of {', '.join(KINDS)}")
    for key in ("carry", "outputs"):
        if key in d and not (isinstance(d[key], list) and all(isinstance(x, str) and x for x in d[key])):
            problems.append(f"{key}: a list of kinds")
    links = d.get("links", [])
    if not (isinstance(links, list) and all(isinstance(x, dict) and x.get("label") and x.get("url") for x in links)):
        problems.append("links: a list of {label, url}")
    if kind != "engine":
        return problems
    if not isinstance(d.get("task"), str) or not d["task"].strip():
        problems.append("task: the task file the team reads (from cwd)")
    judge = d.get("judge")
    if not (isinstance(judge, list) and judge and all(isinstance(x, str) and x for x in judge)):
        problems.append("judge: the judge's command as a list of words ({kb} and {run} name this run's folders)")
    if "asked" in d and not (isinstance(d["asked"], str) and d["asked"].strip()):
        problems.append("asked: what the person said, in their words")
    for key in ("cwd", "socket"):
        if key in d and not (isinstance(d[key], str) and d[key].strip()):
            problems.append(f"{key}: a folder or file path")
    if d.get("judge_mode", "json") not in ("json", "exit"):
        problems.append("judge_mode: json or exit")
    team = d.get("team") if isinstance(d.get("team"), dict) else {}
    try:
        planner = parse_member(str(team.get("planner", "")))
        members = [parse_member(str(m)) for m in team.get("members") or []]
        names = [m["name"] for m in members]
        if not members:
            problems.append("team.members: at least one NAME=BACKEND[:MODEL]")
        if planner["name"] in names:
            problems.append(f"team.planner: {planner['name']} is also a member")
        if len(set(names)) != len(names):
            problems.append("team.members: two members with one name")
        unknown = sorted(set(team.get("about") or {}) - set(names))
        if unknown:
            problems.append(f"team.about: no member called {', '.join(unknown)}")
    except MemberError as exc:
        problems.append(f"team: {exc}")
    if team.get("access", "read") not in ("read", "research"):
        problems.append("team.access: read or research")
    budget = d.get("budget") if isinstance(d.get("budget"), dict) else {}
    if "turns" not in budget:
        problems.append("budget.turns: the member turns a run may use")
    for key, value in budget.items():
        if key not in BUDGET:
            problems.append(f"budget.{key}: not a budget (one of {', '.join(BUDGET)})")
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0 or \
                (key != "time_limit" and int(value) != value):
            problems.append(f"budget.{key}: a positive number" + ("" if key == "time_limit" else " (whole)"))
    show = d.get("show") if isinstance(d.get("show"), dict) else {}
    for key, value in show.items():
        if key not in SHOW or isinstance(value, bool) or not isinstance(value, int) or value < 0:
            problems.append(f"show.{key}: one of {', '.join(SHOW)}, a whole number")
    return problems


class Notebook:
    def __init__(self, folder):
        self.folder = os.path.abspath(os.path.expanduser(folder))
        meta = load_json(os.path.join(self.folder, "notebook.json")) or {}
        self.title = meta.get("title") or "Notebook"
        self.lang = meta.get("lang") if meta.get("lang") in ("en", "zh-TW") else "en"

    def ids(self):
        root = os.path.join(self.folder, "pages")
        if not os.path.isdir(root):
            return []
        return sorted(p for p in os.listdir(root) if ID.match(p) and os.path.isfile(os.path.join(root, p, "page.json")))

    def page(self, pid):
        if not ID.match(str(pid)) or not os.path.isfile(os.path.join(self.folder, "pages", pid, "page.json")):
            raise NotebookError(f"no page {pid!r} in {self.folder}")
        return Page(self, pid)

    def pages(self):
        return [Page(self, p) for p in self.ids()]


class Page:
    def __init__(self, notebook, pid):
        self.notebook, self.id = notebook, pid
        self.dir = os.path.join(notebook.folder, "pages", pid)
        with open(os.path.join(self.dir, "page.json"), "rb") as handle:
            self.raw = handle.read()
        try:
            self.d = json.loads(self.raw.decode("utf-8"))
        except ValueError as exc:
            raise NotebookError(f"{pid}/page.json: {exc}")
        self.history = read_jsonl(os.path.join(self.dir, "history.jsonl"))
        self._facts = {}
        self.files = {}  # entry id -> the file name the view wrote for it

    # ---- the definition
    def cwd(self):
        return os.path.expanduser(self.d.get("cwd") or self.dir)

    def task_path(self):
        task = os.path.expanduser(self.d.get("task") or "")
        return task if os.path.isabs(task) else os.path.join(self.cwd(), task)

    def digest(self):
        """What a person approves: the definition and the task the team will read, together."""
        h = hashlib.sha256(self.raw)
        if self.d.get("task"):
            try:
                with open(self.task_path(), "rb") as handle:
                    h.update(b"\0task\0" + handle.read())
            except OSError:
                h.update(b"\0no task file")
        return h.hexdigest()[:12]

    def approval(self):
        """The approval of the definition as it is now, or None."""
        last = next((e for e in reversed(self.history) if e.get("kind") == "approve"), None)
        return last if last and last.get("digest") == self.digest() else None

    # ---- the history (append-only)
    def append(self, kind, by, at=None, **fields):
        event = dict(fields, kind=kind, by=by, t=now())
        if at is not None:
            event.update(t=round(float(at), 3), recorded=now(), imported=True)
        line = (json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        path = os.path.join(self.dir, "history.jsonl")
        with open(path + ".lock", "a") as lock:
            if fcntl:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                with open(path, "ab") as handle:
                    handle.write(line)
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                if fcntl:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        self.history = read_jsonl(path)
        return event

    def events(self, *kinds):
        return [e for e in self.history if e.get("kind") in kinds]

    def runs(self):
        """Every run of the page, in order, with its folder."""
        ends = {e.get("n"): e for e in self.events("run_end")}
        out = []
        for e in sorted(self.events("run"), key=lambda e: e.get("n") or 0):
            folder = os.path.expanduser(e.get("dir") or "")
            out.append(dict(e, folder=folder if os.path.isabs(folder) else os.path.join(self.dir, folder),
                            end_event=ends.get(e.get("n"))))
        return out

    def facts(self, n):
        if n not in self._facts:
            run = next(r for r in self.runs() if r["n"] == n)
            self._facts[n] = run_facts(run["folder"])
        return self._facts[n]

    def notes(self):
        used = {}
        for r in self.events("run"):
            for nid in r.get("notes") or []:
                used.setdefault(nid, r.get("n"))
        return [dict(e, used_by=used.get(e.get("id"))) for e in self.events("note")]

    def excluded(self):
        """{entry: its exclude event} for entries a person excluded and did not include again."""
        out = {}
        for e in self.events("exclude", "include"):
            if e["kind"] == "exclude":
                out[e.get("entry")] = e
            else:
                out.pop(e.get("entry"), None)
        return out

    def picks(self):
        """{kind: the latest pick event of that kind}."""
        out = {}
        for e in self.events("pick"):
            out[e.get("of")] = e
        return out

    # ---- what waits for a person
    def state(self, at=None):
        """(state, needs): the page's state and what waits for a person: ("approve", None), ("review", n), ("stuck", n)."""
        at = at or time.time()
        runs = self.runs()
        last = runs[-1] if runs else None
        lf = self.facts(last["n"]) if last else None
        closed = None
        for e in self.events("hold", "done", "reopen"):
            closed = None if e["kind"] == "reopen" else e
        if closed and last and last["t"] > closed["t"]:
            closed = None  # a run after a hold opened the page again
        if lf and lf["state"] == "running":
            return "running", []
        if lf and lf["state"] == "stuck" and not closed:
            return "stuck", [("stuck", last["n"])]
        if closed:
            return closed["kind"], []
        needs = []
        approved = self.approval() is not None
        changed = last is not None and last.get("digest") not in (None, self.digest())
        if not approved and (not runs or changed):
            needs.append(("approve", None))
        if lf and lf["state"] in ("ended", "stuck", "missing", "unknown"):
            end = lf.get("end") or last["t"]
            if not any(e["t"] > end for e in self.events(*LOOKED)):
                needs.append(("review", last["n"]))
        if ("approve", None) in needs:
            return "draft", needs
        if needs:
            return "review", needs
        return ("approved" if approved else "reviewed"), needs

    # ---- versions and knowledge across runs
    def entries(self):
        """{id: entry} over every run: where it was made, which runs carried it, who built on it."""
        out = {}
        for r in self.runs():
            f = self.facts(r["n"])
            for e in f.get("made", []):
                out.setdefault(e["id"], dict(e, run=r["n"], carried_into=[], built_on=0, folder=r["folder"],
                                              file=self.files.get(e["id"])))
            for e in f.get("carried", []):
                if e["id"] in out:
                    out[e["id"]]["carried_into"].append(r["n"])
            for e in f.get("made", []):
                for p in e.get("parents") or []:
                    if p in out:
                        out[p]["built_on"] += 1
        return out

    def next_carry(self):
        """The entries the next run would carry (from the latest run): verified, of the carried kinds, not excluded."""
        runs = self.runs()
        if not runs:
            return []
        f = self.facts(runs[-1]["n"])
        kinds, skip = self.d.get("carry"), self.excluded()
        return [e for e in f.get("carried", []) + f.get("made", [])
                if e["status"] == "valid" and (not kinds or e["kind"] in kinds) and e["id"] not in skip]


def run_facts(folder, at=None):
    """What a run folder says: its type, when it started and ended, its state, what it cost and what it made."""
    at = at or time.time()
    if os.path.isfile(os.path.join(folder, "engine.jsonl")):
        return engine_facts(folder, at)
    if os.path.isfile(os.path.join(folder, "plan.json")) and os.path.isfile(os.path.join(folder, "events.jsonl")):
        return dag_facts(folder, at)
    if os.path.isfile(os.path.join(folder, "run.jsonl")) and os.path.isdir(os.path.join(folder, "kb")):
        return coop_facts(folder, at)
    return {"type": "other", "state": "missing" if not os.path.isdir(folder) else "unknown", "start": None, "end": None,
            "seconds": None, "tokens": None, "member_turns": 0, "made": [], "carried": [], "used": [], "members": []}


def last_write(*paths):
    return max((os.path.getmtime(p) for p in paths if os.path.exists(p)), default=0)


def with_files(folder, made):
    """engineview's entries, each with the artifact file its proposal names (kb/artifacts/...), so it can be opened."""
    files = {e.get("id"): e.get("artifact") for e in read_jsonl(os.path.join(folder, "kb", "events.jsonl"))
             if e.get("type") == "propose"}
    return [dict(e, artifact=files.get(e["id"])) for e in made]


def engine_facts(folder, at):
    engine, turns, todos, summary, made, tools = engineview.load(folder)
    made = with_files(folder, made)
    start = next((r for r in engine if r.get("kind") == "start"), {})
    stop = next((r for r in reversed(engine) if r.get("kind") == "stop"), None)
    t0 = start.get("t")
    quiet = last_write(*(os.path.join(folder, x) for x in ("engine.jsonl", "run.jsonl", os.path.join("kb", "events.jsonl"))))
    state = "ended" if (stop or summary) else ("running" if at - quiet < QUIET else "stuck")
    t1 = stop["t"] if stop else max([t0 or 0] + [x.get("end") or x.get("t") or 0 for x in turns + engine])
    carried = [e for e in made if t0 is not None and e.get("t") is not None and e["t"] < t0]
    old = {e["id"] for e in carried}
    new = [e for e in made if e["id"] not in old]
    opened = {eid for _, eid, _ in artifact_reads(tools)}
    built_on = {p for e in new for p in e.get("parents") or []}
    wakes = [r for r in engine if r.get("kind") == "wake"]
    tokens = [x["tokens"] for x in turns + wakes if isinstance(x.get("tokens"), (int, float))]
    valid = [e for e in new if e["status"] == "valid"]
    best = max(valid, key=lambda e: (e["score"], -(e.get("t") or 0)), default=None)
    return {"type": "engine", "state": state, "start": t0, "end": t1, "seconds": (t1 - t0) if t0 else None,
            "members": start.get("members") or sorted({t.get("member") for t in turns if t.get("member")}),
            "planner": start.get("planner"), "turn_budget": start.get("turns"), "member_turns": len(turns),
            "planner_turns": len(wakes), "tokens": sum(tokens) if tokens else None,
            "stopped": stop.get("why") if stop else None, "made": new, "carried": carried,
            "used": [e["id"] for e in carried if e["id"] in built_on or e["id"] in opened], "best": best,
            "valid": len(valid), "invalid": sum(1 for e in new if e["status"] == "invalid"),
            "todos": len(todos), "seeded": start.get("seeded")}


def coop_facts(folder, at):
    """A coop run (rounds): no start record, so it starts when its first turn did."""
    _, turns, _, summary, made, _ = engineview.load(folder)
    made = with_files(folder, made)
    starts = [(x.get("t") or 0) - (x.get("seconds") or 0) for x in turns if x.get("t")]
    t0 = min(starts) if starts else None
    t1 = max([x.get("t") or 0 for x in turns] or [t0 or 0])
    quiet = last_write(os.path.join(folder, "run.jsonl"), os.path.join(folder, "kb", "events.jsonl"))
    state = "ended" if summary else ("running" if at - quiet < QUIET else "stuck")
    tokens = [x["tokens"] for x in turns if isinstance(x.get("tokens"), (int, float))]
    valid = [e for e in made if e["status"] == "valid"]
    return {"type": "coop", "state": state, "start": t0, "end": t1, "seconds": (t1 - t0) if t0 else None,
            "members": (summary or {}).get("members") or sorted({x.get("member") for x in turns if x.get("member")}),
            "member_turns": len(turns), "tokens": (summary or {}).get("tokens") or (sum(tokens) if tokens else None),
            "made": made, "carried": [], "used": [], "valid": len(valid),
            "invalid": sum(1 for e in made if e["status"] == "invalid"),
            "best": max(valid, key=lambda e: (e["score"], -(e.get("t") or 0)), default=None),
            "stopped": (summary or {}).get("stopped")}


def dag_facts(folder, at):
    try:
        plan, events, summary = dagview.load(folder)
    except (OSError, ValueError, KeyError):
        return run_facts(os.path.join(folder, "\0none"), at)
    st = dagview.states(plan, events)
    start = next((e for e in events if e.get("kind") == "run.start"), {})
    end = next((e for e in reversed(events) if e.get("kind") == "run.end"), None)
    quiet = last_write(os.path.join(folder, "events.jsonl"))
    state = "ended" if (end or summary) else ("running" if at - quiet < QUIET else "stuck")
    t0 = start.get("t")
    t1 = end["t"] if end else max([t0 or 0] + [e.get("t") or 0 for e in events])
    counts = collections.Counter(s["state"] for s in st.values())
    return {"type": "dag", "state": state, "start": t0, "end": t1, "seconds": (t1 - t0) if t0 else None,
            "members": sorted({e.get("member") for e in events if e.get("member")}), "plan": plan.get("name"),
            "steps": list(plan.get("order") or []), "step_states": {k: v["state"] for k, v in st.items()},
            "passed": counts["passed"], "failed": counts["failed"], "tokens": None, "member_turns":
            sum(1 for e in events if e.get("kind") == "node.dispatch"), "made": [], "carried": [], "used": [],
            "stopped": next((e.get("why") for e in events if e.get("kind") == "run.stop"), None)}


# ---- commands
def read_text(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def draft(nb, source, by, at=None):
    """Install a page definition (a new page, or a new version of one): it is a draft until approved."""
    try:
        with open(source, "rb") as handle:
            raw = handle.read()
        d = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise NotebookError(f"{source}: {exc}")
    problems = check_definition(d)
    if problems:
        raise NotebookError("; ".join(problems))
    folder = os.path.join(nb.folder, "pages", d["id"])
    path = os.path.join(folder, "page.json")
    existed = os.path.isfile(path)
    if existed:
        with open(path, "rb") as handle:
            if handle.read() == raw:
                raise NotebookError(f"{d['id']}: this definition is already the page's")
    os.makedirs(folder, exist_ok=True)
    with open(path + ".tmp", "wb") as handle:
        handle.write(raw)
    os.replace(path + ".tmp", path)
    page = nb.page(d["id"])
    page.append("revise" if existed else "create", by, at=at, digest=page.digest())
    return page


def approve(page, by, at=None):
    problems = check_definition(page.d)
    if problems:
        raise NotebookError("the definition has problems: " + "; ".join(problems))
    if page.approval():
        raise NotebookError(f"{page.id}: version {page.digest()} is already approved")
    return page.append("approve", by, at=at, digest=page.digest(), definition=page.d)


def task_with_notes(page, notes):
    task = read_text(page.task_path())
    if not notes:
        return task
    lines = [say(page.notebook.lang, "notes_head")]
    lines += [f"- ({local(n['t'])}, {n['by']}) {n['text']}" for n in notes]
    return task.rstrip("\n") + "\n\n" + "\n".join(lines) + "\n"


def run_argv(page, folder, task_file, source=None):
    d, team, budget = page.d, page.d["team"], page.d.get("budget") or {}
    judge = [w.replace("{kb}", os.path.join(folder, "kb")).replace("{run}", folder) for w in d["judge"]]
    argv = [sys.executable, "-m", "herdr_py.engine", "--task", task_file, "--judge", " ".join(shlex.quote(w) for w in judge),
            "--judge-mode", d.get("judge_mode", "json"), "--planner", team["planner"], "--out", folder,
            "--member-access", team.get("access", "read")]
    if d.get("judge_timeout"):
        argv += ["--judge-timeout", str(int(d["judge_timeout"]))]
    if d.get("socket"):  # OpenCode members talk to herdr-py's daemon
        argv += ["--socket", os.path.expanduser(d["socket"])]
    for m in team["members"]:
        argv += ["--member", m]
    for name, text in (team.get("about") or {}).items():
        argv += ["--about", f"{name}={text}"]
    for key in BUDGET:
        if key in budget:
            argv += ["--" + key.replace("_", "-"), f"{budget[key]:g}" if key == "time_limit" else str(int(budget[key]))]
    for key, flag in (("results", "--show-results"), ("failures", "--show-failures"), ("answer_bytes", "--answer-bytes")):
        if key in (d.get("show") or {}):
            argv += [flag, str(d["show"][key])]
    if source is not None:
        argv += ["--seed-from", source]
        for kind in d.get("carry") or []:
            argv += ["--carry", kind]
        for eid in sorted(page.excluded()):
            argv += ["--seed-skip", eid]
    return argv


def start_run(page, by, source_n=None, fresh=False, dry=False, detach=False, out=sys.stdout):
    """Run the page once more with the engine, from its latest run unless told otherwise. Returns the engine's exit
    code (None when detached or a dry run)."""
    problems = check_definition(page.d)
    if problems:
        raise NotebookError("the definition has problems: " + "; ".join(problems))
    if page.d.get("kind", "engine") != "engine":
        raise NotebookError(f"{page.id} is a {page.d.get('kind')} page: run it outside and attach the run")
    if page.approval() is None:
        raise NotebookError(f"{page.id} is a draft: version {page.digest()} (the definition and the task as they are now) "
                            "is not approved; approve it first")
    runs = page.runs()
    going = [r["n"] for r in runs if page.facts(r["n"])["state"] == "running"]
    if going:
        raise NotebookError(f"{page.id}: run {going[0]} is still going")
    source = None
    if not fresh and runs:
        n = source_n if source_n is not None else runs[-1]["n"]
        source = next((r for r in runs if r["n"] == n), None)
        if source is None:
            raise NotebookError(f"{page.id}: no run {n}")
        if page.facts(n)["type"] != "engine":
            raise NotebookError(f"{page.id}: run {n} is not an engine run; it cannot be carried on (--fresh)")
    elif source_n is not None:
        raise NotebookError("--from and --fresh: one or the other")
    n = max((r["n"] for r in runs), default=0) + 1
    folder = os.path.join(page.dir, "runs", str(n))
    notes = [x for x in page.notes() if x["used_by"] is None]
    task = task_with_notes(page, notes)
    task_file = os.path.join(folder, "task.md")
    argv = run_argv(page, folder, task_file, source["folder"] if source else None)
    if dry:
        out.write(f"run {n} of {page.id}" + (f", going on from run {source['n']}" if source else ", from nothing") + "\n")
        out.write("$ " + " ".join(shlex.quote(a) for a in argv) + f"\n(in {page.cwd()})\n\n--- the task\n{task}")
        return None
    if os.path.exists(folder):
        raise NotebookError(f"{folder} already exists")
    os.makedirs(folder)
    with open(task_file, "w", encoding="utf-8") as handle:
        handle.write(task)
    page.append("run", by, n=n, dir=os.path.join("runs", str(n)), digest=page.digest(), notes=[x["id"] for x in notes],
                budget=page.d.get("budget"), **{"from": source["n"] if source else None})
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([ROOT] + [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]))
    if detach:
        with open(os.path.join(folder, "console.log"), "ab") as log:
            subprocess.Popen(argv, cwd=page.cwd(), env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                             start_new_session=True)
        return None
    with open(os.path.join(folder, "console.log"), "ab") as log:  # the engine's report, kept with the run
        code = subprocess.call(argv, cwd=page.cwd(), env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
    page.append("run_end", "notebook", n=n, exit=code)
    with open(os.path.join(folder, "console.log"), encoding="utf-8", errors="replace") as log:
        out.write(f"run {n} of {page.id} ended (exit {code}); {folder}\n" + "".join(log.readlines()[-12:]))
    return code


def attach(page, folder, by, source_n=None, at=None, note=None):
    folder = os.path.abspath(os.path.expanduser(folder))
    facts = run_facts(folder)
    if facts["type"] == "other":
        raise NotebookError(f"{folder}: not a run folder (no engine.jsonl, nor plan.json with events.jsonl)")
    runs = page.runs()
    if any(os.path.realpath(r["folder"]) == os.path.realpath(folder) for r in runs):
        raise NotebookError(f"{folder} is already a run of {page.id}")
    if source_n is not None and not any(r["n"] == source_n for r in runs):
        raise NotebookError(f"{page.id}: no run {source_n}")
    n = max((r["n"] for r in runs), default=0) + 1
    extra = {"note": note} if note else {}
    return page.append("run", by, at=at if at is not None else facts.get("start"), n=n, dir=folder, attached=True,
                       **dict(extra, **{"from": source_n}))


def find_entry(page, eid):
    entry = page.entries().get(eid)
    if entry is None:
        raise NotebookError(f"{page.id}: no entry {eid} in its runs")
    return entry


def pick(page, eid, by, at=None):
    entry = find_entry(page, eid)
    if entry["status"] != "valid":
        raise NotebookError(f"{eid} did not pass its judge ({entry['status']}); only a verified version can be current")
    return page.append("pick", by, at=at, entry=eid, run=entry["run"], of=entry["kind"])


def add_note(page, text, by, at=None):
    if not text.strip():
        raise NotebookError("a note says something")
    return page.append("note", by, at=at, id=f"n{len(page.events('note')) + 1}", text=text.strip())


# ---- the view
STYLE = TOKENS + """* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.55 -apple-system,"SF Pro Text","PingFang TC","Noto Sans TC",sans-serif;
  padding-inline:16px; padding-block:16px 40px; -webkit-font-smoothing:antialiased; }
main { max-width:1080px; margin:0 auto; display:flex; flex-direction:column; gap:14px; }
a { color:var(--ice); text-decoration:none; } a:hover { text-decoration:underline; }
.panel { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:14px; min-width:0;
  box-shadow:inset 0 1px 0 var(--hi), 0 10px 30px rgba(0,0,0,.6); }
.top { display:flex; flex-direction:column; gap:6px; }
.brand { font-size:.72rem; letter-spacing:.08em; text-transform:uppercase; color:var(--faint); }
h1 { font-size:1.3rem; margin:0; overflow-wrap:anywhere; font-weight:650; text-wrap:balance; }
h2 { font-size:.78rem; margin:0 0 10px; letter-spacing:.06em; color:var(--muted); font-weight:600; }
h3 { font-size:1rem; margin:0; font-weight:620; overflow-wrap:anywhere; }
.muted, .state { color:var(--muted); font-size:.86rem; overflow-wrap:anywhere; }
.faint { color:var(--faint); font-size:.8rem; }
.chips { display:flex; flex-wrap:wrap; gap:6px; }
.chip { font-size:.76rem; color:var(--muted); background:var(--inset); border:1px solid var(--line); border-radius:999px;
  padding:1px 9px; overflow-wrap:anywhere; font-variant-numeric:tabular-nums; }
.chip.good { color:#7FD8B6; border-color:rgba(16,185,129,.35); } .chip.bad { color:#F3A6A6; border-color:rgba(239,68,68,.35); }
.chip.warn { color:#E9C27A; border-color:rgba(212,162,76,.4); } .chip.ice { color:var(--ice); border-color:rgba(147,197,253,.35); }
.led { display:inline-block; width:6px; height:6px; border-radius:50%; background:var(--faint); margin-right:7px; vertical-align:2px; flex:none; }
.led.ok { background:var(--ok); } .led.bad { background:var(--bad); } .led.warn { background:var(--amber); }
.led.run { background:var(--ink); animation:breathe 1.6s ease-in-out infinite; }
@keyframes breathe { 50% { opacity:.3; } }
.inbox { border-color:rgba(212,162,76,.35); }
.items { list-style:none; margin:0; padding:0; display:flex; flex-direction:column; gap:10px; min-width:0; }
.items > li { background:var(--inset); border:1px solid var(--line); border-radius:10px; padding:10px 12px; overflow-wrap:anywhere;
  display:flex; flex-direction:column; gap:5px; min-width:0; }
.items > li > *, .day > * { min-width:0; max-width:100%; }
.row { display:flex; flex-wrap:wrap; align-items:baseline; gap:6px 10px; }
.cmd { font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace; color:var(--muted); background:var(--bg); border:1px solid var(--line);
  border-radius:6px; padding:5px 8px; white-space:pre-wrap; overflow-wrap:anywhere; max-width:100%; }
.roles { list-style:none; margin:10px 0 0; padding:0; font-size:.84rem; display:flex; flex-direction:column; gap:4px; }
.roles li { overflow-wrap:anywhere; } .roles b { font-weight:600; }
.kv { display:grid; grid-template-columns:minmax(5.5em,max-content) minmax(0,1fr); gap:4px 12px; margin:0; font-size:.88rem; }
.kv dt { color:var(--faint); } .kv dd { margin:0; overflow-wrap:anywhere; }
pre.task { font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace; color:var(--muted); background:var(--inset);
  border:1px solid var(--line); border-radius:8px; padding:8px 10px; max-height:16em; overflow:auto; white-space:pre-wrap; margin:8px 0 0; }
.grid2 { display:grid; gap:14px; }
@media (min-width:900px) { .grid2 { grid-template-columns:minmax(0,1fr) minmax(0,1fr); align-items:start; } }
.fig { overflow-x:auto; } .fig svg { display:block; margin:0 auto; width:100%; height:auto; }
.box { fill:var(--inset); stroke:var(--wire); stroke-width:1; } .box.me { fill:var(--amber-bg); stroke:var(--amber); }
.box.team { fill:var(--ice-bg); stroke:var(--ice); } .box.ok { fill:var(--ok-bg); stroke:var(--ok); }
.box.bad { fill:var(--bad-bg); stroke:var(--bad); } .box.dash { stroke-dasharray:4 3; }
.tx { fill:var(--ink); font-size:12px; } .tx.b { font-weight:600; } .tx.s { fill:var(--muted); font-size:10.5px; }
.ar { fill:none; stroke:var(--wire); stroke-width:1.3; } .ar.back { stroke-dasharray:5 4; } .ar.carry { stroke:var(--ok); }
.ah { fill:var(--wire); } .ah.carry { fill:var(--ok); } .al { fill:var(--muted); font-size:10.5px; }
.legend { display:flex; flex-wrap:wrap; gap:4px 14px; font-size:.76rem; color:var(--muted); margin-top:8px; }
.legend i { display:inline-block; width:12px; height:9px; border-radius:3px; border:1px solid var(--faint); margin-right:5px; vertical-align:-1px; }
.legend i.me { background:var(--amber-bg); border-color:var(--amber); } .legend i.team { background:var(--ice-bg); border-color:var(--ice); }
.legend i.ok { background:var(--ok-bg); border-color:var(--ok); } .legend i.carry { border:0; border-top:2px solid var(--ok); height:0; border-radius:0; vertical-align:3px; }
.vers { list-style:none; margin:6px 0 0; padding:0; display:flex; flex-direction:column; gap:4px; font-size:.86rem; }
.vers li { display:flex; flex-wrap:wrap; gap:4px 10px; align-items:baseline; padding:5px 8px; border-radius:8px; border:1px solid transparent; }
.vers li.cur { border-color:rgba(16,185,129,.4); background:var(--ok-bg); }
.mono { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.8rem; }
.hist { list-style:none; margin:0; padding:0; font-size:.84rem; }
.hist li { padding:5px 0; border-top:1px solid var(--line); overflow-wrap:anywhere; } .hist li:first-child { border-top:0; }
.day { display:flex; flex-direction:column; gap:10px; }
details summary { cursor:pointer; color:var(--muted); font-size:.86rem; overflow-wrap:anywhere; }
@media (prefers-reduced-motion:reduce) { .led.run { animation:none; } }
"""

SHELL = """<!doctype html>
<html lang="{lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
{style}</style></head>
<body><main>
{body}
</main></body></html>
"""

STATE_LED = {"draft": "warn", "running": "run", "stuck": "bad", "review": "warn", "approved": "ok", "reviewed": "ok",
             "hold": "", "done": "ok"}
STATE_CHIP = {"draft": "warn", "running": "ice", "stuck": "bad", "review": "warn", "approved": "good", "reviewed": "good",
              "hold": "", "done": "good"}


def day_label(day, lang):
    t = time.mktime(time.strptime(day, "%Y-%m-%d"))
    wd = say(lang, "weekdays").split()[time.localtime(t).tm_wday]
    return f"{day}（{wd}）" if lang == "zh-TW" else f"{wd} {day}"


def need_text(lang, need, page):
    what, n = need
    if what == "approve":
        return say(lang, "need_approve")
    if what == "review":
        return say(lang, "need_review", n=n)
    f = page.facts(n)
    return say(lang, "need_stuck", n=n, m=int((time.time() - (f.get("end") or 0)) // 60))


def tilde(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if str(path).startswith(home + os.sep) else str(path)


def short(path):
    """A path as a person types it: the home folder as ~ (left unquoted, so the shell expands it)."""
    home = os.path.expanduser("~")
    return "~/" + shlex.quote(path[len(home) + 1:]) if path.startswith(home + os.sep) else shlex.quote(path)


def command(nb, *words):
    return "python3 -m herdr_py.notebook " + " ".join([short(nb.folder)] + [shlex.quote(w) for w in words])


def cmd_html(nb, *words):
    return f'<div class="cmd">{esc(command(nb, *words))}</div>'


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


def best_of(versions):
    return max(versions, key=lambda e: (e["score"], e.get("t") or 0), default=None)


def export_file(entry, files_dir):
    """Copy an entry's artifact for people to open: its kind line taken off, HTML kept as a page. Returns its name."""
    src = os.path.join(entry["folder"], "kb", entry["artifact"]) if entry.get("artifact") else None
    if not src or not os.path.isfile(src):
        return None
    with open(src, "rb") as handle:
        text = handle.read().decode("utf-8", "replace")
    first, sep, rest = text.partition("\n")
    if TAG.match(first):
        text = rest
    head = text.lstrip()[:200].lower()
    ext = ".html" if head.startswith(("<!doctype html", "<html")) else (".svg" if head.startswith("<svg") else ".txt")
    name = entry["id"] + ext
    os.makedirs(files_dir, exist_ok=True)
    with open(os.path.join(files_dir, name + ".tmp"), "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(os.path.join(files_dir, name + ".tmp"), os.path.join(files_dir, name))
    return name


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


def box(x, y, w, h, cls, title, sub=None, size=12):
    out = f'<rect class="box {cls}" x="{x}" y="{y}" width="{w}" height="{h}" rx="7"/>'
    ty = y + (h / 2 + 4 if not sub else h / 2 - 3)
    out += f'<text class="tx b" x="{x + 10}" y="{ty:.1f}">{esc(fit(title, w - 20, size))}</text>'
    if sub:
        out += f'<text class="tx s" x="{x + 10}" y="{ty + 15:.1f}">{esc(fit(sub, w - 20, 10.5))}</text>'
    return out


def life_svg(lang):
    """How a page works: who does each step, and the loop from a review to the next run."""
    steps = say(lang, "life").split("|")
    who = ["me", "", "me", "team", "me", "team"]
    W, x, w, h, gap = 360, 16, 262, 34, 16
    parts, y = [], 8
    for i, (text, cls) in enumerate(zip(steps, who)):
        parts.append(box(x, y, w, h, cls, text))
        if i < len(steps) - 1:
            parts.append(arrow(x + w / 2, y + h, x + w / 2, y + h + gap))
        y += h + gap
    top = 8 + 3 * (h + gap) + h / 2  # the run
    bottom = 8 + 5 * (h + gap) + h / 2  # the next run
    parts.append(f'<path class="ar back" d="M{x + w},{bottom:.1f} H{x + w + 34} V{top:.1f} H{x + w + 7}"/>')
    parts.append(f'<polygon class="ah" points="{x + w},{top:.1f} {x + w + 7},{top - 3.5:.1f} {x + w + 7},{top + 3.5:.1f}"/>')
    parts.append(f'<text class="al" x="{x + w + 40}" y="{(top + bottom) / 2:.1f}" transform="rotate(90 {x + w + 40} {(top + bottom) / 2:.1f})" '
                 f'text-anchor="middle">n + 1</text>')
    H = y - gap + 8
    return (f'<svg viewBox="0 0 {W} {H:.0f}" role="img" aria-label="{esc(" → ".join(steps))}" style="max-width:{W}px">'
            + "".join(parts) + "</svg>")


def team_svg(page, lang):
    """The team as the page defines it: the planner hands out todos, the members answer, the judge decides what passes,
    and what passes is the page's knowledge, which the next run carries."""
    d = page.d
    team = d.get("team") or {}
    try:
        planner = parse_member(team.get("planner", ""))
        members = [parse_member(m) for m in team.get("members") or []]
    except MemberError:
        return ""
    about = team.get("about") or {}
    W, x, w = 360, 14, 250
    parts, y = [], 8

    def who(m):
        return f"{m['name']} · {m['backend']}" + (f":{m['model']}" if m.get("model") else "")

    parts.append(box(x, y, w, 36, "team", who(planner), say(lang, "t_planner")))
    y_members = y + 36 + 26
    parts.append(arrow(x + 30, y + 36, x + 30, y_members, "", say(lang, "t_todos"), x + 38, y + 36 + 17))
    y = y_members
    for m in members:
        parts.append(box(x, y, w, 36, "", who(m), about.get(m["name"]) or say(lang, "t_members")))
        y += 42
    y_judge = y + 20
    parts.append(arrow(x + 30, y - 6, x + 30, y_judge, "", say(lang, "t_answers"), x + 38, y + 11))
    judge = " ".join(os.path.basename(w) if i < 2 else w for i, w in enumerate(d.get("judge") or [])) or "judge"
    parts.append(box(x, y_judge, w, 36, "", judge, say(lang, "t_judge")))
    y_kb = y_judge + 36 + 26
    parts.append(arrow(x + 30, y_judge + 36, x + 30, y_kb, "carry", say(lang, "t_verified"), x + 38, y_judge + 36 + 17))
    carry = ", ".join(d.get("carry") or []) or say(lang, "carry_all")
    parts.append(box(x, y_kb, w, 36, "ok", say(lang, "page_kb"), f"{say(lang, 'carry')}: {carry}"))
    # what passed goes back to the planner (its next todos) and on to the next run
    parts.append(f'<path class="ar back" d="M{x + w},{y_kb + 18} H{x + w + 26} V{8 + 18} H{x + w + 7}"/>')
    parts.append(f'<polygon class="ah" points="{x + w},{8 + 18} {x + w + 7},{8 + 14.5} {x + w + 7},{8 + 21.5}"/>')
    H = y_kb + 36 + 10
    label = f"{who(planner)} → " + ", ".join(m["name"] for m in members) + f" → {judge} → {say(lang, 'page_kb')}"
    return f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{esc(label)}" style="max-width:{W}px">' + "".join(parts) + "</svg>"


def chain_svg(page, lang):
    """Every run as a box, top to bottom; a green arrow from the run each one went on from, with what it carried."""
    runs = page.runs()
    if not runs:
        return ""
    W, x, w, h, gap = 360, 64, 280, 46, 22
    pos, parts = {}, []
    for i, r in enumerate(runs):
        y = 8 + i * (h + gap)
        pos[r["n"]] = y
        f = page.facts(r["n"])
        first = made_by_kind(page, f)[:1]
        sub = " · ".join(x for x in (
            (span_text(f.get("seconds"), lang) if f.get("seconds") else "") if f["state"] == "ended" else say(lang, "no_end"),
            f"{tokens_text(f.get('tokens'))} tokens" if f.get("tokens") else "",
            f"{first[0][0]} {score_text(first[0][1]['score'])}" if first else "") if x)
        cls = {"ended": "", "running": "team", "stuck": "bad"}.get(f["state"], "dash")
        title = say(lang, "run_n", n=r["n"]) + f" · {local(f.get('start') or r['t'])}" + (f" · {f['type']}" if f["type"] != "engine" else "")
        parts.append(box(x, y, w, h, cls, title, sub))
    for i, r in enumerate(runs):
        src = r.get("from")
        if src not in pos:
            continue
        y1, y2 = pos[src] + h / 2, pos[r["n"]] + h / 2
        bend = x - 14 - 12 * (i % 3)
        f = page.facts(r["n"])
        parts.append(f'<path class="ar carry" d="M{x},{y1:.1f} H{bend} V{y2:.1f} H{x - 7}"/>')
        parts.append(f'<polygon class="ah carry" points="{x},{y2:.1f} {x - 7},{y2 - 3.5:.1f} {x - 7},{y2 + 3.5:.1f}"/>')
        n = len(f.get("carried") or [])
        if n:
            parts.append(f'<text class="al" x="{bend - 4}" y="{y2 - 6:.1f}" text-anchor="end">{n}</text>')
    H = 8 + len(runs) * (h + gap) - gap + 8
    label = "; ".join(say(lang, "run_n", n=r["n"]) + (f" ← {r['from']}" if r.get("from") else "") for r in runs)
    return f'<svg viewBox="0 0 {W} {H:.0f}" role="img" aria-label="{esc(label)}" style="max-width:{W}px">' + "".join(parts) + "</svg>"


def run_card(page, r, lang, rel):
    f = page.facts(r["n"])
    head = [f'<b>{esc(say(lang, "run_n", n=r["n"]))}</b>', f'<span class="muted">{esc(local(f.get("start") or r["t"], "%Y-%m-%d %H:%M"))}</span>']
    state = {"ended": "", "running": "ice", "stuck": "bad", "missing": "bad", "unknown": "bad"}[f["state"]]
    head.append(f'<span class="chip {state}">{esc(say(lang, "st_run_" + f["state"]) if f["state"] in ("ended", "running", "stuck", "missing") else f["state"])}</span>')
    if f.get("seconds"):
        head.append(f'<span class="chip">{esc(span_text(f["seconds"], lang))}</span>')
    lines = []
    lines.append(say(lang, "from_n", n=r["from"]) if r.get("from") else say(lang, "fresh"))
    if r.get("attached"):
        lines.append(say(lang, "attached"))
    if f["type"] in ("engine", "coop"):
        team = ", ".join(f.get("members") or [])
        lines.append(f"{say(lang, 'team')}: " + (f"{f['planner']} → " if f.get("planner") else "") + team)
        lines.append(cost_text(lang, f.get("tokens"), f"{f['member_turns']}/{f.get('turn_budget') or '?'}" if f.get("turn_budget")
                               else f["member_turns"], f.get("seconds"), f["state"] == "ended"))
        carried = f.get("carried") or []
        lines.append(say(lang, "carried", n=len(carried), kinds=kinds_text(carried, lang), u=len(f.get("used") or []))
                     if carried else say(lang, "carried_none"))
        lines.append(say(lang, "made", n=len(f["made"]), v=f["valid"], i=f["invalid"]))
        made = made_text(page, [f], lang)
        if made:
            lines.append(say(lang, "outputs_line", items=made))
    elif f["type"] == "dag":
        failed = say(lang, "dag_failed", n=f["failed"]) if f["failed"] else ""
        lines.append(f"DAG {f.get('plan') or ''}: " + steps_text(lang, f))
        lines.append(say(lang, "dag_steps", passed=f["passed"], n=len(f["steps"]), failed=failed))
    if f.get("stopped"):
        lines.append(say(lang, "stopped", why=f["stopped"]))
    if r.get("notes"):
        lines.append(say(lang, "notes_used", ids=", ".join(r["notes"])))
    if r.get("note"):
        lines.append(r["note"])
    link = f' · <a href="{esc(rel)}">{esc(say(lang, "replay"))}</a>' if rel else ""
    return (f'<li><div class="row">{" ".join(head)}{link}</div>'
            + "".join(f'<div class="muted">{esc(x)}</div>' for x in lines) + "</li>")


def history_html(page, lang):
    rows = []
    for e in sorted(page.history, key=lambda e: e.get("t") or 0):
        k = e.get("kind")
        why = e.get("why") or ""
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
        }.get(k, lambda: k)()
        mark = f' <span class="chip">{esc(say(lang, "imported"))} {esc(local(e.get("recorded")))}</span>' if e.get("imported") else ""
        rows.append(f'<li><span class="faint">{esc(local(e.get("t"), "%Y-%m-%d %H:%M"))}</span> · <b>{esc(e.get("by"))}</b> · '
                    f'{esc(text)}{mark}</li>')
    return '<ul class="hist">' + "".join(rows) + "</ul>"


def inbox_items(nb, pages, lang, prefix):
    items = []
    for page in pages:
        state, needs = page.state()
        for need in needs:
            body = [f'<div class="row"><i class="led {"bad" if need[0] == "stuck" else "warn"}"></i>'
                    f'<h3><a href="{prefix}{page.id}/">{esc(page.d.get("title"))}</a></h3>'
                    f'<span class="chip warn">{esc(need_text(lang, need, page))}</span></div>']
            if need[0] == "approve":
                body.append(f'<div class="muted">{esc(page.d.get("goal"))}</div>')
                team = page.d.get("team") or {}
                if team.get("members"):
                    body.append(f'<div class="faint">{esc(say(lang, "team"))}: {esc(team.get("planner"))} → '
                                f'{esc(", ".join(team["members"]))}</div>')
                b = page.d.get("budget") or {}
                if b:
                    body.append(f'<div class="faint">{esc(say(lang, "budget"))}: '
                                f'{esc(", ".join(f"{k} {v:g}" for k, v in sorted(b.items())))}</div>')
                body.append(f'<div>{esc(say(lang, "todo_approve"))}</div>')
                body.append(f'<div class="faint">{esc(say(lang, "say"))}</div>' + cmd_html(nb, "run", page.id, "--dry-run")
                            + cmd_html(nb, "approve", page.id))
            elif need[0] == "review":
                f = page.facts(need[1])
                body.append(f'<div class="muted">{esc(cost_text(lang, f.get("tokens"), f.get("member_turns"), f.get("seconds"), f["state"] == "ended"))}</div>')
                body.append(f'<div>{esc(say(lang, "todo_review"))}</div>')
                body.append(f'<div class="faint">{esc(say(lang, "say"))}</div>')
                picks = page.picks()
                for kind, versions in outputs_of(page).items():
                    best = best_of([v for v in versions if v["run"] == need[1]]) or best_of(versions)
                    if kind in picks:
                        continue
                    if best:
                        body.append(f'<div class="muted">{esc(kind)}: {esc(say(lang, "no_pick"))}; {esc(say(lang, "suggest"))} '
                                    f'<a href="{prefix}{page.id}/files/{esc(best.get("file") or "")}">{esc(best["id"])}</a> '
                                    f'({esc(score_text(best["score"]))}, {esc(best["member"])}, {esc(say(lang, "run_n", n=best["run"]))})</div>')
                        body.append(cmd_html(nb, "pick", page.id, best["id"]))
                body.append(cmd_html(nb, "note", page.id, "…") + cmd_html(nb, "accept", page.id))
            items.append("<li>" + "".join(body) + "</li>")
    return items


def page_html(nb, page, lang, run_links):
    d = page.d
    state, needs = page.state()
    runs = page.runs()
    facts = [page.facts(r["n"]) for r in runs]
    tokens = sum(f.get("tokens") or 0 for f in facts)
    seconds = sum(f.get("seconds") or 0 for f in facts)
    chips = [f'<span class="chip {STATE_CHIP[state]}">{esc(say(lang, "st_" + state))}</span>',
             f'<span class="chip">{esc(say(lang, "opened"))} {esc(d.get("day"))}</span>',
             f'<span class="chip">{esc(say(lang, "n_runs", n=len(runs)))}</span>']
    if tokens:
        chips.append(f'<span class="chip">{esc(tokens_text(tokens))} tokens</span>')
    if seconds:
        chips.append(f'<span class="chip">{esc(span_text(seconds, lang))}</span>')
    body = [f'<header class="top"><div class="brand"><a href="../../">{esc(say(lang, "back"))}</a> · {esc(nb.title)}</div>'
            f'<h1>{esc(d.get("title"))}</h1><div class="state"><i class="led {STATE_LED[state]}"></i>{esc(d.get("goal"))}</div>'
            f'<div class="chips">{"".join(chips)}</div></header>']
    items = inbox_items(nb, [page], lang, "../")
    if items:
        body.append(f'<section class="panel inbox"><h2>{esc(say(lang, "inbox"))}</h2><ul class="items">{"".join(items)}</ul></section>')
    # the definition
    kv = []
    approval = page.approval()
    if approval:
        kv.append(("", say(lang, "approved_as", d=page.digest(), by=approval.get("by"), when=local(approval.get("t")))))
    else:
        kv.append(("", say(lang, "not_approved", d=page.digest())))
    if d.get("asked"):
        kv.append((say(lang, "asked"), d["asked"]))
    if d.get("task"):
        kv.append((say(lang, "task_file"), d["task"]))
    if d.get("judge"):
        kv.append((say(lang, "judge"), " ".join(d["judge"])))
    b = d.get("budget") or {}
    parts = [say(lang, "b_" + {"turns": "turns", "planner_wakes": "wakes", "time_limit": "time", "max_open": "open",
                               "turn_timeout": "turn_timeout"}[k], n=f"{b[k]:g}") for k in BUDGET if k in b]
    if parts:
        kv.append((say(lang, "budget"), " · ".join(parts)))
    if d.get("kind", "engine") == "engine":
        kv.append((say(lang, "carry"), ", ".join(d.get("carry") or []) or say(lang, "carry_all")))
    if d.get("outputs"):
        kv.append((say(lang, "outputs"), ", ".join(d["outputs"])))
    if (d.get("team") or {}).get("access"):
        kv.append((say(lang, "access"), d["team"]["access"]))
    if d.get("cwd"):
        kv.append((say(lang, "folder"), d["cwd"]))
    task = ""
    if d.get("task"):
        try:
            text = read_text(page.task_path())
            task = f'<pre class="task">{esc(text if len(text) < 6000 else text[:6000] + "…")}</pre>'
        except OSError:
            task = ""
    definition = ('<dl class="kv">' + "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in kv) + "</dl>" + task)
    team = team_svg(page, lang)
    left = f'<section class="panel"><h2>{esc(say(lang, "goal"))}</h2>{definition}</section>'
    roles = ""
    t = d.get("team") or {}
    if t.get("members"):
        about = t.get("about") or {}
        roles = '<ul class="roles">' + "".join(
            f'<li><b>{esc(m.split("=")[0])}</b> <span class="faint">{esc(m.split("=", 1)[1] if "=" in m else "")}</span>'
            + (f' · {esc(about[m.split("=")[0]])}' if about.get(m.split("=")[0]) else "") + "</li>" for m in t["members"]) + "</ul>"
    right = (f'<section class="panel"><h2>{esc(say(lang, "team"))}</h2><div class="fig">{team}</div>{roles}'
             f'<div class="legend"><span><i class="team"></i>planner</span><span><i class="ok"></i>{esc(say(lang, "page_kb"))}</span>'
             f'<span>{esc("虛線：通過的回到 planner" if lang == "zh-TW" else "dashed: what passed goes back to the planner")}</span></div></section>'
             if team else "")
    body.append(f'<div class="grid2">{left}{right}</div>')
    # the runs
    if runs:
        cards = "".join(run_card(page, r, lang, run_links.get(r["n"])) for r in reversed(runs))
        body.append(f'<div class="grid2"><section class="panel"><h2>{esc(say(lang, "chain"))}</h2><div class="fig">{chain_svg(page, lang)}</div>'
                    f'<div class="legend"><span><i class="carry"></i>{esc("延續（數字：帶入幾條）" if lang == "zh-TW" else "goes on from (number: entries carried)")}</span></div></section>'
                    f'<section class="panel"><h2>{esc(say(lang, "runs"))}</h2><ul class="items">{cards}</ul></section></div>')
    # versions
    outs = outputs_of(page)
    if any(outs.values()):
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
                tag = (f'<span class="chip good">{esc(say(lang, "current"))}</span>' if is_cur else "")
                rows.append(f'<li class="{"cur" if is_cur else ""}">{tag}<span class="mono">{esc(v["id"])}</span>'
                            f'<span>{esc(say(lang, "run_n", n=v["run"]))} · {esc(v["member"])} · {esc(score_text(v["score"]))}</span>'
                            f'<span class="faint">{esc(local(v.get("t")))}</span>{link}</li>')
            note = (say(lang, "picked", by=cur.get("by"), when=local(cur.get("t"))) if cur else say(lang, "no_pick"))
            more = f'<div class="faint">… {len(versions) - 12}</div>' if len(versions) > 12 else ""
            blocks.append(f'<div><div class="row"><b>{esc(say(lang, "kind_versions", kind=kind, n=len(versions)))}</b>'
                          f'<span class="faint">{esc(note)}</span></div><ul class="vers">{"".join(rows)}</ul>{more}</div>')
        body.append(f'<section class="panel"><h2>{esc(say(lang, "versions"))}</h2><div class="items" style="gap:14px">{"".join(blocks)}</div></section>')
    # knowledge
    entries = [e for e in page.entries().values() if e["status"] == "valid"]
    if entries:
        nxt = {e["id"] for e in page.next_carry()}
        excluded = page.excluded()
        kinds = collections.OrderedDict()
        shown = set(outs) if any(outs.values()) else set()
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
                    x = excluded[e["id"]]
                    bits.append(say(lang, "excluded", by=x.get("by"), why=x.get("why") or ""))
                name = e.get("name") or e.get("summary") or ""
                mark = "→ " if e["id"] in nxt else ""
                link = f' <a href="files/{esc(e["file"])}">{esc(say(lang, "open"))}</a>' if e.get("file") else ""
                rows.append(f'<li><span class="mono">{esc(mark + e["id"])}</span><span>{esc(fit(name, 520, 13))}</span>'
                            f'<span class="faint">{esc(" · ".join(bits))}</span>{link}</li>')
            more = f'<div class="faint">… {len(group) - 20}</div>' if len(group) > 20 else ""
            blocks.append(f'<div><b>{esc(kind)}</b> <span class="faint">{len(group)}</span><ul class="vers">{"".join(rows)}</ul>{more}</div>')
        line = say(lang, "kb_line", n=len(entries), kinds=kinds_text(entries, lang), m=len(nxt))
        above = [k for k in outs if outs[k]] if shown else []
        body.append(f'<section class="panel"><h2>{esc(say(lang, "page_kb"))}</h2><div class="muted">{esc(line)}'
                    f'{esc("（→ 標的是下一次會帶入的）" if lang == "zh-TW" else " (→ marks what the next run carries)")}</div>'
                    + (f'<div class="faint">{esc(say(lang, "outputs_above", kinds=", ".join(above)))}</div>' if above else "")
                    + f'<div class="items" style="gap:12px;margin-top:8px">{"".join(blocks)}</div></section>')
    # notes and links
    notes = page.notes()
    rows = [f'<li><span class="mono">{esc(x["id"])}</span> {esc(x["text"])} <span class="faint">· {esc(x.get("by"))} · {esc(local(x.get("t")))} · '
            f'{esc(say(lang, "note_used", n=x["used_by"]) if x["used_by"] else say(lang, "note_next"))}</span></li>' for x in notes]
    body.append(f'<section class="panel"><h2>{esc(say(lang, "notes"))}</h2>'
                + (f'<ul class="hist">{"".join(rows)}</ul>' if rows else f'<div class="muted">{esc(say(lang, "notes_none"))}</div>')
                + f'<div class="faint" style="margin-top:8px">{esc(say(lang, "say"))}</div>' + cmd_html(nb, "note", page.id, "…") + "</section>")
    if d.get("links"):
        rows = "".join(f'<li><a href="{esc(x["url"])}">{esc(x["label"])}</a></li>' for x in d["links"])
        body.append(f'<section class="panel"><h2>{esc(say(lang, "links"))}</h2><ul class="hist">{rows}</ul></section>')
    body.append(f'<section class="panel"><h2>{esc(say(lang, "history"))}</h2>{history_html(page, lang)}</section>')
    return SHELL.format(lang=esc(lang), title=esc(d.get("title")), style=STYLE, body="\n".join(body))


def find_runs(roots):
    """Run folders under these folders (a child, or a child's run/ folder): engine runs and DAG runs."""
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


def home_html(nb, pages, lang, roots=()):
    states = {p.id: p.state() for p in pages}
    runs_n = sum(len(p.runs()) for p in pages)
    items = inbox_items(nb, pages, lang, "p/")
    chips = [f'<span class="chip">{esc(say(lang, "n_pages", n=len(pages)))}</span>',
             f'<span class="chip">{esc(say(lang, "n_runs", n=runs_n))}</span>',
             f'<span class="chip {"warn" if items else "good"}">{esc(say(lang, "n_wait", n=len(items)))}</span>']
    body = [f'<header class="top"><div class="brand">{esc(say(lang, "brand"))}</div><h1>{esc(nb.title)}</h1>'
            f'<div class="state">{esc(say(lang, "updated", when=local(time.time(), "%Y-%m-%d %H:%M")))}</div>'
            f'<div class="chips">{"".join(chips)}</div></header>']
    body.append(f'<section class="panel inbox"><h2>{esc(say(lang, "inbox"))}</h2>'
                + (f'<ul class="items">{"".join(items)}</ul>' if items else f'<div class="muted">{esc(say(lang, "inbox_none"))}</div>')
                + "</section>")
    # pages by day: a page is on every day it was opened, ran or was decided on
    days = collections.defaultdict(list)
    for p in pages:
        on = {p.d.get("day")}
        on |= {day_of(r["t"]) for r in p.runs()}
        on |= {day_of(e["t"]) for e in p.history if e.get("kind") in LOOKED}
        for day in on:
            if day:
                days[day].append(p)
    sections = []
    for day in sorted(days, reverse=True):
        cards = []
        for p in sorted(days[day], key=lambda p: p.d.get("title") or ""):
            state, needs = states[p.id]
            runs = [r for r in p.runs() if day_of(r["t"]) == day]
            facts = [p.facts(r["n"]) for r in runs]
            lines = []
            if runs:
                tokens = sum(f.get("tokens") or 0 for f in facts)
                turns = sum(f.get("member_turns") or 0 for f in facts)
                seconds = sum(f.get("seconds") or 0 for f in facts)
                ended = all(f["state"] == "ended" for f in facts)
                made = made_text(p, facts, lang, who=False)
                lines.append(say(lang, "runs_that_day", ns="、".join(str(r["n"]) for r in runs) if lang == "zh-TW"
                                 else ", ".join(str(r["n"]) for r in runs))
                             + (" · " + say(lang, "outputs_line", items=made) if made else ""))
                lines.append(cost_text(lang, tokens, turns, seconds, ended))
                if facts[-1].get("steps"):
                    lines.append(f"{say(lang, 'steps')}: " + steps_text(lang, facts[-1]))
            elif not p.runs():
                lines.append(say(lang, "no_run_yet"))
            picks = p.picks()
            outs = [(k, v) for k, v in outputs_of(p).items() if v]
            if outs:
                cur = [f"{k} {picks[k]['entry']}" for k, _ in outs if k in picks]
                lines.append(f"{say(lang, 'current')}: " + ", ".join(cur) if cur else say(lang, "no_pick"))
            team = p.d.get("team") or {}
            if team.get("members"):
                lines.append(f"{say(lang, 'team')}: " + team.get("planner", "").split("=")[0] + " → "
                             + ", ".join(m.split("=")[0] for m in team["members"]))
            elif p.runs():
                ms = sorted({m for r in p.runs() for m in p.facts(r["n"]).get("members") or []})
                if ms:
                    lines.append(f"{say(lang, 'team')}: " + ", ".join(ms))
            kb = [e for e in p.entries().values() if e["status"] == "valid"]
            if kb:
                lines.append(f"{say(lang, 'kb')}: " + say(lang, "kb_line", n=len(kb), kinds=kinds_text(kb, lang), m=len(p.next_carry())))
            cards.append(f'<li><div class="row"><i class="led {STATE_LED[state]}"></i><h3><a href="p/{p.id}/">{esc(p.d.get("title"))}</a></h3>'
                         f'<span class="chip {STATE_CHIP[state]}">{esc(say(lang, "st_" + state))}</span></div>'
                         f'<div class="muted">{esc(p.d.get("goal"))}</div>'
                         + "".join(f'<div class="faint">{esc(x)}</div>' for x in lines) + "</li>")
        sections.append(f'<section class="panel day"><h2>{esc(day_label(day, lang))}</h2><ul class="items">{"".join(cards)}</ul></section>')
    body.append(f'<div class="grid2"><div class="day">{"".join(sections)}</div>'
                f'<section class="panel"><h2>{esc(say(lang, "how"))}</h2><div class="fig">{life_svg(lang)}</div>'
                f'<div class="legend"><span><i class="me"></i>{esc("你" if lang == "zh-TW" else "you")}</span>'
                f'<span><i class="team"></i>{esc("團隊" if lang == "zh-TW" else "the team")}</span>'
                f'<span><i></i>Claude</span></div></section></div>')
    if roots:
        found = find_runs(roots)
        held = {os.path.realpath(r["folder"]) for p in pages for r in p.runs()}
        loose = [f for f in found if os.path.realpath(f) not in held]
        m = len(found) - len(loose)
        rate = f"{m / len(found):.0%}" if found else "?"
        rows = []
        for folder in loose:
            f = run_facts(folder)
            rows.append(f'<li><span class="mono">{esc(tilde(folder))}</span> '
                        f'<span class="faint">{esc(f["type"])} · {esc(local(f.get("start")))} · {esc(f["state"])}</span></li>')
        body.append(f'<section class="panel"><h2>{esc(say(lang, "unattached"))}</h2><details><summary>'
                    f'{esc(say(lang, "attach_rate", m=m, n=len(found), roots=", ".join(tilde(os.path.abspath(os.path.expanduser(r))) for r in roots), p=rate))}</summary>'
                    f'<ul class="hist" style="margin-top:8px">{"".join(rows)}</ul></details></section>')
    return SHELL.format(lang=esc(lang), title=esc(nb.title), style=STYLE, body="\n".join(body))


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(path + ".tmp", path)


def view(nb, out, roots=()):
    """Write the notebook as HTML under out: index.html, p/<id>/index.html, p/<id>/runs/<n>.html, p/<id>/files/."""
    lang, pages = nb.lang, nb.pages()
    problems = []
    for page in pages:
        base = os.path.join(out, "p", page.id)
        for e in page.entries().values():  # every verified file, so a version can be opened from the page
            if e["status"] == "valid":
                page.files[e["id"]] = export_file(e, os.path.join(base, "files"))
        links = {}
        for r in page.runs():
            f = page.facts(r["n"])
            try:
                if f["type"] == "engine":
                    write(os.path.join(base, "runs", f"{r['n']}.html"), engineview.render(r["folder"]))
                elif f["type"] == "dag":
                    write(os.path.join(base, "runs", f"{r['n']}.html"), dagview.render(r["folder"]))
                elif f["type"] == "coop":
                    write(os.path.join(base, "runs", f"{r['n']}.html"), coopview.build(r["folder"]))
                else:
                    continue
                links[r["n"]] = f"runs/{r['n']}.html"
            except Exception as exc:  # noqa: BLE001 - one run's page must not stop the notebook's
                problems.append(f"{page.id} run {r['n']}: {type(exc).__name__}: {exc}")
        write(os.path.join(base, "index.html"), page_html(nb, page, lang, links))
    write(os.path.join(out, "index.html"), home_html(nb, pages, lang, roots))
    return problems


# ---- the command line
def status_text(nb):
    lang, out = nb.lang, []
    pages = nb.pages()
    waiting = [(p, need) for p in pages for need in p.state()[1]]
    out.append(f"{nb.title}: " + say(lang, "n_pages", n=len(pages)) + ", " + say(lang, "n_wait", n=len(waiting)))
    for p, need in waiting:
        out.append(f"  ! {p.id}: {need_text(lang, need, p)}")
    for p in pages:
        state, _ = p.state()
        runs = p.runs()
        last = p.facts(runs[-1]["n"]) if runs else None
        best = last.get("best") if last else None
        out.append(f"  {p.id} [{say(lang, 'st_' + state)}] {p.d.get('title')}: " + say(lang, "n_runs", n=len(runs))
                   + (f", {say(lang, 'best', score=score_text(best['score']))}" if best else ""))
    return "\n".join(out)


def parse_at(text):
    if text is None:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return time.mktime(time.strptime(text, fmt))
        except ValueError:
            continue
    raise NotebookError(f"--at {text!r}: seconds since 1970, or YYYY-MM-DD HH:MM[:SS] (local time)")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m herdr_py.notebook", description=__doc__.split("\n\n")[0])
    ap.add_argument("notebook", help="the notebook's folder")
    ap.add_argument("command", choices=["draft", "approve", "run", "attach", "note", "pick", "exclude", "include", "accept",
                                        "hold", "done", "reopen", "status", "view"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--by", default=os.environ.get("USER") or "person", help="who decided (default: this user)")
    ap.add_argument("--at", help="when it happened, for a record made before the notebook existed (marked imported)")
    ap.add_argument("--why", default="", help="the reason, for exclude, accept, hold, done and reopen")
    ap.add_argument("--from", dest="source", type=int, metavar="N", help="run / attach: the run this one goes on from")
    ap.add_argument("--fresh", action="store_true", help="run: start from nothing")
    ap.add_argument("--dry-run", action="store_true", help="run: show the engine command and the task, run nothing")
    ap.add_argument("--detach", action="store_true", help="run: start the run and return")
    ap.add_argument("--note", help="attach: a line about the run")
    ap.add_argument("--out", help="view: the folder to write")
    ap.add_argument("--runs", action="append", default=[], metavar="DIR", help="view: list the run folders under DIR no page holds")
    a = ap.parse_args(argv)
    nb = Notebook(a.notebook)
    need = {"draft": 1, "approve": 1, "run": 1, "attach": 2, "note": 2, "pick": 2, "exclude": 2, "include": 2, "accept": 1,
            "hold": 1, "done": 1, "reopen": 1, "status": 0, "view": 0}[a.command]
    if len(a.args) != need:
        print(f"herdr-py notebook: {a.command} takes {need} argument{'s' if need != 1 else ''}", file=sys.stderr)
        return 2
    try:
        at = parse_at(a.at)
        if a.command == "draft":
            page = draft(nb, a.args[0], a.by, at)
            print(f"{page.id}: version {page.digest()} is a draft; to run it, approve it: {command(nb, 'approve', page.id)}")
        elif a.command == "status":
            print(status_text(nb))
        elif a.command == "view":
            if not a.out:
                raise NotebookError("view: --out DIR")
            problems = view(nb, os.path.abspath(os.path.expanduser(a.out)), a.runs)
            for p in problems:
                print(f"herdr-py notebook: warning: {p}", file=sys.stderr)
            print(os.path.join(os.path.abspath(os.path.expanduser(a.out)), "index.html"))
        else:
            page = nb.page(a.args[0])
            if a.command == "approve":
                approve(page, a.by, at)
                print(f"{page.id}: version {page.digest()} approved")
            elif a.command == "run":
                code = start_run(page, a.by, a.source, a.fresh, a.dry_run, a.detach)
                return 0 if code is None else code
            elif a.command == "attach":
                e = attach(page, a.args[1], a.by, a.source, at, a.note)
                print(f"{page.id}: run {e['n']} is {e['dir']}")
            elif a.command == "note":
                e = add_note(page, a.args[1], a.by, at)
                print(f"{page.id}: note {e['id']} goes into the next run")
            elif a.command == "pick":
                e = pick(page, a.args[1], a.by, at)
                print(f"{page.id}: {e['entry']} is the current {e['of']}")
            elif a.command in ("exclude", "include"):
                find_entry(page, a.args[1])
                excluded = a.args[1] in page.excluded()
                if excluded == (a.command == "exclude"):
                    raise NotebookError(f"{a.args[1]} is {'already' if excluded else 'not'} excluded")
                page.append(a.command, a.by, at=at, entry=a.args[1], why=a.why)
                print(f"{page.id}: {a.args[1]} {a.command}d")
            else:
                page.append(a.command, a.by, at=at, why=a.why)
                print(f"{page.id}: {a.command}")
    except NotebookError as exc:
        print(f"herdr-py notebook: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
