#!/usr/bin/env python3
"""Slide team: two drawers take turns redrawing an infographic slide until an art director (a vision model) is satisfied.

Agents (herdr-py, one OpenCode server):
  drawA, drawB   text model; write make_deck.py -> deck.json with open-slide-py elements. They alternate: every redraw
                 is done by the *other* drawer, starting from the current files and NOTES.md (memory across sessions).
  art            vision model; looks at the original picture and our render, answers SCORE n/10 and FIX lines.
Program roles (no model): build = run make_deck.py again in the agents' sandbox after every turn (so the checks judge the
slide the program makes now, never a deck.json an earlier round left behind), lint = `open_slide_py validate`, content =
required labels present, render = open-slide-py SVG export + headless Chrome screenshot. A drawer that stops on its
own without changing make_deck.py, or leaves it failing, is sent back with the reason (nudge, --nudges times per round). The supervisor (this program) routes every message and writes chat.jsonl
(who said what to whom), which the viewer shows and the video is made from.

usage: slide_team.py --socket SOCK --workdir DIR --reference ref.png --open-slide PATH [--rounds 4] [--target 8]
"""
import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from herdr_py.client import Client  # noqa: E402

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
PLAN = ["ROW 1 \"Shared worker\" (top 160): Long prompt + six P tokens, Live streams + A and B, the 1 GPU box with the Decode box "
        "and the KV grid (labels KV, local), the connector lines, and the Output tokens lanes New / A / B.",
        "ROW 2 \"Chunked prefill\" (top 470): the same parts as row 1, but the GPU box holds an orange \"Chunk 3\" box, the last two P "
        "tokens are orange, and the live streams wait (red || mark and red WAIT label); output label PREFILL.",
        "ROW 3 \"Separate prefill + decode\" (top 780): the Prefill pool (orange Prefill box, green KV cache squares, TRANSFERRING) and "
        "the Decode pool (green A B squares, blue Decode box), the KV copy label between them, dense A and B outputs; output label KV COPY.",
        "POLISH: fix the art director's points and any missing labels; make spacing even and nothing overlap."]
PLANNED = """You are {name}, a slide drawer. The manager gives you this round's part:
{assignment}
{prev} worked on the slide before you. First read make_deck.py, kit.py and SPEC.md. kit.py has the helpers:
text(x, y, w, h, s, size, color, bold, align), box(x, y, w, h, fill, stroke, radius), token(x, y, label, kind), tokens(x, y, labels, kind),
line(x, y, w, h, color, width) and dot(x, y). Token kinds: prompt, prefill, kv, decode.
Then rewrite the WHOLE make_deck.py with the write tool (do not use the edit tool): keep everything that is already there and add your part.
The art director's last review (score {score}/10):
{fixes}
Labels still missing: {missing}. Layout checker: {lint}.
Run python3 make_deck.py and python3 -m open_slide_py validate deck.json yourself with the bash tool and fix any error. Update NOTES.md."""
BUILD_NOTE = "After your turn the supervisor runs python3 make_deck.py itself: if it stops with an error, this round has no slide."
NUDGE = """Supervisor: you ended your turn, but {reason}.
Continue from where you are. Your part this round: {where}
Rewrite the WHOLE make_deck.py with the write tool (keep what is already there), run python3 make_deck.py and
python3 -m open_slide_py validate deck.json with the bash tool, and fix any error before you stop."""
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


def clip(text, n=200):
    """Keep the head and the tail: an error's last line says what is wrong."""
    return text if len(text) <= n else text[:60] + " ... " + text[-(n - 65):]


def digest(path):
    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return None


def findings(stdout, deck=None):
    """`open_slide_py validate` prints a JSON list of findings; turn it into (errors, warnings) lines that name the element by
    its id, so a drawer knows what to move. None when the output is not that list (the caller falls back to raw lines)."""
    try:
        items = json.loads(stdout)
    except ValueError:
        return None
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        return None
    errors, warnings = [], []
    for item in items:
        path = str(item.get("path", ""))
        where = path
        found = re.search(r"slides\[(\d+)\]\.elements\[(\d+)\]", path)
        if found and deck:
            try:
                where = deck["slides"][int(found.group(1))]["elements"][int(found.group(2))]["id"] + path[found.end():]
            except (KeyError, IndexError, TypeError):
                pass
        line = f"{item.get('code', '?')} at {where}: {item.get('message', '')}"
        (errors if item.get("severity") == "error" else warnings).append(line)
    return errors, warnings


def nudge_reason(state, changed, built, build_text):
    """Why the supervisor sends a drawer back to work, or None. Only a drawer that stopped on its own is nudged: one stopped
    at the time limit has used its turn."""
    if state != "idle":
        return None
    if not changed:
        return "make_deck.py is unchanged"
    if not built:
        return build_text
    return None


