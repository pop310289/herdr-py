"""Live team dashboard: one read-only web page that shows a team run while it happens.

    python3 -m herdr_py.dashboard RUN_DIR [--port 8770] [--host 127.0.0.1] [--socket DAEMON_SOCKET]
    python3 -m herdr_py.dashboard RUN_DIR --html run.html     # the same page saved as one file: open it in any browser

RUN_DIR is a run folder made by examples/slide_team/run_demo.py (it holds work/) or the --workdir of layout_team.py.
The dashboard only reads it:
  chat.jsonl              who said what to whom: {t, from, to, kind: message|check|control|end, text}
  summary.json            written when the run ends: {turns: [...], final: {match, psnr, missing}, lessons: {text: n}}
  renders/manifest.jsonl  every kept picture {t, round, path}, next to the PNGs and the rowN-original.png crops
  codex/agents.json       Codex members (state, tokens, turns, stream); codex/events.jsonl tells their sessions
With --socket the members come from the herdr-py daemon (agent.list) instead of codex/agents.json. Until summary.json
exists the scores per row are read from the conversation (the program's "picture" lines), so the chart moves live.

The page loads the whole state once and then gets every change over Server-Sent Events (/api/events). Every /api and
/files request needs the token printed at start, as with the daemon's web UI: the page shows model output and, with
--socket, what the daemon knows, so other users of the machine get nothing without it. /files serves image files
inside RUN_DIR only, reached without following any symlink, so the run's state/token, passwords and logs never leave
the folder. Nothing is ever written: there are no write endpoints.
"""
import argparse
import base64
import errno
import hmac
import http.server
import json
import math
import os
import re
import secrets
import stat
import struct
import sys
import threading
import time
import urllib.parse

