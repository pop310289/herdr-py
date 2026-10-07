#!/usr/bin/env python3
"""Layout team: small models describe each row of the infographic as a JSON list of components; the program draws, checks
and keeps the best version.

Why this shape: in the first three runs, 8B drawers asked to write a whole drawing program made syntax errors, misused
helpers, dropped finished work and still reported success. Here a drawer only turns a checklist
(layout/SPEC_portrait.md) into JSON; components.py draws it as SVG; the program checks it (errors say how to fix them),
compares the picture with the original square by square (imgcmp.py) and accepts a new version only when the match
improves. The art director (vision model) compares the same row of both pictures and suggests changes; the program
decides. Every mistake the program catches goes into a lessons list that is put at the top of every later prompt.

Every turn starts a new session by default (--sessions fresh): the program puts the checklist, the current list, the
lessons and the notes into each prompt, so no member depends on a conversation that OpenCode may compact at any moment.
Per row: drawA drafts -> check (errors go back to the same drawer, up to --fixes times) -> picture match -> art notes ->
drawB revises -> keep the better -> art notes -> drawA revises ... (--revisions per row). chat.jsonl records who said
what to whom (view.py shows it; the video is made from it).
usage: layout_team.py --socket SOCK --workdir DIR --reference ref.png --open-slide PATH [--revisions 2]
       layout_team.py --backend codex --workdir DIR --reference ref.png --open-slide PATH   (Codex CLI members)
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import components as C  # noqa: E402
from codex_agents import CodexAgents  # noqa: E402
import imgcmp  # noqa: E402
import scoring  # noqa: E402
import slide_team  # noqa: E402

SIZE = (1206, 1441)
ROWS = {1: (152, 558), 2: (558, 960), 3: (960, 1400)}
ZONES = (("left", 0, 370), ("middle", 370, 780), ("right", 780, 1206))
ROW_LABELS = {1: ["Long prompt", "Live streams", "1 GPU", "Decode", "KV", "local", "Output tokens", "Schematic timing", "New"],
              2: ["Long prompt", "Live streams", "WAIT", "1 GPU", "Chunk 3", "KV", "local", "Output tokens", "PREFILL", "New"],
              3: ["Long prompt", "Live streams", "Prefill pool", "Prefill", "KV cache", "TRANSFERRING", "copy", "Decode pool",
                  "Decode", "Output tokens", "KV COPY", "New"]}
LABEL_WEIGHT = 0.03  # score = picture match - 0.03 per missing label


def phone_icons(x, y):
    """The original is a phone screenshot: like, comment and share icons sit on its lower right edge."""
    return x >= 1075 and y >= 965


DRAFT = """You are {name}, a slide drawer. Turn the checklist for row {n} into a JSON list of components: one checklist line
becomes one component (two when the line names two things). Do not write code, do not use tools, do not skip lines.
{lessons}
{guide}

{checklist}
Reply with ONLY one ```json block that contains the list."""
REVISE = """You are {name}, a slide drawer. Improve row {n} of the slide. {prev} made the current list below; change what the
notes ask and keep everything else. Do not write code and do not use tools.
{lessons}
What the program found when it compared our picture with the original:
{program}
What the art director saw (it looked at both pictures):
{art}
{guide}

{checklist}
Current list:
```json
{current}
```
Reply with ONLY one ```json block that contains the whole improved list for row {n}."""
FIX = """Supervisor: the program cannot draw this list for row {n} yet:
{errors}
{guide}