def build(work, argv, timeout=120):
    """Run make_deck.py from scratch: returns (ok, message). The old deck.json is removed first, so a program that fails
    leaves no slide behind instead of an earlier round's deck.json (the morning run judged round 1's file for 3 rounds)."""
    deck = os.path.join(work, "deck.json")
    if os.path.exists(deck):
        os.remove(deck)
    try:
        p = subprocess.run(argv, cwd=work, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"make_deck.py did not finish within {timeout}s"
    except OSError as exc:
        return False, f"could not run the build command: {exc}"
    lines = [l.rstrip() for l in p.stdout.splitlines() if l.strip()]
    if p.returncode != 0:
        return False, "make_deck.py stopped with an error: " + " | ".join(lines[-4:])[-300:]
    if not os.path.exists(deck):
        return False, "make_deck.py ran but did not write deck.json"
    return True, "make_deck.py ran; deck.json rebuilt"


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
        if a.plan == "rows":  # a starter kit, as a real project would provide: helpers plus a skeleton with the header done
            for name in ("kit.py", "make_deck.py"):
                shutil.copy(os.path.join(HERE, "scaffold", name), os.path.join(self.work, name))

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

    def build(self):
        if not self.a.build_cmd:
            return True, "not rebuilt (no --build-cmd), so the checks judge deck.json as the drawer left it"
        return build(self.work, shlex.split(self.a.build_cmd))

    def lint(self):
        deck = os.path.join(self.work, "deck.json")
        if not os.path.exists(deck):
            return False, ["deck.json does not exist (run python3 make_deck.py)"], []
        env = dict(os.environ, PYTHONPATH=self.a.open_slide, PYTHONDONTWRITEBYTECODE="1")
        p = subprocess.run([sys.executable, "-m", "open_slide_py", "validate", deck], cwd=self.work, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=120)
        try:
            with open(deck, encoding="utf-8") as handle:
                parsed = findings(p.stdout, json.load(handle))
        except (OSError, ValueError):
            parsed = findings(p.stdout)
        if parsed is not None:
            errors, warnings = parsed
            return p.returncode == 0, errors or ([f"validate exited with code {p.returncode}"] if p.returncode else []), warnings
        lines = [l.strip() for l in (p.stdout + p.stderr).splitlines() if l.strip()]  # not the JSON list: pass the raw lines on
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
        shutil.copy(os.path.join(self.work, "deck.json"), os.path.join(self.renders, f"round-{round_no}.json"))
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
            step = None
            if a.plan == "rows":
                step = PLAN[min(round_no, len(PLAN)) - 1]
                prev = "The manager (it set up kit.py and the skeleton)" if round_no == 1 else drawers[round_no % 2]
                prompt = PLANNED.format(name=drawer, assignment=step, prev=prev, score=score,
                                        fixes="\n".join(fixes) or "(no review yet)", missing=", ".join(missing) or "none",
                                        lint=lint_text or "no problems")
                self.chat("manager", drawer, f"round {round_no}: {step.split(':')[0]}" + (f" + {len(fixes)} fixes from art" if fixes else ""))
            elif round_no == 1:
                prompt = FIRST.format(name=drawer, example=EXAMPLE)
                self.chat("supervisor", drawer, "round 1: draw the slide from SPEC.md (open-slide-py, make_deck.py -> deck.json)")
            else:
                prev = drawers[round_no % 2]
                prompt = REDRAW.format(name=drawer, prev=prev, score=score, fixes="\n".join(fixes) or "(no specific fixes)",
                                       lint=lint_text or "no problems", missing=", ".join(missing) or "none")
                self.chat("supervisor", drawer, f"round {round_no}: improve {prev}'s version (score {score}/10); "
                                                f"{len(fixes)} fixes from art, {len(missing)} missing labels")
            if a.build_cmd:
                prompt += "\n" + BUILD_NOTE
            before = digest(os.path.join(self.work, "make_deck.py"))
            reply, state = self.run_turn(drawer, prompt, a.draw_model, timeout=a.turn_timeout)
            self.chat(drawer, "supervisor", (reply or f"(no reply; {state})")[-400:])
            built, build_text = self.build()
            self.chat("build", "supervisor", build_text, "check")
            nudges = 0
            while nudges < a.nudges:  # the drawer stopped on its own without finishing: send it back with the reason
                reason = nudge_reason(state, digest(os.path.join(self.work, "make_deck.py")) != before, built, build_text)
                if not reason:
                    break
                nudges += 1
                self.chat("supervisor", drawer, f"nudge: {clip(reason)}; continue from where you are", "control")
                text = NUDGE.format(reason=reason, where=step or "the art director's fixes and the missing labels in your last instructions")
                reply, state = self.run_turn(drawer, text + ("\n" + BUILD_NOTE if a.build_cmd else ""), a.draw_model,
                                             timeout=min(a.turn_timeout, 600))
                self.chat(drawer, "supervisor", (reply or f"(no reply; {state})")[-400:])
                built, build_text = self.build()
                self.chat("build", "supervisor", build_text, "check")
            if built:
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
            else:  # no slide this round: say why, instead of judging a deck.json that an earlier round left behind
                ok, warnings, missing, score = False, [], list(REQUIRED), 0
                lint_text = build_text
                fixes = [f"FIX: make_deck.py - {build_text}; make it run before anything else"]
                self.chat("render", "art", "no picture this round: make_deck.py did not run", "check")
            self.history.append({"round": round_no, "drawer": drawer, "built": built, "nudges": nudges, "score": score, "missing": len(missing), "lint_ok": ok,
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
    ap.add_argument("--nudges", type=int, default=1, help="times per round the supervisor sends a drawer that stopped early back to work")
    ap.add_argument("--build-cmd", help="runs make_deck.py where the agents run, e.g. 'docker exec -w /work NAME python3 make_deck.py'")
    ap.add_argument("--plan", choices=["none", "rows"], default="none",
                    help="rows: give the drawers a starter kit and let the manager assign one row per round")
    return Team(ap.parse_args()).run()


if __name__ == "__main__":
    sys.exit(main())
