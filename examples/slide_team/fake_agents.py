"""Scripted members for dry runs and tests: no model, no network. Same contract as CodexAgents.run_turn.

The drawers answer from layout/reference_portrait.json (the checklist drawn by hand) with planted mistakes, so one run
goes through every path of the layout team: a draft the program cannot draw (fixed on a later turn), a draft without
its main box, a revision that lowers the match (rejected) and one equal to the reference row (accepted). The art
director answers DIFF lines when both pictures exist. The seed decides which rows get which mistakes, so repeated runs
differ the way real ones do. turns.jsonl records the kind of every turn (draft, fix, revise, art): the ground truth
that tests compare reports with.
"""
import json
import os
import re
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def reference_rows():
    with open(os.path.join(HERE, "layout", "reference_portrait.json"), encoding="utf-8") as handle:
        ref = json.load(handle)
    return {n: [c for c in ref if c["id"].startswith(f"r{n}.")] for n in (1, 2, 3)}


def block(comps):
    return "```json\n" + json.dumps(comps) + "\n```"


class Script:
    """What the scripted members say. One script per run: both drawers count the revisions of a row together."""

    def __init__(self, seed=1):
        self.seed, self.rows = seed, reference_rows()
        self.revisions, self.fixes = {}, {}

    def row(self, n):
        return json.loads(json.dumps(self.rows[n]))  # a copy the caller may change

    def reply(self, prompt, files=()):
        """(kind of turn, row, reply text)."""
        found = re.search(r"row (\d)", prompt)
        n = int(found.group(1)) if found else 1
        kind, text = self.answer(n, prompt, files)
        return kind, n, text

    def answer(self, n, prompt, files):
        if prompt.startswith("You are the art director"):
            if len(files) != 2 or not all(os.path.exists(path) for path in files):
                return "art", "I did not get both pictures."
            return "art", f"DIFF: row {n} token boxes - move them 10 px left\nDIFF: row {n} lanes - make the arrows longer"
        if prompt.startswith("Supervisor: the program cannot draw"):
            self.fixes[n] = self.fixes.get(n, 0) + 1
            comps = self.row(n)
            if self.seed % 2 == 0 and self.fixes[n] == 1:  # even seeds need a second fix
                del next(c for c in comps if c["type"] == "tokens")["x"]
                return "fix", block(comps)
            next(c for c in comps if c["id"] == f"r{n}.prompt")["x"] += 60
            return "fix", block(comps)
        if "Turn the checklist for row" in prompt:
            comps = self.row(n)
            if (n + self.seed) % 3 == 0:  # one row per run cannot be drawn: a lane without "y"
                del next(c for c in comps if c["type"] == "lane")["y"]
            else:  # a draft without its main box
                comps.remove(next(c for c in comps if c["type"] == "box"))
            return "draft", block(comps)
        if "Improve row" in prompt:
            self.revisions[n] = self.revisions.get(n, 0) + 1
            comps = self.row(n)
            if self.revisions[n] == 1 or (self.revisions[n] == 2 and (n + self.seed) % 2):
                return "revise", block(comps[: len(comps) // 2])  # half the row: mostly worse than the version kept
            return "revise", block(comps)
        return "other", "?"


class FakeAgents:
    def __init__(self, root, script=None):
        self.root, self.script = root, script or Script()
        os.makedirs(root, exist_ok=True)
        self.agents = {}
        self.lock = threading.Lock()
        self.turns = open(os.path.join(root, "turns.jsonl"), "a", encoding="utf-8", buffering=1)

    def close(self):
        self.turns.close()

    def forget(self, name):
        """Scripted members keep no conversation."""

    def run_turn(self, name, prompt, model=None, files=(), timeout=600):
        with self.lock:
            kind, row, text = self.script.reply(prompt, files)
            self.turns.write(json.dumps({"t": round(time.time(), 2), "agent": name, "kind": kind, "row": row, "model": model,
                                         "files": len(files)}) + "\n")
            agent = self.agents.setdefault(name, {"name": name, "state": "idle", "tokens": 0, "turns": 0,
                                                  "stream": {"kind": "text", "text": ""}})
            agent["tokens"] += (len(prompt) + len(text)) // 4  # about four characters per token
            agent["turns"] += 1
            agent["stream"] = {"kind": "text", "text": text[-300:]}
            tmp = os.path.join(self.root, "agents.json.tmp")
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self.agents, handle)
            os.replace(tmp, os.path.join(self.root, "agents.json"))
        return text, "idle"
