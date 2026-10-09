"""A notebook of pages: every piece of work a person hands to a team is a page (a task). A page keeps its definition
(goal, task, judge, team, budget), every run, what its teams verified and the person's decisions, and the next run goes
on from an earlier one with what it verified, the skills its members wrote and the person's notes.

    python3 -m herdr_py.notebook NOTEBOOK request GOAL [--title T]   a new task, in a line: Claude drafts its page
    python3 -m herdr_py.notebook NOTEBOOK draft PAGE.json [--request r1]   a new page, or a new version of one (a draft)
    python3 -m herdr_py.notebook NOTEBOOK approve PAGE         the draft, as it is now, may run
    python3 -m herdr_py.notebook NOTEBOOK run PAGE [--from N | --fresh] [--dry-run] [--detach]
    python3 -m herdr_py.notebook NOTEBOOK attach PAGE RUN_DIR [--from N]     a run made outside the notebook
    python3 -m herdr_py.notebook NOTEBOOK note PAGE TEXT        for the next run
    python3 -m herdr_py.notebook NOTEBOOK pick PAGE ENTRY       this version is the current one (of its kind)
    python3 -m herdr_py.notebook NOTEBOOK exclude|include PAGE ENTRY     what later runs may carry
    python3 -m herdr_py.notebook NOTEBOOK accept|hold|done|reopen PAGE
    python3 -m herdr_py.notebook NOTEBOOK status                what waits for a person, page by page
    python3 -m herdr_py.notebook NOTEBOOK view --out DIR [--runs DIR]...    the notebook as pages (notebookview.py)
    python3 -m herdr_py.notebook NOTEBOOK serve [--port 8790] [--runs DIR]...  the same, live, with buttons

NOTEBOOK/notebook.json holds the title, the language of the pages (en or zh-TW) and, optionally, a style sheet of the
notebook's own for its pages ("style"); NOTEBOOK/requests.jsonl the
requests for new tasks (append-only). Each page is a folder NOTEBOOK/pages/<id>/: page.json (its definition),
history.jsonl (append-only: every draft, approval, run, note, pick, exclusion and hold, with who and when) and
runs/<n>/ (the engine's run folders; an attached run stays where it is and is only read). The rules:
- a draft never runs: run refuses a page whose definition, task or judge program differs from what was last approved;
- a person's decisions are only appended (--by names who decided; --at writes an earlier time for a record made
  before the notebook existed, and marks the event imported);
- a run goes on from an earlier run of the page (the latest, unless --from or --fresh): the engine starts its
  knowledge base from that run's verified entries of the kinds the page carries, leaving out what a person excluded
  (engine --seed-from), and the notes no run has used yet are added to the task;
- every run is held to the page's budget (member turns, planner wakes, time limit); more needs an approved new version.
What waits for a person is worked out from these records, never stored: a request to draft, a draft to approve, a run
that ended and that nobody has looked at since (a note, a pick, an exclusion, accept, hold or done), and a run with no
end that has written nothing for 15 minutes.
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

from . import dagview, engineview
from .engineview import artifact_reads, read_jsonl
from .members import MemberError, parse_member

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
    "n_pages": ("1 page|{n} pages", "{n} 頁"),
    "n_runs": ("1 run|{n} runs", "{n} 次執行"),
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
    "tokens": ("{n} tokens", "{n} tokens"), "turns": ("1 member turn|{n} member turns", "{n} 個成員回合"),
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
    "t_todos": ("todos", "待辦"), "t_answers": ("answers", "答案"),
    "t_judge": ("judge", "評分"), "t_verified": ("what passed", "通過的"), "t_next": ("next run", "下一次"),
    "t_members": ("members", "成員"),
}


def say(lang, key, **kw):
    text = S[key][1 if lang == "zh-TW" else 0]
    if "|" in text and "{" in text:  # "one|many": the first when n is 1
        one, many = text.split("|", 1)
        text = one if kw.get("n") == 1 else many
    return text.format(**kw)


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
    if seconds and 0 < seconds < 1:
        return "不到 1 秒" if lang == "zh-TW" else "<1s"
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


def append_line(path, event):
    """Append one JSON line under a file lock (append-only records: history.jsonl, requests.jsonl)."""
    line = (json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
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
    if "icon" in d and not (isinstance(d["icon"], str) and 1 <= len(d["icon"].strip()) <= 3):
        problems.append("icon: one to three characters for the rail")
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
        self.style = meta.get("style")  # a CSS file of the notebook's own, added to every page of the view
        self.tree = meta.get("tree") or "wide"  # the team drawn sideways (wide), top down (tall), or both for a style to choose

    def ids(self):
        root = os.path.join(self.folder, "pages")
        if not os.path.isdir(root):
            return []
        return sorted(p for p in os.listdir(root) if ID.match(p) and os.path.isfile(os.path.join(root, p, "page.json")))

    def extra_style(self):
        """The notebook's own style sheet (notebook.json "style", relative to its folder), or "" when it names none or
        the file cannot be read. It cannot end the page's style element."""
        if not isinstance(self.style, str) or not self.style.strip():
            return ""
        path = os.path.expanduser(self.style)
        path = path if os.path.isabs(path) else os.path.join(self.folder, path)
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            return ""
        return re.sub(r"</(style)", r"<\\/\1", text, flags=re.I)

    def requests(self):
        """Every request for a new task, with its state: open until a draft names it (drafted) or a person drops it."""
        rows = read_jsonl(os.path.join(self.folder, "requests.jsonl"))
        out = collections.OrderedDict()
        for e in rows:
            if e.get("kind") == "request" and e.get("id") not in out:
                out[e["id"]] = dict(e, state="open", page=None)
            elif e.get("kind") in ("drafted", "dropped") and e.get("request") in out:
                out[e["request"]].update(state=e["kind"], page=e.get("page"))
        return list(out.values())

    def ask(self, goal, by, title=None, at=None):
        """Record a request for a new task (the + of the view): Claude drafts its page, a person approves it."""
        if not isinstance(goal, str) or not goal.strip():
            raise NotebookError("a new task needs its goal in a line")
        rid = f"r{len(self.requests()) + 1}"
        return self._append("requests.jsonl", dict(kind="request", id=rid, goal=goal.strip()[:2000],
                                                    title=(title or "").strip()[:200] or None), by, at)

    def _append(self, name, event, by, at=None):
        event = dict(event, by=by, t=now())
        if at is not None:
            event.update(t=round(float(at), 3), recorded=now(), imported=True)
        os.makedirs(self.folder, exist_ok=True)
        append_line(os.path.join(self.folder, name), event)
        return event

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
        """What a person approves: the definition, the task the team will read and the judge's own program (a script
        the judge command names, found from cwd), together: a judge changed after an approval changes what passes."""
        h = hashlib.sha256(self.raw)
        if self.d.get("task"):
            try:
                with open(self.task_path(), "rb") as handle:
                    h.update(b"\0task\0" + handle.read())
            except OSError:
                h.update(b"\0no task file")
        for word in self.d.get("judge") or []:
            if not isinstance(word, str) or not word.endswith((".py", ".sh", ".js", ".rb", ".pl")):
                continue
            path = os.path.expanduser(word)
            path = path if os.path.isabs(path) else os.path.join(self.cwd(), path)
            try:
                with open(path, "rb") as handle:
                    h.update(b"\0judge\0" + word.encode("utf-8") + b"\0" + handle.read())
            except OSError:
                h.update(b"\0no judge file " + word.encode("utf-8"))
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
        path = os.path.join(self.dir, "history.jsonl")
        append_line(path, event)
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
    reads = artifact_reads(tools)
    opened = {eid for _, eid, _ in reads}
    built_on = {p for e in new for p in e.get("parents") or []}
    wakes = [r for r in engine if r.get("kind") == "wake"]
    sent_back = [{"wake": w.get("wake"), "attempt": w.get("attempt"), "problems": w.get("problems") or []}
                 for w in wakes if w.get("problems")]
    bad_turns = [{"member": t.get("member"), "turn": t.get("turn"), "state": t.get("state"), "problem": t.get("problem"),
                  "tail": t.get("reply_tail")} for t in turns if t.get("problem") or t.get("state") not in (None, "idle")]
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
            "todos": len(todos), "seeded": start.get("seeded"), "reads": [(a, eid) for a, eid, _ in reads],
            "sent_back": sent_back, "bad_turns": bad_turns, "broken": (summary or {}).get("broken") or [],
            "idle": (summary or {}).get("idle_seconds") or {}, "planner_wakes": len({w.get("wake") for w in wakes}),
            "wake_budget": start.get("planner_wakes"),
            # per agent, as recorded: every member turn, every todo, every planner wake (the agent cards and their panels)
            "turn_list": [{k: t.get(k) for k in ("member", "turn", "todo", "state", "status", "score", "entry", "kind", "seconds",
                                                 "tokens", "problem", "start", "end")} for t in turns],
            "todo_list": [{k: x.get(k) for k in ("id", "text", "for", "member", "state", "entry", "score", "added", "ended", "review")}
                          for x in todos],
            "wake_list": [{"wake": w.get("wake"), "added": len(w.get("added") or []), "dropped": len(w.get("dropped") or []),
                           "problems": w.get("problems") or [], "seconds": w.get("seconds"), "tokens": w.get("tokens"),
                           "done": w.get("done"), "state": w.get("state")} for w in wakes],
            "working": sorted({x.get("member") for x in todos if x.get("state") == "taken" and x.get("member")})}


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
    why = {k: v.get("why") for k, v in st.items() if v.get("why")}
    return {"type": "dag", "plan_doc": plan, "why": why, "attempts": {k: v["attempts"] for k, v in st.items()}, "state": state, "start": t0, "end": t1, "seconds": (t1 - t0) if t0 else None,
            "members": sorted({e.get("member") for e in events if e.get("member")}), "plan": plan.get("name"),
            "steps": list(plan.get("order") or []), "step_states": {k: v["state"] for k, v in st.items()},
            "passed": counts["passed"], "failed": counts["failed"], "tokens": None, "member_turns":
            sum(1 for e in events if e.get("kind") == "node.dispatch"), "made": [], "carried": [], "used": [],
            "stopped": next((e.get("why") for e in events if e.get("kind") == "run.stop"), None)}


