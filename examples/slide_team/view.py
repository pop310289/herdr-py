#!/usr/bin/env python3
"""Terminal view of a slide-team run: agents at the top, the conversation (who said what to whom) below.

Reads the herdr-py daemon (agent states) and chat.jsonl written by slide_team.py. Exits a few seconds after the run ends.
usage: view.py --socket SOCK --chat chat.jsonl
"""
import argparse
import json
import os
import shutil
import sys
import textwrap
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from herdr_py.client import Client, ClientError  # noqa: E402
from herdr_py.display import row, tail, text_width  # noqa: E402

ROLE = {"manager": "1;94", "supervisor": "1;97", "drawA": "36", "drawB": "35", "art": "33", "lint": "32", "content": "32", "render": "32", "team": "1;97"}
STATE = {"starting": ("start", "30;47"), "working": ("working", "30;43"), "retry": ("retry", "30;45"), "blocked": ("asks", "97;41"),
         "idle": ("idle", "30;42"), "aborted": ("stopped", "97;100"), "error": ("error", "97;41")}


def wrap(text, width):
    out = []
    for para in str(text).splitlines() or [""]:
        out += textwrap.wrap(para, width=width, break_long_words=True, break_on_hyphens=False) or [""]
    return out


def frame(cols, rows, agents, chat, started):
    rounds = [m for m in chat if m["from"] in ("supervisor", "manager") and m["text"].startswith("round ")]
    scores = [m["text"].split()[1] for m in chat if m["from"] == "art" and m["text"].startswith("SCORE")]
    left = f" herdr-py · slide team · round {len(rounds)}"
    right = ("score " + " > ".join(s.split("/")[0] for s in scores) + "/10 " if scores else "") + time.strftime("%H:%M:%S ")
    lines = [row([(left + " " * max(1, cols - text_width(left) - text_width(right)) + right, "1;97;44")], cols)]
    for name in ("drawA", "drawB", "art"):
        a = agents.get(name)
        if not a:
            lines.append(row([(f" {name:<6}", ROLE[name]), (" waiting to be called", "2")], cols))
            continue
        label, color = STATE.get(a["state"], (a["state"], "0"))
        lines.append(row([(f" {name:<6}", "1;" + ROLE[name]), (f" {label} ", color), (f" {a['tokens']:>7,} tok ", "2"),
                          (tail(a["stream"]["text"], max(0, cols - 34)) if a["stream"]["text"] else "", "2")], cols))
    checks = {m["from"]: m["text"] for m in chat if m["from"] in ("lint", "content", "render")}
    lines.append(row([(" checks ", "1;32"), ("lint: " + checks.get("lint", "-")[:22], "2"), ("  labels: " + checks.get("content", "-")[:18], "2")], cols))
    lines.append(row([(" conversation " + "─" * cols, "90")], cols))
    body = []
    for m in chat[-60:]:
        stamp = time.strftime("%H:%M:%S", time.localtime(m["t"]))
        head = [(f" {stamp} ", "2"), (m["from"], "1;" + ROLE.get(m["from"], "0")), (" → ", "2"), (m["to"], ROLE.get(m["to"], "0"))]
        body.append(row(head, cols))
        for text in wrap(m["text"], cols - 4)[:4]:
            body.append(row([("   " + text, ROLE.get(m["from"], "") if m["kind"] != "check" else "2")], cols))
    room = rows - len(lines)
    lines += body[-room:]
    lines += [" " * cols] * (rows - len(lines))
    return "\x1b[H" + "".join(f"\x1b[{i + 1};1H{line}" for i, line in enumerate(lines[:rows]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", required=True)
    ap.add_argument("--chat", required=True)
    a = ap.parse_args()
    client = Client(a.socket, timeout=10)
    chat, pos, ended, started = [], 0, None, time.time()
    sys.stdout.write("\x1b[?25l\x1b[2J")
    while True:
        if os.path.exists(a.chat):
            with open(a.chat, encoding="utf-8") as handle:
                handle.seek(pos)
                for line in handle:
                    if line.endswith("\n"):
                        chat.append(json.loads(line))
                        pos += len(line.encode("utf-8"))
        try:
            agents = {x["name"]: x for x in client.call("agent.list")["agents"]}
        except ClientError:
            agents = {}
        cols, rows = shutil.get_terminal_size((66, 34))
        sys.stdout.write(frame(cols, rows, agents, chat, started))
        sys.stdout.flush()
        if any(m["kind"] == "end" for m in chat):
            ended = ended or time.time()
            if time.time() - ended > 4:
                break
        time.sleep(0.5)
    sys.stdout.write("\x1b[?25h\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
