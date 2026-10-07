#!/usr/bin/env python3
"""Slide team: two drawers take turns redrawing an infographic slide until an art director (a vision model) is satisfied.

Agents (herdr-py, one OpenCode server):
  drawA, drawB   text model; write make_deck.py -> deck.json with open-slide-py elements. They alternate: every redraw
                 is done by the *other* drawer, starting from the current files and NOTES.md (memory across sessions).
  art            vision model; looks at the original picture and our render, answers SCORE n/10 and FIX lines.
Program roles (no model): lint = `open_slide_py validate`, content = required labels present, render = open-slide-py
SVG export + headless Chrome screenshot. The supervisor (this program) routes every message and writes chat.jsonl
(who said what to whom), which the viewer shows and the video is made from.

usage: slide_team.py --socket SOCK --workdir DIR --reference ref.png --open-slide PATH [--rounds 4] [--target 8]
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from herdr_py.client import Client, ClientError  # noqa: E402

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
REQUIRED = ["LLM Serving: When to Split", "Prefill and Decode", "Prefill", "KV cache", "Decode", "Shared worker", "Chunked prefill",
            "Separate prefill + decode", "Long prompt", "Live streams", "Output tokens", "1 GPU", "KV", "local", "Chunk 3", "WAIT",
            "Prefill pool", "Decode pool", "KV copy", "TRANSFERRING", "New", "Based on TheAiEdge.io"]
EXAMPLE = ('{"schema_version": 1, "id": "llm-serving", "title": "LLM Serving", "width": 1920, "height": 1080, "slides": [{"id": "s1", '
           '"title": "LLM Serving", "background": "#F4F7FB", "elements": [{"id": "t1", "type": "text", "text": "Shared worker", "x": 40, '
           '"y": 160, "width": 400, "height": 44, "font_size": 32, "color": "#1B2430", "bold": true}, {"id": "gpu1", "type": "rect", '
           '"x": 640, "y": 190, "width": 480, "height": 260, "fill": "#EAF1F8", "stroke": "#6B7A8C", "stroke_width": 3, "radius": 12}, '
           '{"id": "l1", "type": "line", "x": 1200, "y": 300, "width": 670, "height": 0, "stroke": "#2F80ED", "stroke_width": 2}]}]}')
FIRST = """You are {name}, a slide drawer in a small team. Work only in the current folder.
Goal: recreate the infographic described in SPEC.md as ONE 1920x1080 slide with open-slide-py.
How: write make_deck.py (Python standard library only) that builds the deck as Python dicts and writes deck.json with json.dump,
then run: python3 make_deck.py   and   python3 -m open_slide_py validate deck.json   and fix any errors it prints.
Element types: text (text, font_size, color, bold, align), rect (fill, stroke, stroke_width, radius), ellipse, line (stroke,
stroke_width; width or height may be 0). Every element needs a unique id, type, x, y, width, height. Colours are #RRGGBB.
A minimal valid deck.json looks like this:
{example}
Use loops in make_deck.py for repeated token boxes. Keep NOTES.md short: what you drew and what is still missing.
You must run both commands yourself with the bash tool: the work is not done until deck.json exists and validate passes.
An art director will compare your slide with the original picture, and another drawer may continue your work."""
REDRAW = """You are {name}, a slide drawer. {prev} made the current version: read make_deck.py, NOTES.md and SPEC.md first.
Improve it; do not start over.
The art director compared our slide with the original picture (score {score}/10) and asks for:
{fixes}
Layout checker (open_slide_py validate): {lint}
Labels still missing from the slide: {missing}
Edit make_deck.py, then run python3 make_deck.py and python3 -m open_slide_py validate deck.json yourself with the bash tool,
and update NOTES.md. The work is not done until deck.json is regenerated and validate passes."""
ART = """You are the art director. Image 1 is the original infographic. Ignore the like, comment and share icons and the number 53
on its right edge: they belong to the phone app, not to the diagram. Image 2 is our slide (round {round}).
Compare them carefully: the three rows, the GPU and pool boxes, the token boxes, colours, labels, connector lines, alignment, spacing.
Reply in this exact format and nothing else:
SCORE: n/10
FIX: <row or area> - <one specific change>
(at most 5 FIX lines, the most important first; 10 means as clear and polished as the original)"""


def screenshot(chrome, svg, png, profile, timeout=90):
    """Headless Chrome screenshot of an SVG file. Chrome with a fresh profile can write the PNG and then not exit, so
    wait for the file to appear and stop changing, then end Chrome ourselves (it never touches the user's own profile)."""
    if os.path.exists(png):
        os.remove(png)
    proc = subprocess.Popen([chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
                             "--no-default-browser-check", "--disable-extensions", "--disable-component-update",
                             "--disable-background-networking", "--disable-sync", "--window-size=1920,1080",
                             "--user-data-dir=" + profile, "--screenshot=" + png, "file://" + svg],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline, last, steady = time.time() + timeout, -1, None
    try:
        while time.time() < deadline:
            size = os.path.getsize(png) if os.path.exists(png) else -1
            if size > 0 and (proc.poll() is not None or (size == last and steady and time.time() - steady > 1.0)):
                break
            if size > 0 and size == last:
                steady = steady or time.time()
            else:
                steady = None
            last = size
            if proc.poll() is not None and size <= 0:
                break
            time.sleep(0.25)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    return os.path.exists(png) and os.path.getsize(png) > 0


class Team:
    def __init__(self, a):
        self.a = a
        self.client = Client(a.socket, timeout=None)
        self.work = os.path.abspath(a.workdir)
        self.renders = os.path.join(self.work, "renders")
        os.makedirs(self.renders, exist_ok=True)
        self.chat_path = a.chat or os.path.join(self.work, "chat.jsonl")
        self.chat_file = open(self.chat_path, "a", encoding="utf-8", buffering=1)
        self.manifest = open(os.path.join(self.renders, "manifest.jsonl"), "a", encoding="utf-8", buffering=1)
        self.history = []
        self.ref_small = os.path.join(self.renders, "reference-small.png")
        subprocess.run(["sips", "-Z", "1000", a.reference, "--out", self.ref_small], stdout=subprocess.DEVNULL, check=True)
        shutil.copy(os.path.join(HERE, "SPEC.md"), os.path.join(self.work, "SPEC.md"))

    def chat(self, frm, to, text, kind="message"):
        self.chat_file.write(json.dumps({"t": round(time.time(), 2), "from": frm, "to": to, "kind": kind, "text": text}, ensure_ascii=False) + "\n")

    def run_turn(self, name, prompt, model, files=(), timeout=900):
        names = {x["name"] for x in self.client.call("agent.list")["agents"]}
        if name in names:
            self.client.call("agent.prompt", name=name, text=prompt, files=list(files))
        else:
            self.client.call("agent.start", name=name, prompt=prompt, model=model, files=list(files))
        start = time.time()
        while True:
            view = self.client.call("agent.get", name=name)
            if view["state"] in ("idle", "aborted", "error") and not view["followups_left"]:
                break
            if time.time() - start > timeout:
                self.client.call("agent.abort", name=name, reason="turn time limit")
                self.chat("supervisor", name, f"stopped: over the {timeout}s turn limit", "control")
                break
            time.sleep(1)
        texts = [m["text"] for m in self.client.call("agent.read", name=name, limit=4)["messages"]
                 if m["kind"] == "text" and m["role"] == "assistant"]
        return (texts[-1].strip() if texts else ""), view["state"]

    def lint(self):
        deck = os.path.join(self.work, "deck.json")
        if not os.path.exists(deck):
            return False, ["deck.json does not exist (run python3 make_deck.py)"], []
        env = dict(os.environ, PYTHONPATH=self.a.open_slide, PYTHONDONTWRITEBYTECODE="1")
        p = subprocess.run([sys.executable, "-m", "open_slide_py", "validate", deck], cwd=self.work, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=120)
        lines = [l.strip() for l in p.stdout.splitlines() if l.strip()]
        errors = [l for l in lines if "error" in l.lower()] if p.returncode else []
        warnings = [l for l in lines if "warn" in l.lower() or "警告" in l]
        return p.returncode == 0, errors or ([lines[-1]] if p.returncode and lines else []), warnings

    def render(self, round_no):
        env = dict(os.environ, PYTHONPATH=self.a.open_slide, PYTHONDONTWRITEBYTECODE="1")
        svg = os.path.join(self.renders, f"round-{round_no}.svg")
        png = os.path.join(self.renders, f"round-{round_no}.png")
        p = subprocess.run([sys.executable, "-S", "-m", "open_slide_py", "export", os.path.join(self.work, "deck.json"), svg, "--slide", "1"],
                           cwd=self.work, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=120)
        if p.returncode != 0:
            return None, p.stdout.strip()[-300:]
        if not screenshot(self.a.chrome, svg, png, os.path.join(self.renders, ".chrome-profile")):
            return None, "Chrome did not write the screenshot"
        small = os.path.join(self.renders, f"round-{round_no}-small.png")
        subprocess.run(["sips", "-Z", "1000", png, "--out", small], stdout=subprocess.DEVNULL, check=True)
        self.manifest.write(json.dumps({"t": round(time.time(), 2), "round": round_no, "path": png}) + "\n")
        return small, ""

    def content(self):
        try:
            deck = json.load(open(os.path.join(self.work, "deck.json"), encoding="utf-8"))
            texts = " | ".join(e.get("text", "") for s in deck.get("slides", []) for e in s.get("elements", []) if e.get("type") == "text")
        except (OSError, ValueError, AttributeError):
            return list(REQUIRED)
        return [label for label in REQUIRED if label.lower() not in texts.lower()]

    def run(self):
        a = self.a
        self.chat("supervisor", "team", f"task: recreate the infographic as one slide; up to {a.rounds} rounds, stop at art score >= {a.target}/10", "control")
        drawers = ["drawA", "drawB"]
        score, fixes, lint_text, missing = 0, [], "", []
        for round_no in range(1, a.rounds + 1):
            drawer = drawers[(round_no - 1) % 2]
            if round_no == 1:
                prompt = FIRST.format(name=drawer, example=EXAMPLE)
                self.chat("supervisor", drawer, "round 1: draw the slide from SPEC.md (open-slide-py, make_deck.py -> deck.json)")
            else:
                prev = drawers[round_no % 2]
                prompt = REDRAW.format(name=drawer, prev=prev, score=score, fixes="\n".join(fixes) or "(no specific fixes)",
                                       lint=lint_text or "no problems", missing=", ".join(missing) or "none")
                self.chat("supervisor", drawer, f"round {round_no}: improve {prev}'s version (score {score}/10); "
                                                f"{len(fixes)} fixes from art, {len(missing)} missing labels")
            reply, state = self.run_turn(drawer, prompt, a.draw_model, timeout=a.turn_timeout)
            self.chat(drawer, "supervisor", (reply or f"(no reply; {state})")[-400:])
            ok, errors, warnings = self.lint()
            lint_text = "; ".join(errors + warnings[:5]) if (errors or warnings) else ""
            self.chat("lint", drawers[round_no % 2], ("OK" if ok and not warnings else ("; ".join(errors) if errors else
                      f"{len(warnings)} warning(s): " + "; ".join(warnings[:3])))[:400], "check")
            missing = self.content()
            self.chat("content", drawers[round_no % 2], ("all required labels present" if not missing else
                      f"{len(missing)} missing: " + ", ".join(missing))[:400], "check")
            try:
                small, why = self.render(round_no) if ok or not errors else (None, "invalid deck")
            except Exception as exc:  # a broken tool must show up in the conversation, not kill the supervisor
                small, why = None, f"render failed: {type(exc).__name__}: {exc}"[:200]
            if small is None:
                score, fixes = 0, [f"FIX: whole slide - the deck could not be rendered ({why})"]
                self.chat("render", "art", f"no picture this round: {why}", "check")
            else:
                self.chat("render", "art", f"round-{round_no}.png ready", "check")
                try:
                    art_reply, _ = self.run_turn("art", ART.format(round=round_no), a.art_model, files=[self.ref_small, small], timeout=600)
                except Exception as exc:
                    art_reply = f"(art director failed: {type(exc).__name__}: {exc})"[:300]
                found = re.search(r"SCORE:\s*(\d+(?:\.\d+)?)\s*/\s*10", art_reply)
                score = float(found.group(1)) if found else 0
                fixes = [l.strip() for l in art_reply.splitlines() if l.strip().upper().startswith("FIX")][:5]
                self.chat("art", drawers[round_no % 2], f"SCORE {score:g}/10" + ("\n" + "\n".join(fixes) if fixes else
                          ("\n(no FIX lines; raw reply: " + art_reply[-200:] + ")")))
            self.history.append({"round": round_no, "drawer": drawer, "score": score, "missing": len(missing), "lint_ok": ok,
                                 "warnings": len(warnings), "fixes": fixes})
            if score >= a.target and not missing and ok:
                self.chat("supervisor", "team", f"accepted after round {round_no} (score {score:g}/10)", "control")
                break
        else:
            self.chat("supervisor", "team", f"stopped after {a.rounds} rounds; best score {max(h['score'] for h in self.history):g}/10", "control")
        self.chat("supervisor", "team", "finished", "end")
        json.dump({"rounds": self.history}, open(os.path.join(self.work, "summary.json"), "w"), indent=1)
        return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", required=True)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--reference", required=True)
    ap.add_argument("--open-slide", required=True, help="folder that contains the open_slide_py package")
    ap.add_argument("--rounds", type=int, default=4)
    ap.add_argument("--target", type=float, default=8)
    ap.add_argument("--draw-model", default="ollama/qwen3-8b-32k:latest")
    ap.add_argument("--art-model", default="ollama/qwen3-vl-32k:latest")
    ap.add_argument("--turn-timeout", type=int, default=900)
    ap.add_argument("--chrome", default=CHROME)
    ap.add_argument("--chat")
    return Team(ap.parse_args()).run()


if __name__ == "__main__":
    sys.exit(main())