{checklist}
The list to fix:
```json
{previous}
```
Fix exactly these problems, keep the rest, and reply with ONLY one ```json block that contains the whole list for row {n}."""
ART = """You are the art director. Image 1 is row {n} of the original infographic. Image 2 is the same row of our drawing, at
the same scale. Ignore shading, shadows and any phone icons on the right edge of image 1.
List at most 3 differences that matter, the most important first, one per line, in this form:
DIFF: <part> - <what to change in our drawing, with a direction or a size>
If the two rows look the same, reply SAME."""


class Lessons:
    """Mistakes the program caught in this run; rendered at the top of every later drawer prompt (forced, not optional)."""

    def __init__(self):
        self.items = {}

    def add(self, text):
        new = text not in self.items
        self.items[text] = self.items.get(text, 0) + 1
        return new

    def text(self):
        if not self.items:
            return ""
        return ("Mistakes already made in this run; do not repeat them:\n"
                + "\n".join(f"- {t} ({n} time{'s' if n > 1 else ''})" for t, n in self.items.items()))


def lesson_of(error, reply):
    """'r1.lane1: missing "y"' -> 'a lane: missing "y"' (the type is what generalises; the id is this list's own)."""
    cid, _, rest = error.partition(": ")
    kinds = {c.get("id"): c.get("type") for c in reply if isinstance(c, dict)} if isinstance(reply, list) else {}
    return f"a {kinds[cid]}: {rest}" if cid in kinds and kinds[cid] and rest else error


def score_of(match, missing):
    return match - LABEL_WEIGHT * len(missing)


def better(new, best, epsilon=0.002):
    """Keep-best rule: a new version replaces the best only when its score is higher by more than epsilon."""
    return best is None or new["score"] > best["score"] + epsilon


def art_notes(reply):
    lines = [l.strip() for l in reply.splitlines() if l.strip().upper().startswith("DIFF")]
    return lines[:3], (not lines and "SAME" in reply.upper())


def summary_of(comps):
    ids = ", ".join(c.get("id", "?") for c in comps[:6] if isinstance(c, dict))
    return f"{len(comps)} components: {ids}" + (", ..." if len(comps) > 6 else "")


class LayoutTeam(slide_team.Team):
    def __init__(self, a):  # pylint: disable=super-init-not-called  (only chat and run_turn are shared with Team)
        self.a = a
        self.work = os.path.abspath(a.workdir)
        if a.backend == "codex":  # Codex CLI sessions instead of OpenCode agents: same turns, same checks, same scores
            self.codex = CodexAgents(os.path.join(self.work, "codex"), a.codex, a.codex_model, fresh=a.sessions == "fresh")
            self.run_turn = self.codex.run_turn
            a.draw_model = a.art_model = a.codex_model
        else:
            if not a.socket:
                raise SystemExit("--socket is needed with --backend opencode")
            self.client = slide_team.Client(a.socket, timeout=None)
        self.renders = os.path.join(self.work, "renders")
        os.makedirs(self.renders, exist_ok=True)
        self.chat_file = open(a.chat or os.path.join(self.work, "chat.jsonl"), "a", encoding="utf-8", buffering=1)
        self.manifest = open(os.path.join(self.renders, "manifest.jsonl"), "a", encoding="utf-8", buffering=1)
        spec = open(os.path.join(HERE, "layout", "SPEC_portrait.md"), encoding="utf-8").read()
        shutil.copy(os.path.join(HERE, "layout", "SPEC_portrait.md"), os.path.join(self.work, "SPEC.md"))
        self.checklist = {n: spec[spec.index(f"## Row {n}"):spec.index(f"## Row {n + 1}") if n < 3 else len(spec)].strip() for n in ROWS}
        base_errors, _, self.base = C.check(json.load(open(os.path.join(HERE, "layout", "base_portrait.json"))), size=SIZE)
        assert not base_errors, base_errors
        self.original = imgcmp.read_png(a.reference)
        if self.original[:2] != SIZE:
            raise SystemExit(f"the original must be {SIZE[0]} x {SIZE[1]} pixels, got {self.original[0]} x {self.original[1]}")
        self.original_cells = imgcmp.cells(self.original, phone_icons)
        self.prepared = scoring.prepare(self.original, phone_icons)  # the original's squares and edges, read once
        self.rows = {n: [] for n in ROWS}  # accepted components per row
        self.lessons = Lessons()
        self.history, self.turn_no, self.render_no = [], 0, 0

    # ---- drawing and judging (program)
    def render(self, comps, tag):
        self.render_no += 1
        base = os.path.join(self.renders, f"{self.render_no:02d}-{tag}")
        with open(base + ".svg", "w", encoding="utf-8") as handle:
            handle.write(C.svg(comps, size=SIZE))
        if not slide_team.screenshot(self.a.chrome, base + ".svg", base + ".png", os.path.join(self.renders, ".chrome-profile"), size=SIZE):
            raise RuntimeError("Chrome did not write the screenshot")
        return base + ".png"

    def evaluate(self, n, comps, tag):
        others = [c for k, row in self.rows.items() if k != n for c in row]
        png = self.render(self.base + others + comps, tag)
        y0, y1 = ROWS[n]
        result = scoring.compare(imgcmp.read_png(png), self.original, phone_icons, box=(0, y0, SIZE[0], y1),
                                 regions=[(f"row {n} {zone} (x {x0}-{x1})", x0, y0, x1, y1) for zone, x0, x1 in ZONES],
                                 prepared=self.prepared, score_mode=self.a.score)
        missing = C.missing(comps, ROW_LABELS[n])
        return {"match": round(result["match"], 4), "psnr": round(result["psnr"], 2), "missing": missing, "png": png,
                "strict": round(result["strict"], 4), "score": round(score_of(result["score"], missing), 4),
                "notes": scoring.feedback(result, limit=4),
                "comps": comps}

    def strict_note(self, old, ev):
        """In strict mode the decision follows the strict score (borders and text colour too), not match: say so."""
        return f" (strict {old:.3f} -> {ev['strict']:.3f})" if self.a.score == "strict" else ""

    def program_notes(self, ev):
        notes = list(ev["notes"])
        if ev["missing"]:
            notes.insert(0, "labels still missing in this row: " + ", ".join(ev["missing"]))
        return "\n".join(f"- {x}" for x in notes) or "- nothing big"

    # ---- talking to the agents
    def run_turn(self, name, prompt, model, files=(), timeout=900):  # OpenCode members (Codex members replace this)
        return slide_team.Team.run_turn(self, name, prompt, model, files=files, timeout=timeout, fresh=self.a.sessions == "fresh")

    def next_turn(self):
        self.turn_no += 1
        return self.turn_no

    def ask(self, drawer, prompt, n):
        """One drawer turn plus up to --fixes fix-ups; returns a valid component list or None."""
        reply, _ = self.run_turn(drawer, prompt, self.a.draw_model, timeout=self.a.turn_timeout)
        for attempt in range(self.a.fixes + 1):
            comps = C.parse_reply(reply or "")
            if comps is None:
                errors, ok = ["the reply had no ```json block with a JSON list"], None
                self.chat(drawer, "supervisor", (reply or "(no reply)")[-300:])
            else:
                errors, warnings, ok = C.check(comps, prefix=f"r{n}.", size=SIZE)
                self.chat(drawer, "supervisor", summary_of(comps))
                if not errors:
                    self.chat("check", drawer, f"OK: {len(ok)} components" + (f"; {len(warnings)} warnings: " + "; ".join(warnings[:2]) if warnings else ""), "check")
                    return ok
            for e in errors:
                if self.lessons.add(lesson_of(e, comps)):
                    self.chat("lessons", "team", "new lesson: " + lesson_of(e, comps), "check")
            self.chat("check", drawer, f"{len(errors)} errors: " + "; ".join(errors[:3]), "check")
            if attempt == self.a.fixes:
                return None
            self.chat("supervisor", drawer, f"fix: {'; '.join(errors[:2])[:160]}", "control")
            previous = json.dumps(comps, indent=0) if comps is not None else (reply or "")[-6000:]
            reply, _ = self.run_turn(drawer, FIX.format(n=n, errors="\n".join(f"- {e}" for e in errors[:8]), guide=C.guide(SIZE),
                                                        checklist=self.checklist[n], previous=previous), self.a.draw_model,
                                     timeout=self.a.turn_timeout)
        return None

    def review(self, n, ev, to):
        y0, y1 = ROWS[n]
        ours = os.path.join(self.renders, f"row{n}-ours-{self.render_no:02d}.png")
        theirs = os.path.join(self.renders, f"row{n}-original.png")
        imgcmp.write_png(ours, imgcmp.crop(imgcmp.read_png(ev["png"]), 0, y0, SIZE[0], y1))
        if not os.path.exists(theirs):
            imgcmp.write_png(theirs, imgcmp.crop(self.original, 0, y0, SIZE[0], y1))
        self.chat("render", "art", f"row {n}: original and ours ready", "check")
        try:
            reply, _ = self.run_turn("art", ART.format(n=n), self.a.art_model, files=[theirs, ours], timeout=self.a.art_timeout)
        except Exception as exc:  # a broken reviewer must show up in the conversation, not stop the team
            reply = f"(art director failed: {type(exc).__name__}: {exc})"
        notes, same = art_notes(reply)
        self.chat("art", to, "\n".join(notes) if notes else ("SAME" if same else "(no DIFF lines) " + reply[-160:]))
        return notes

    # ---- the run
    def run(self):
        a = self.a
        drawers = ["drawA", "drawB"]
        self.chat("supervisor", "team", f"task: rebuild the infographic row by row from the checklist; {a.revisions} revisions per row; "
                                       "the program keeps a version only when the picture match improves", "control")
        k = 0
        for n in ROWS:
            drawer = drawers[k % 2]
            k += 1
            self.chat("manager", drawer, f"round {self.next_turn()}: row {n} draft from the checklist")
            comps = self.ask(drawer, DRAFT.format(name=drawer, n=n, lessons=self.lessons.text(), guide=C.guide(SIZE),
                                                  checklist=self.checklist[n]), n)
            best = None
            if comps is not None:
                best = self.evaluate(n, comps, f"row{n}-draft")
                self.rows[n] = comps
                self.manifest.write(json.dumps({"t": round(time.time(), 2), "round": self.turn_no, "path": best["png"]}) + "\n")
                self.chat("picture", "team", f"row {n}: match {best['match']:.3f}, {len(best['missing'])} labels missing: first version kept", "check")
            self.history.append({"turn": self.turn_no, "row": n, "drawer": drawer, "kind": "draft", "valid": comps is not None,
                                 "match": best and best["match"], "strict": best and best["strict"], "score": best and best["score"],
                                 "missing": best and best["missing"], "accepted": comps is not None})
            for _ in range(a.revisions):
                drawer = drawers[k % 2]
                k += 1
                prev = drawers[k % 2]
                notes = self.review(n, best, drawer) if best else []
                program = self.program_notes(best) if best else "- there is no version yet: build the row from the checklist"
                self.chat("manager", drawer, f"round {self.next_turn()}: row {n} revise {prev}'s version: {len(notes)} art notes, "
                                             f"{program.count(chr(10)) + 1} program notes")
                prompt = REVISE.format(name=drawer, n=n, prev=prev, lessons=self.lessons.text(), program=program,
                                       art="\n".join(f"- {x}" for x in notes) or "- (no notes)", guide=C.guide(SIZE),
                                       checklist=self.checklist[n], current=json.dumps(best["comps"] if best else [], indent=0))
                comps = self.ask(drawer, prompt, n)
                record = {"turn": self.turn_no, "row": n, "drawer": drawer, "kind": "revise", "valid": comps is not None, "accepted": False}
                if comps is not None:
                    ev = self.evaluate(n, comps, f"row{n}-rev")
                    record.update(match=ev["match"], strict=ev["strict"], score=ev["score"], missing=ev["missing"])
                    if better(ev, best):
                        old, old_strict = (best["match"], best["strict"]) if best else (0, 0)
                        best, self.rows[n], record["accepted"] = ev, comps, True
                        self.manifest.write(json.dumps({"t": round(time.time(), 2), "round": self.turn_no, "path": ev["png"]}) + "\n")
                        self.chat("picture", "team", f"row {n}: match {old:.3f} -> {ev['match']:.3f}, {len(ev['missing'])} labels missing: accepted"
                                                     + self.strict_note(old_strict, ev), "check")
                    else:
                        self.chat("picture", "team", f"row {n}: match {best['match']:.3f} -> {ev['match']:.3f}, {len(ev['missing'])} labels "
                                                     f"missing: rejected, kept the better version" + self.strict_note(best["strict"], ev), "check")
                        if self.lessons.add("a revision that lowered the picture match was rejected: change only what the notes ask"):
                            self.chat("lessons", "team", "new lesson: a revision that lowered the picture match was rejected", "check")
                self.history.append(record)
        return self.finish()

    def finish(self):
        comps = self.base + [c for n in ROWS for c in self.rows[n]]
        final = os.path.join(self.work, "slide")
        with open(final + ".svg", "w", encoding="utf-8") as handle:
            handle.write(C.svg(comps, size=SIZE))
        slide_team.screenshot(self.a.chrome, final + ".svg", final + ".png", os.path.join(self.renders, ".chrome-profile"), size=SIZE)
        result = scoring.compare(imgcmp.read_png(final + ".png"), self.original, phone_icons, prepared=self.prepared,
                                 score_mode=self.a.score)
        missing = C.missing(comps, slide_team.REQUIRED)
        self.make_deck(final + ".png")
        json.dump({"turns": self.history, "final": {"match": round(result["match"], 4), "strict": round(result["strict"], 4),
                                                    "psnr": round(result["psnr"], 2), "missing": missing, "score_mode": self.a.score},
                   "lessons": self.lessons.items}, open(os.path.join(self.work, "summary.json"), "w"), indent=1)
        self.chat("supervisor", "team", f"finished: whole-slide match {result['match']:.3f}, PSNR {result['psnr']:.1f} dB, "
                                       f"{len(missing)} required labels missing", "control")
        self.chat("supervisor", "team", "finished", "end")
        if getattr(self, "codex", None):
            self.codex.close()
        return 0

    def make_deck(self, png):
        """One 16:9 slide with the drawing centred; exported with open-slide-py (the drawing is a picture in the PPTX)."""
        h = 1080
        w = round(h * SIZE[0] / SIZE[1])
        shutil.copy(png, os.path.join(self.work, "drawing.png"))
        deck = {"schema_version": 1, "id": "llm-serving", "title": "LLM Serving: When to Split Prefill and Decode", "width": 1920,
                "height": 1080, "lang": "en-US", "slides": [{"id": "s1", "title": "LLM Serving", "background": C.BG, "elements": [
                    {"id": "drawing", "type": "image", "path": "drawing.png", "alt": "LLM serving: shared worker, chunked prefill, "
                     "separate prefill and decode pools (based on TheAiEdge.io)", "x": (1920 - w) // 2, "y": 0, "width": w, "height": h}]}]}
        with open(os.path.join(self.work, "deck.json"), "w", encoding="utf-8") as handle:
            json.dump(deck, handle, indent=1)
        env = dict(os.environ, PYTHONPATH=self.a.open_slide, PYTHONDONTWRITEBYTECODE="1")
        p = subprocess.run([sys.executable, "-m", "open_slide_py", "export", "deck.json", "slide.pptx"], cwd=self.work, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=180)
        self.chat("render", "team", "slide.pptx written" if p.returncode == 0 else "PPTX export failed: " + p.stdout.strip()[-200:], "check")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", help="herdr-py daemon socket (backend opencode)")
    ap.add_argument("--backend", choices=["opencode", "codex"], default="opencode")
    ap.add_argument("--codex", help="codex executable (default: $CODEX_BIN, the one inside ChatGPT.app, or codex on PATH)")
    ap.add_argument("--codex-model", help="model for every Codex member (default: the one in the user's Codex config)")
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--reference", required=True, help="the original picture, 1206 x 1441 PNG")
    ap.add_argument("--open-slide", required=True, help="folder that contains the open_slide_py package")
    ap.add_argument("--revisions", type=int, default=2, help="revisions per row after the draft")
    ap.add_argument("--sessions", choices=["fresh", "keep"], default="fresh",
                    help="fresh: every turn in a new session, the prompt carries everything (a long session gets compacted "
                         "at an unknown moment); keep: one conversation per member")
    ap.add_argument("--fixes", type=int, default=2, help="times a drawer may fix a list the program cannot draw")
    ap.add_argument("--score", choices=scoring.SCORE_MODES, default="strict",
                    help="strict: keep a revision only when the score that also sees borders and text colour rises (scoring.py); "
                         "match: the square-colour match alone, as before")
    ap.add_argument("--draw-model", default="ollama/qwen3-8b-32k:latest")
    ap.add_argument("--art-model", default="ollama/qwen3-vl-32k:latest")
    ap.add_argument("--turn-timeout", type=int, default=600)
    ap.add_argument("--art-timeout", type=int, default=300)
    ap.add_argument("--chrome", default=slide_team.CHROME)
    ap.add_argument("--chat")
    return LayoutTeam(ap.parse_args()).run()


if __name__ == "__main__":
    sys.exit(main())
