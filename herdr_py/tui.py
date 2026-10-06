"""Full-screen terminal dashboard for a running herdr-py daemon.

Keys: up/down (or k/j) select an agent; a approve once, A approve always, r reject (the selected agent's oldest pending
request); p type a prompt (Enter sends, Esc cancels); x abort (asks y/n); q quit. Quitting only closes the dashboard:
the daemon and the agents keep running.
"""
import collections
import os
import select
import shutil
import sys
import termios
import threading
import time
import tty

from .client import ClientError
from .display import row, tail, text_width

LABEL = {"starting": ("starting", "30;47"), "working": ("working", "30;43"), "retry": ("retry", "30;45"),
         "blocked": ("NEEDS YOU", "97;41"), "idle": ("idle", "30;42"), "aborted": ("aborted", "97;100"), "error": ("error", "97;41")}
TONE = {"ok": "32", "bad": "31", "warn": "1;31", "info": "36", "dim": "2", "": ""}


class Dashboard:
    def __init__(self, client):
        self.client = client
        self.agents = []
        self.selected = 0
        self.log = collections.deque(maxlen=50)
        self.mode = None          # None | "prompt" | "confirm-abort"
        self.buffer = ""
        self.message = ""
        self.dirty = threading.Event()
        self.lock = threading.Lock()
        self.alive = True
        self.connected = True

    # ------------------------------------------------------------ data
    def refresh(self):
        try:
            agents = self.client.call("agent.list")["agents"]
        except ClientError as exc:
            self.message = f"daemon: {exc}"
            self.connected = False
            return
        with self.lock:
            self.agents = agents
            self.selected = min(self.selected, max(0, len(agents) - 1))
            self.connected = True
        self.dirty.set()

    def follow(self):
        """Background thread: subscribe to events; refresh the agent list when something changes."""
        while self.alive:
            try:
                for event in self.client.events():
                    if not self.alive:
                        return
                    kind = event.get("type")
                    if kind in ("permission.asked", "permission.decided", "agent.state"):
                        self.log.append((time.strftime("%H:%M:%S"), event))
                    if kind == "events_lost":
                        self.log.append((time.strftime("%H:%M:%S"), {"type": "events_lost", "agent": "-"}))
                    if kind != "agent.output" or time.time() - getattr(self, "_last", 0) > 0.25:
                        self._last = time.time()
                        self.refresh()
            except ClientError as exc:
                self.message = f"daemon: {exc}"
                self.connected = False
                self.dirty.set()
                time.sleep(1)

    def current(self):
        with self.lock:
            return self.agents[self.selected] if self.agents else None

    def act(self, method, **params):
        try:
            self.client.call(method, **params)
            self.message = f"{method} sent"
        except ClientError as exc:
            self.message = str(exc)
        self.refresh()

    # ------------------------------------------------------------ keys
    def key(self, k):
        agent = self.current()
        if self.mode == "prompt":
            if k in ("\r", "\n"):
                if agent and self.buffer.strip():
                    self.act("agent.prompt", name=agent["name"], text=self.buffer.strip())
                self.mode, self.buffer = None, ""
            elif k == "\x1b":
                self.mode, self.buffer = None, ""
            elif k in ("\x7f", "\b"):
                self.buffer = self.buffer[:-1]
            elif k.isprintable():
                self.buffer += k
            return
        if self.mode == "confirm-abort":
            if k in ("y", "Y") and agent:
                self.act("agent.abort", name=agent["name"])
            self.mode = None
            return
        if k in ("q", "\x03"):
            self.alive = False
        elif k in ("\x1b[A", "k"):
            self.selected = max(0, self.selected - 1)
        elif k in ("\x1b[B", "j"):
            self.selected = min(len(self.agents) - 1, self.selected + 1) if self.agents else 0
        elif k in ("a", "A", "r") and agent:
            if not agent["pending"]:
                self.message = f"{agent['name']} has nothing waiting"
            else:
                reply = {"a": "once", "A": "always", "r": "reject"}[k]
                self.act("permission.reply", id=agent["pending"][0]["id"], reply=reply, by="tui")
        elif k == "p" and agent:
            self.mode, self.buffer = "prompt", ""
        elif k == "x" and agent:
            self.mode = "confirm-abort"
        self.dirty.set()

    # ------------------------------------------------------------ drawing
    def frame(self, cols, rows):
        with self.lock:
            agents = list(self.agents)
            selected = self.selected
        title = " herdr-py  " + (f"{len(agents)} agent(s)" if self.connected else "DISCONNECTED")
        clock = time.strftime("%H:%M:%S ")
        lines = [row([(title + " " * max(1, cols - text_width(title) - len(clock)) + clock, "1;97;44")], cols)]
        log_rows = min(8, max(3, rows // 6))
        body = rows - 1 - 2 - log_rows
        pane = max(4, body // max(1, len(agents))) if agents else body
        for i, a in enumerate(agents):
            label, color = LABEL.get(a["state"], (a["state"], "0"))
            head = [(f"{'>' if i == selected else ' '} {a['name']:<10}", "1;7" if i == selected else "1"), (f" {label} ", color),
                    (f" {a['seconds_in_state']:>5.0f}s  {a['tokens']:,} tok  turn {a['turns']}", "2")]
            block = [row(head, cols)]
            for p in a["pending"][:2]:
                block.append(row([(f"   ? {'child ' if p['child'] else ''}{p['description']}", "1;31"), (f"  [{p['id'][-6:]}]", "2")], cols))
            for item in a["activity"][-(pane - 2 - len(block) + 1):]:
                block.append(row([("   " + item["text"], TONE.get(item["tone"], ""))], cols))
            block = block[:pane - 1]
            while len(block) < pane - 1:
                block.append(" " * cols)
            stream = a["stream"]
            block.append(row([(f"   {stream['kind'] or 'output'}: ", "2;36"), (tail(stream["text"], cols - 14), "2")], cols)
                         if stream["text"] else " " * cols)
            lines += block[:pane]
        lines = lines[:1 + body] + [" " * cols] * max(0, 1 + body - len(lines))
        lines.append(row([(" events", "1;97;44"), (" " * cols, "97;44")], cols))
        for t, e in list(self.log)[-log_rows:]:
            text = {"permission.asked": lambda: f"{e.get('agent')}: asks {e.get('description', '')} ({e.get('policy')})",
                    "permission.decided": lambda: f"{e.get('agent')}: {e.get('reply')} by {e.get('by')}",
                    "agent.state": lambda: f"{e.get('agent')}: {e.get('state')} ({e.get('reason', '')})",
                    "events_lost": lambda: "events lost: dashboard fell behind"}.get(e.get("type"), lambda: str(e))()
            lines.append(row([(f" {t} ", "2"), (text, "")], cols))
        lines = lines[:rows - 1] + [" " * cols] * max(0, rows - 1 - len(lines))
        if self.mode == "prompt":
            footer = row([(" prompt> ", "1;30;43"), (" " + self.buffer + "_", "")], cols)
        elif self.mode == "confirm-abort":
            footer = row([(" abort the selected agent? y/n ", "1;97;41")], cols)
        else:
            footer = row([(" up/down select  a approve  A always  r reject  p prompt  x abort  q quit ", "30;47"),
                          ("  " + self.message, "2")], cols)
        lines.append(footer)
        return "\x1b[H" + "".join(f"\x1b[{i + 1};1H{line}" for i, line in enumerate(lines[:rows]))


def read_key(fd):
    data = os.read(fd, 1).decode(errors="replace")
    if data == "\x1b":
        ready, _, _ = select.select([fd], [], [], 0.03)
        if ready:
            data += os.read(fd, 2).decode(errors="replace")
    return data


def run_tui(client):
    board = Dashboard(client)
    board.refresh()
    threading.Thread(target=board.follow, daemon=True).start()
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    out = sys.stdout
    try:
        tty.setcbreak(fd)
        out.write("\x1b[?1049h\x1b[?25l")
        last = 0
        while board.alive:
            ready, _, _ = select.select([fd], [], [], 0.25)
            if ready:
                board.key(read_key(fd))
            if board.dirty.is_set() or time.time() - last > 1:
                board.dirty.clear()
                cols, rows = shutil.get_terminal_size((100, 32))
                out.write(board.frame(cols, rows))
                out.flush()
                last = time.time()
    finally:
        board.alive = False
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        out.write("\x1b[?25h\x1b[?1049l")
        out.flush()
    print("dashboard closed; the daemon and its agents keep running (`herdr-py list`).")
    return 0