# ---- commands
def read_text(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def draft(nb, source, by, at=None, request=None):
    """Install a page definition (a new page, or a new version of one): it is a draft until approved. request: the
    request (r1, r2, ...) this page answers, which is then marked drafted."""
    if request is not None and not any(r["id"] == request and r["state"] == "open" for r in nb.requests()):
        raise NotebookError(f"no open request {request}")
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
    page.append("revise" if existed else "create", by, at=at, digest=page.digest(),
                **({"request": request} if request else {}))
    if request is not None:
        nb._append("requests.jsonl", {"kind": "drafted", "request": request, "page": d["id"]}, by, at)
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


# ---- shared with the view
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
                                        "hold", "done", "reopen", "status", "view", "request", "serve"])
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
    ap.add_argument("--request", help="draft: the request (r1, r2, ...) this page answers")
    ap.add_argument("--title", help="request: a title for the new task")
    ap.add_argument("--host", default="127.0.0.1", help="serve: the address to listen on (default: this machine only)")
    ap.add_argument("--port", type=int, default=8790, help="serve: the port (default 8790)")
    ap.add_argument("--runs", action="append", default=[], metavar="DIR", help="view: list the run folders under DIR no page holds")
    a = ap.parse_args(argv)
    nb = Notebook(a.notebook)
    need = {"draft": 1, "approve": 1, "run": 1, "attach": 2, "note": 2, "pick": 2, "exclude": 2, "include": 2, "accept": 1,
            "hold": 1, "done": 1, "reopen": 1, "status": 0, "view": 0, "request": 1, "serve": 0}[a.command]
    if len(a.args) != need:
        print(f"herdr-py notebook: {a.command} takes {need} argument{'s' if need != 1 else ''}", file=sys.stderr)
        return 2
    try:
        at = parse_at(a.at)
        if a.command == "draft":
            page = draft(nb, a.args[0], a.by, at, request=a.request)
            print(f"{page.id}: version {page.digest()} is a draft; to run it, approve it: {command(nb, 'approve', page.id)}")
        elif a.command == "status":
            print(status_text(nb))
        elif a.command == "request":
            e = nb.ask(a.args[0], a.by, title=a.title, at=at)
            print(f"request {e['id']}: Claude drafts the page; it runs after a person approves it")
        elif a.command == "serve":
            from . import notebookview
            notebookview.serve(nb.folder, a.host, a.port, roots=a.runs,
                               ready=lambda url: print(f"open: {url}  (the buttons need the token after #)", flush=True))
        elif a.command == "view":
            from . import notebookview
            if not a.out:
                raise NotebookError("view: --out DIR")
            problems = notebookview.view(nb, os.path.abspath(os.path.expanduser(a.out)), a.runs)
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