from . import __version__
from .client import Client, ClientError
from .server import ThreadingHTTPServer

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "dashboard")
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
          "/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"), "/icon.svg": ("icon.svg", "image/svg+xml")}
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
SECURITY_HEADERS = (("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
                                                "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
                    ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"))
# Per-agent counters an agent view may carry (shown on the member cards when present, ignored when absent); integers
# inside a "counters" object are shown too, under their own names.
COUNTERS = (("compactions", "compactions"), ("truncated_turns", "truncated turns"), ("reasoning_only_turns", "reasoning-only turns"))
ROUND = re.compile(r"round (\d+): row (\d+) (draft|revise)\b")                    # manager -> drawer
PICTURE = re.compile(r"row (\d+): match ([\d.]+)(?: -> ([\d.]+))?, (\d+) labels? missing: (.*)")  # picture -> team
LESSON = "new lesson: "                                                            # lessons -> team
MAX_TEXT = 4000
NOFOLLOW, DIRECTORY, NONBLOCK = getattr(os, "O_NOFOLLOW", 0), getattr(os, "O_DIRECTORY", 0), getattr(os, "O_NONBLOCK", 0)


# ---------------------------------------------------------------------------- reading inside the run folder only
def split_rel(rel):
    """'work/renders/a.png' -> ['work', 'renders', 'a.png']; None unless it is a plain relative path (no '..', '.',
    empty or hidden parts)."""
    if not isinstance(rel, str) or not rel or "\x00" in rel or "\\" in rel:
        return None
    parts = rel.split("/")
    if any(p in ("", ".", "..") or p.startswith(".") for p in parts):
        return None
    return parts


def open_inside(root, parts):
    """Open root/parts... for reading, one step at a time relative to the folder opened before, with O_NOFOLLOW on every
    step: a symlink anywhere on the way (even one swapped in after a check) makes it fail, and so does anything that is
    not a regular file (O_NONBLOCK: a FIFO cannot hang the server). Returns a file descriptor; raises OSError."""
    fd = os.open(root, os.O_RDONLY | DIRECTORY)
    try:
        for name in parts[:-1]:
            inner = os.open(name, os.O_RDONLY | DIRECTORY | NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = inner
        handle = os.open(parts[-1], os.O_RDONLY | NOFOLLOW | NONBLOCK, dir_fd=fd)
    finally:
        os.close(fd)
    if not stat.S_ISREG(os.fstat(handle).st_mode):
        os.close(handle)
        raise OSError(errno.EINVAL, "not a regular file")
    return handle


def png_size(head):
    """[width, height] from the first 24 bytes of a PNG file, else None."""
    if len(head) >= 24 and head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
        return list(struct.unpack(">II", head[16:24]))
    return None


def clean(value):
    """Make a value safe for JSON.parse in a browser: inf -> "inf" (a PSNR of identical pictures), nan -> None."""
    if isinstance(value, float):
        if math.isnan(value):
            return None
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return value
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    return value


def number(value):
    """int/float -> float; anything else (None, text, True) -> None. Keeps inf for clean() to name."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class Lines:
    """A JSON-lines file read as it grows: complete lines only (a line being written waits for its newline); a line that
    is not a JSON object is counted in `bad` and skipped; a file that shrank or was replaced is read again from the
    start (`resets` goes up)."""

    def __init__(self, root, parts, pick=None):
        self.root, self.parts, self.pick = root, parts, pick
        self.items, self.bad, self.pos, self.rest, self.ident, self.resets = [], 0, 0, b"", None, 0

    def reset(self, ident):
        self.items, self.bad, self.pos, self.rest, self.ident = [], 0, 0, b"", ident

    def read(self):
        """Read what was added since the last call; True when anything changed."""
        try:
            fd = open_inside(self.root, self.parts)
        except OSError:
            if self.ident is None:
                return False
            self.reset(None)  # the file is gone
            self.resets += 1
            return True
        changed = False
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            ident = (info.st_dev, info.st_ino)
            if self.ident is not None and (ident != self.ident or info.st_size < self.pos):
                self.reset(ident)
                self.resets += 1
                changed = True
            self.ident = ident
            if info.st_size <= self.pos:
                return changed
            handle.seek(self.pos)
            data = handle.read(info.st_size - self.pos)
        self.pos += len(data)
        lines = (self.rest + data).split(b"\n")
        self.rest = lines.pop()
        for raw in lines:
            if not raw.strip():
                continue
            try:
                item = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                item = None
            if not isinstance(item, dict):
                self.bad += 1
                changed = True
                continue
            item = self.pick(item) if self.pick else item
            if item is not None:
                self.items.append(item)
                changed = True
        return changed


class JsonFile:
    """A JSON file that is rewritten as a whole. summary.json is not written atomically: a half-written file keeps the
    last good value and is read again at the next poll."""

    def __init__(self, root, parts):
        self.root, self.parts = root, parts
        self.sig = self.value = None

    def read(self):
        try:
            fd = open_inside(self.root, self.parts)
        except OSError:
            changed = self.sig is not None
            self.sig = self.value = None
            return changed
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            sig = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            if sig == self.sig:
                return False
            data = handle.read()
        try:
            value = json.loads(data.decode("utf-8", "replace"))
        except ValueError:
            return False
        self.sig, self.value = sig, value
        return True


def chat_line(item):
    text = str(item.get("text", ""))
    return {"t": number(item.get("t")), "from": str(item.get("from", "")), "to": str(item.get("to", "")),
            "kind": str(item.get("kind") or "message"), "text": text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + " …"}


def thread_started(item):
    """codex/events.jsonl: keep only the thread.started events (one per Codex session)."""
    event = item.get("event")
    if isinstance(event, dict) and event.get("type") == "thread.started" and item.get("agent"):
        return {"agent": str(item["agent"]), "thread": str(event.get("thread_id") or "")}
    return None


# ---------------------------------------------------------------------------- the state model
def counters_of(view):
    out, seen = [], set()
    nested = view.get("counters") if isinstance(view.get("counters"), dict) else {}
    for key, label in COUNTERS:
        for source in (view, nested):
            value = count(source.get(key))
            if value is not None and key not in seen:
                out.append({"key": key, "label": label, "value": value})
                seen.add(key)
    for key in sorted(nested):  # counters this list does not know yet
        value = count(nested[key])
        if value is not None and key not in seen:
            out.append({"key": str(key), "label": str(key).replace("_", " "), "value": value})
    return out


def member(name, view, sessions, sessions_more, since=None, pending=()):
    stream = view.get("stream") if isinstance(view.get("stream"), dict) else {}
    words = str(stream.get("text") or "")
    return {"name": str(name), "state": str(view.get("state") or "unknown"), "tokens": count(view.get("tokens")),
            "turns": count(view.get("turns")), "sessions": sessions, "sessions_more": sessions_more, "since": since,
            "words": words[-300:], "words_kind": str(stream.get("kind") or ""), "words_from": "stream" if words else "",
            "pending": [str(p) for p in pending][:3], "counters": counters_of(view)}


def member_from_daemon(view):
    """An agent view from the daemon (core.Agent.view) -> a member card."""
    past = view.get("past_sessions") if isinstance(view.get("past_sessions"), list) else []
    sessions, more = count(view.get("sessions")), False
    if sessions is None:  # the view lists at most the last five earlier sessions
        sessions, more = 1 + len(past), len(past) >= 5
    pending = [p.get("description", "") for p in view.get("pending") or [] if isinstance(p, dict)]
    return member(view.get("name", "?"), view, sessions, more, since=number(view.get("since")), pending=pending)


def members_from_file(data, threads):
    """codex/agents.json ({name: {state, tokens, turns, stream}}) -> member cards; sessions = Codex threads started."""
    sessions = {}
    for item in threads:
        sessions.setdefault(item["agent"], set()).add(item["thread"])
    out = []
    for name, view in (data.items() if isinstance(data, dict) else []):
        if isinstance(view, dict):
            n = count(view.get("sessions"))
            if n is None and name in sessions:
                n = len(sessions[name])
            out.append(member(view.get("name") or name, view, n, False))
    return out


def finish_rows(rows):
    """{row: [points]} -> [{row, points, kept, best}]: kept[i] is the match of the version kept after point i."""
    out = []
    for row in sorted(rows):
        kept, best = [], None
        for p in rows[row]:
            if p["valid"] and p["accepted"] and p["match"] is not None:
                best = p["match"]
            kept.append(best)
        out.append({"row": row, "points": rows[row], "kept": kept, "best": best})
    return out


def rows_from_summary(turns):
    rows = {}
    for item in turns:
        if not isinstance(item, dict) or count(item.get("row")) is None:
            continue
        missing = item.get("missing") if isinstance(item.get("missing"), list) else None
        rows.setdefault(item["row"], []).append({
            "turn": count(item.get("turn")), "drawer": str(item.get("drawer") or ""), "kind": str(item.get("kind") or ""),
            "valid": bool(item.get("valid")), "accepted": bool(item.get("accepted")), "match": number(item.get("match")),
            "missing_count": None if missing is None else len(missing), "missing": None if missing is None else [str(x) for x in missing],
            "pending": False})
    return finish_rows(rows)


def rows_from_chat(chat, ended):
    """The same rows, live, from the conversation: a manager line opens a turn ("round N: row R draft|revise"), the
    program's picture line closes it with the match and the verdict; a turn that never got a picture line was invalid
    (the drawer's list could not be drawn) once the next turn starts or the run ends, and pending until then."""
    rows, current = {}, None
    for m in chat:
        if m["from"] == "manager":
            found = ROUND.match(m["text"])
            if found:
                if current is not None:
                    current["pending"] = False
                current = {"turn": int(found.group(1)), "drawer": m["to"], "kind": found.group(3), "valid": False,
                           "accepted": False, "match": None, "missing_count": None, "missing": None, "pending": True,
                           "row": int(found.group(2))}
                rows.setdefault(current["row"], []).append(current)
        elif m["from"] == "picture" and current is not None and current["pending"]:
            found = PICTURE.match(m["text"])
            if found and int(found.group(1)) == current["row"]:
                current.update(match=float(found.group(3) or found.group(2)), missing_count=int(found.group(4)), valid=True,
                               accepted=not found.group(5).startswith("rejected"), pending=False)
    if current is not None and ended:
        current["pending"] = False
    for points in rows.values():
        for p in points:
            p.pop("row")
    return finish_rows(rows)


def segment_start(chat):
    """Index of the line that started the last run in this conversation (a rerun into the same folder appends)."""
    start = 0
    for i, m in enumerate(chat):
        if m["from"] == "supervisor" and m["kind"] == "control" and m["text"].startswith("task:"):
            start = i
    return start


class RunFolder:
    """What the page shows, read from a run folder (and the daemon). refresh() re-reads only what changed and returns
    a version number that goes up whenever there is something new; state() is the page's JSON."""

    def __init__(self, path, socket_path=None, member_poll_s=1.0):
        self.root = os.path.realpath(path)
        if not os.path.isdir(self.root):
            raise ValueError(f"{path} is not a folder")
        self.client = Client(socket_path, timeout=3) if socket_path else None
        self.member_poll_s = member_poll_s
        self.lock = threading.Lock()
        self.prefix, self.version, self.epoch, self.chat_resets, self.cache = None, 0, 0, 0, None
        self.daemon_members, self.daemon_error, self.daemon_at, self.renders_stamp = [], None, None, None
        self.open_readers()

    def rel(self, *names):
        return "/".join(n for n in (self.prefix,) + names if n)

    def open_readers(self):
        """The work folder is RUN_DIR/work when that exists (it may appear after the dashboard started), else RUN_DIR."""
        prefix = "work" if os.path.isdir(os.path.join(self.root, "work")) else ""
        if prefix == self.prefix:
            return False
        self.prefix = prefix

        def parts(*names):
            return split_rel(self.rel(*names))
        self.chat = Lines(self.root, parts("chat.jsonl"), pick=chat_line)
        self.manifest = Lines(self.root, parts("renders", "manifest.jsonl"))
        self.threads = Lines(self.root, parts("codex", "events.jsonl"), pick=thread_started)
        self.summary = JsonFile(self.root, parts("summary.json"))
        self.agents = JsonFile(self.root, parts("codex", "agents.json"))
        self.chat_resets = 0
        self.epoch += 1
        return True

    def poll_daemon(self):
        if self.client is None:
            return False
        now = time.monotonic()
        if self.daemon_at is not None and now - self.daemon_at < self.member_poll_s:
            return False
        self.daemon_at = now
        members, error = self.daemon_members, None
        try:
            members = [member_from_daemon(v) for v in self.client.call("agent.list")["agents"] if isinstance(v, dict)]
        except (ClientError, OSError, ValueError, KeyError, TypeError) as exc:  # busy or gone: keep the last members
            error = f"{type(exc).__name__}: {exc}"
        changed = (members, error) != (self.daemon_members, self.daemon_error)
        self.daemon_members, self.daemon_error = members, error
        return changed

    def refresh(self):
        with self.lock:
            changed = self.open_readers()
            for reader in (self.chat, self.manifest, self.threads, self.summary, self.agents):
                changed = reader.read() or changed
            try:
                info = os.lstat(os.path.join(self.root, self.prefix, "renders"))
                renders = (info.st_ino, info.st_mtime_ns)  # a picture appeared or went away
            except OSError:
                renders = None
            if renders != self.renders_stamp:
                self.renders_stamp, changed = renders, True
            changed = self.poll_daemon() or changed
            if self.chat.resets != self.chat_resets:  # the conversation started over: pages need a new snapshot
                self.chat_resets = self.chat.resets
                self.epoch += 1
            if changed or self.cache is None:
                self.version += 1
                self.cache = clean(self.build())
            return self.version

    def state(self, chat_from=0):
        with self.lock:
            cache = self.cache
        out = dict(cache)
        out.update(chat=cache["chat"][chat_from:], chat_from=chat_from, now=round(time.time(), 3))
        return out

    # ---- pictures
    def image(self, rel):
        """{path, size} for an image file inside RUN_DIR that can be served, else None."""
        parts = split_rel(rel)
        if parts is None or os.path.splitext(parts[-1])[1].lower() not in IMAGE_TYPES:
            return None
        try:
            fd = open_inside(self.root, parts)
        except OSError:
            return None
        try:
            head = os.read(fd, 24)
        finally:
            os.close(fd)
        return {"path": "/".join(parts), "size": png_size(head)}

    def locate(self, path):
        """A picture path as the run wrote it (absolute, from wherever the run was) -> an image inside RUN_DIR: the same
        file if it is still inside, else renders/<name> (the run folder was moved or copied)."""
        if not isinstance(path, str) or not path:
            return None
        if os.path.isabs(path):
            first = os.path.relpath(os.path.realpath(path), self.root)
        else:
            first = os.path.normpath(self.rel(path))
        return self.image(first) or self.image(self.rel("renders", os.path.basename(path)))

    def pictures(self, entries, turns, since):
        out, originals = [], {}
        for item in entries:
            t = number(item.get("t"))
            if since is not None and t is not None and t < since:
                continue  # a picture from an earlier run in the same folder
            round_no, path = count(item.get("round")), item.get("path")
            row, point = turns.get(round_no, (None, None))
            if row is None:
                found = re.search(r"row(\d+)", os.path.basename(str(path)))
                row = int(found.group(1)) if found else None
            if row not in originals:
                originals[row] = (self.image(self.rel("renders", f"row{row}-original.png")) if row is not None else None) \
                    or self.image(self.rel("renders", "reference-small.png"))
            out.append({"round": round_no, "t": t, "row": row, "name": os.path.basename(str(path)), "ours": self.locate(path),
                        "original": originals[row], "drawer": point and point["drawer"], "kind": point and point["kind"],
                        "match": point and point["match"]})
        return out

    # ---- everything the page shows
    def build(self):
        chat = self.chat.items
        start = segment_start(chat)
        run = chat[start:]
        ended = any(m["kind"] == "end" for m in run)
        summary = self.summary.value if isinstance(self.summary.value, dict) and ended else {}
        if isinstance(summary.get("turns"), list):
            rows = rows_from_summary(summary["turns"])
        else:
            rows = rows_from_chat(run, ended)
        turns = {p["turn"]: (r["row"], p) for r in rows for p in r["points"]}
        lessons = summary.get("lessons")
        if isinstance(lessons, dict):
            lessons = [{"text": str(k), "count": count(v)} for k, v in lessons.items()]
        else:
            lessons = [{"text": m["text"][len(LESSON):], "count": None} for m in run if m["from"] == "lessons" and m["text"].startswith(LESSON)]
        given = summary.get("final") if isinstance(summary.get("final"), dict) else None
        final = None
        if given is not None:
            missing = given.get("missing")
            final = {"match": number(given.get("match")), "psnr": number(given.get("psnr")),
                     "missing": [str(x) for x in missing] if isinstance(missing, list) else None}
            if number(given.get("strict")) is not None:  # runs scored by scoring.py (layout_team.py --score)
                final["strict"] = number(given.get("strict"))
                final["score_mode"] = given.get("score_mode") if given.get("score_mode") in ("strict", "match") else None
        progress = None
        for i in range(len(run) - 1, -1, -1):
            m = run[i]
            found = ROUND.match(m["text"]) if m["from"] == "manager" else None
            if found:
                progress = {"round": int(found.group(1)), "row": int(found.group(2)), "kind": found.group(3), "member": m["to"], "t": m["t"]}
                later = run[i + 1:]
                handed = [k for k, x in enumerate(later) if x["to"] == "art"]
                if handed and not any(x["from"] == "art" for x in later[handed[-1] + 1:]):  # the art director has the row now
                    progress.update(kind="review", member="art", t=later[handed[-1]]["t"])
                break
        if self.client is not None:
            members, source = [dict(m) for m in self.daemon_members], "daemon"
        else:
            members, source = members_from_file(self.agents.value, self.threads.items), "codex" if self.agents.value is not None else None
        for m in members:  # nothing streamed yet (a fresh session): the member's last line in the conversation
            if not m["words"]:
                said = [c["text"] for c in run if c["from"] == m["name"]]
                if said:
                    m.update(words=said[-1][-300:], words_kind="said", words_from="chat")
        return {"version": self.version, "epoch": self.epoch,
                "run": {"name": os.path.basename(self.root), "work": self.prefix or ".", "started": run[0]["t"] if run else None,
                        "last": chat[-1]["t"] if chat else None, "ended": ended, "progress": progress, "has_chat": self.chat.ident is not None,
                        "members_from": source, "members_error": self.daemon_error, "bad_lines": self.chat.bad,
                        "earlier_lines": start, "summary": bool(summary)},
                "members": members, "chat_total": len(chat), "chat": chat, "rows": rows,
                "pictures": self.pictures(self.manifest.items, turns, run[0]["t"] if run and start else None),
                "lessons": lessons, "final": final}


# ---------------------------------------------------------------------------- HTTP
class Dashboard:
    def __init__(self, run_dir, host="127.0.0.1", port=8770, socket_path=None, poll_s=1.0, token=None, keepalive_s=15.0):
        self.run = RunFolder(run_dir, socket_path=socket_path, member_poll_s=poll_s)
        self.token = token or secrets.token_urlsafe(18)
        self.poll_s, self.keepalive_s = poll_s, keepalive_s
        self.stopping = threading.Event()
        self.web = ThreadingHTTPServer((host, port), make_handler(self))
        self.web.daemon_threads = True

    @property
    def url(self):
        host, port = self.web.server_address[:2]
        return f"http://{host}:{port}/#token={self.token}"

    def start(self):
        threading.Thread(target=self.web.serve_forever, daemon=True, name="herdr-py-dashboard").start()
        return self

    def stop(self):
        self.stopping.set()
        self.web.shutdown()
        self.web.server_close()


def make_handler(dash):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = f"herdr-py-dashboard/{__version__}"

        def log_message(self, fmt, *args):
            pass

        def authorized(self, query):
            header = self.headers.get("Authorization", "")
            given = header[7:] if header.startswith("Bearer ") else (query.get("token") or [""])[0]
            return hmac.compare_digest(given.encode(), dash.token.encode())

        def start_reply(self, status, ctype, length=None, extra=()):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            if length is not None:
                self.send_header("Content-Length", str(length))
            for name, value in SECURITY_HEADERS + tuple(extra):
                self.send_header(name, value)
            self.end_headers()

        def reply(self, status, obj):
            body = json.dumps(obj, ensure_ascii=False, allow_nan=False).encode()
            self.start_reply(status, "application/json; charset=utf-8", len(body), (("Cache-Control", "no-store"),))
            self.wfile.write(body)

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            if url.path in STATIC:
                name, ctype = STATIC[url.path]
                with open(os.path.join(WEB_DIR, name), "rb") as handle:
                    body = handle.read()
                self.start_reply(200, ctype, len(body), (("Cache-Control", "no-store"),))
                self.wfile.write(body)
                return
            if not url.path.startswith(("/api/", "/files/")):
                return self.reply(404, {"error": "not found"})
            if not self.authorized(urllib.parse.parse_qs(url.query)):
                return self.reply(401, {"error": "missing or wrong token: open the link the dashboard printed"})
            if url.path == "/api/state":
                dash.run.refresh()
                return self.reply(200, dash.run.state())
            if url.path == "/api/events":
                return self.events()
            if url.path.startswith("/files/"):
                return self.file(urllib.parse.unquote(url.path[len("/files/"):]))
            return self.reply(404, {"error": "not found"})

        def refuse(self):  # a read-only server: say so, for every method that would change something
            self.reply(405, {"error": "read-only: the dashboard has no write endpoints"})

        do_POST = do_PUT = do_PATCH = do_DELETE = refuse

        def file(self, rel):
            parts = split_rel(rel)
            ctype = IMAGE_TYPES.get(os.path.splitext(parts[-1])[1].lower()) if parts else None
            if ctype is None:
                return self.reply(404, {"error": "not found"})
            try:
                fd = open_inside(dash.run.root, parts)
            except OSError:
                return self.reply(404, {"error": "not found"})
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                etag = '"%x-%x"' % (info.st_mtime_ns, info.st_size)
                if self.headers.get("If-None-Match") == etag:
                    self.start_reply(304, ctype, extra=(("ETag", etag), ("Cache-Control", "no-cache")))
                    return
                self.start_reply(200, ctype, info.st_size, (("ETag", etag), ("Cache-Control", "no-cache")))
                try:
                    while True:
                        chunk = handle.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the page moved on (a slider dragged past this picture)

        def send_event(self, obj):
            self.wfile.write(b"data: " + json.dumps(obj, ensure_ascii=False, allow_nan=False).encode() + b"\n\n")
            self.wfile.flush()

        def events(self):
            """The whole state first ("snapshot"), then an "update" with what changed: new conversation lines only (from
            chat_from on) plus everything else, which is small. A conversation that started over sends a new snapshot."""
            try:
                self.start_reply(200, "text/event-stream; charset=utf-8", extra=(("Cache-Control", "no-store"),))
                self.wfile.write(b"retry: 3000\n\n")
                dash.run.refresh()
                state = dash.run.state()
                state["type"] = "snapshot"
                self.send_event(state)
                quiet = time.monotonic()
                while not dash.stopping.wait(dash.poll_s):
                    if dash.run.refresh() != state["version"]:
                        update = dash.run.state(chat_from=state["chat_total"])
                        if update["epoch"] != state["epoch"] or update["chat_total"] < state["chat_total"]:
                            update = dash.run.state()
                            update["type"] = "snapshot"
                        else:
                            update["type"] = "update"
                        state = update
                        self.send_event(state)
                        quiet = time.monotonic()
                    elif time.monotonic() - quiet > dash.keepalive_s:
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                        quiet = time.monotonic()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

    return Handler


def saved_pictures(value, found):
    """Paths of every picture record ({path, size}) anywhere in the page state."""
    if isinstance(value, dict):
        if isinstance(value.get("path"), str) and "size" in value:
            found.add(value["path"])
        for item in value.values():
            saved_pictures(item, found)
    elif isinstance(value, list):
        for item in value:
            saved_pictures(item, found)
    return found


def save_html(run_dir, out_path, socket_path=None):
    """The page as one HTML file with the run as it is now: style, script, state and pictures (data: URIs) inside, so it
    opens from disk with no server and no token; it does not update. Pictures are read as /files serves them: image files
    inside RUN_DIR only, without following symlinks. Model text is escaped so it cannot end the data's <script> element."""
    run = RunFolder(run_dir, socket_path=socket_path)
    run.poll_daemon()
    run.refresh()
    state = run.state()
    files = {}
    for rel in sorted(saved_pictures(state, set())):
        parts = split_rel(rel)
        ctype = IMAGE_TYPES.get(os.path.splitext(parts[-1])[1].lower()) if parts else None
        if ctype is None:
            continue
        try:
            fd = open_inside(run.root, parts)
        except OSError:
            continue  # gone since: the page says the picture could not be loaded
        with os.fdopen(fd, "rb") as handle:
            files[rel] = f"data:{ctype};base64," + base64.b64encode(handle.read()).decode("ascii")
    data = json.dumps({"state": state, "files": files}, ensure_ascii=False, allow_nan=False)
    data = data.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")

    def asset(name):
        with open(os.path.join(WEB_DIR, name), encoding="utf-8") as handle:
            return handle.read()

    script = asset("dashboard.js")
    if "</script" in script.lower():
        raise ValueError("dashboard.js contains </script>: it cannot be put inside the page")
    icon = base64.b64encode(asset("icon.svg").encode("utf-8")).decode("ascii")
    page = asset("index.html")
    for old, new in (('<link rel="stylesheet" href="dashboard.css">', "<style>\n" + asset("dashboard.css") + "</style>"),
                     ('<link rel="icon" href="icon.svg" type="image/svg+xml">', f'<link rel="icon" href="data:image/svg+xml;base64,{icon}">'),
                     ('<script src="dashboard.js"></script>',
                      f'<script type="application/json" id="run-data">{data}</script>\n<script>\n{script}</script>')):
        if page.count(old) != 1:
            raise ValueError(f"index.html changed: {old} is not there once")
        page = page.replace(old, new)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(page)
    os.replace(tmp, out_path)
    return {"path": out_path, "pictures": len(files), "bytes": len(page.encode("utf-8"))}


def parse_args(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m herdr_py.dashboard", description=__doc__.split("\n\n")[0])
    ap.add_argument("run_dir", metavar="RUN_DIR", help="a run folder (with work/ inside) or a team's --workdir")
    ap.add_argument("--port", type=int, default=8770, help="0 picks a free port")
    ap.add_argument("--host", default="127.0.0.1", help="address to listen on (default 127.0.0.1; reach it through an SSH tunnel)")
    ap.add_argument("--socket", help="herdr-py daemon socket: members' states from the daemon instead of codex/agents.json")
    ap.add_argument("--poll", type=float, default=1.0, help="seconds between looks at the run folder and the daemon")
    ap.add_argument("--html", metavar="FILE", help="save the page with the run as it is now into one HTML file and exit "
                                                  "(no server, no token; the file does not update)")
    return ap.parse_args(argv)


def main(argv=None):
    from .cli import utf8_stdout
    utf8_stdout()
    a = parse_args(argv)
    if a.html:
        try:
            saved = save_html(a.run_dir, a.html, socket_path=a.socket)
        except (ValueError, OSError, ClientError) as exc:
            print(f"herdr-py dashboard: {exc}", file=sys.stderr)
            return 2
        print(f"saved: {saved['path']} ({saved['pictures']} pictures, {saved['bytes'] / 1e6:.1f} MB); open it in a browser")
        return 0
    try:
        dash = Dashboard(a.run_dir, host=a.host, port=a.port, socket_path=a.socket, poll_s=a.poll)
    except (ValueError, OSError) as exc:
        print(f"herdr-py dashboard: {exc}", file=sys.stderr)
        return 2
    print(f"herdr-py dashboard {__version__}: {dash.run.root} (read-only)" + (f", members from {a.socket}" if a.socket else ""))
    if a.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"note: listening on {a.host}; anyone who can reach this port and has the link can read the run")
    print(f"open: {dash.url}", flush=True)
    try:
        dash.web.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        dash.stopping.set()
        dash.web.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
