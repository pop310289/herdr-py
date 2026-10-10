"""What an engine run's records say was wasted or went wrong, for the person reading its replay: findings, each with the
numbers that show it and what to change. Reads the folder herdr_py.engine writes (summary.json, engine.jsonl, run.jsonl,
kb/, members/); a run too old to hold a record just has fewer findings. Nothing is guessed that the records do not hold.

    python3 -m herdr_py.rundiag RUN_DIR [--json]

The rules (codes):
- same_failure: two or more answers turned down for the same reason (the first words of the judge's detail).
- no_gain: a verified result that neither beat the best score when it was judged nor was built on by a later entry.
- unused_skill: a skill nobody in the run built on or opened (it may still help a next run that carries it).
- page_overlap: pages opened with a fetch tool by more than one member.
- after_best: member turns that started after the run's best score was first reached (wrap-up turns told apart).
- budget_left: the run stopped with member turns unused because the planner's wakes ran out.
- waiting: members that spent a quarter of the run or more free with nothing to take.
- sent_back: planner replies the program sent back, and why.
- repairs: turns that repaired an answer in the turn, and how many repairs helped.
- team_tools: the MCP tools members made for each other (verified ARTIFACT: mcp answers), how many answered the
  engine's probe, and how often members called them.
- lost: member turns that left nothing (no answer recorded: they ran out of time, broke, or answered without the
  format), with the tokens they used.
- cancelled: member turns the engine cancelled when the target was reached.
- skill_overlap: pairs of skills that quote the same sentences (three or more, of five words or more), unless one is a
  revision of the other (it builds on it and keeps its name): the same rules written down twice.
- references: the files brought from other tasks (reference/), and how many of them members opened.
- broken: what broke in the setup (a backend, the judge)."""
import argparse
import collections
import itertools
import json
import os
import re
import sys

from . import engineview
from .teamkb import TAG, TeamKB

WAIT_SHARE = 0.25  # a member free this share of the run (or more) waited too long
QUOTE = re.compile(r'"([^"\n]{20,})"|\u201c([^\u201d\n]{20,})\u201d')  # a quoted sentence ("..." or curly quotes)
QUOTE_WORDS = 5  # a quote counts when it has this many words or more (a field's name alone does not)
SHARED_QUOTES = 3  # two skills quoting this many of the same sentences restate each other


def jsonl(path):
    out = []
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out


def load_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def artifact_kind(run, entry):
    if not entry.get("artifact"):
        return entry.get("kind")
    try:
        with open(os.path.join(run, "kb", entry["artifact"]), encoding="utf-8", errors="replace") as handle:
            first = handle.readline()
    except OSError:
        return entry.get("kind")
    m = TAG.match(first)
    return m.group(2).lower() if m else entry.get("kind")


def quotes(text):
    """The sentences a text quotes, each lower case with its white space and markup (* and `) taken out."""
    out = set()
    for m in QUOTE.finditer(text):
        q = " ".join(re.sub(r"[*`]", "", m.group(1) or m.group(2)).lower().split()).rstrip(".")
        if len(q.split()) >= QUOTE_WORDS:
            out.add(q)
    return out


def skill_name(text):
    m = re.search(r"^name:\s*(\S.*?)\s*$", text, re.M)
    return m.group(1).lower() if m else None


def ancestors(eid, by_id):
    seen, todo = set(), list((by_id.get(eid) or {}).get("parents") or [])
    while todo:
        p = todo.pop()
        if p not in seen:
            seen.add(p)
            todo.extend((by_id.get(p) or {}).get("parents") or [])
    return seen


def tok(n):
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def findings(run):
    """The findings for a run folder: a list of {"code", "title", "numbers", "suggestion", "args"} (args: what a view
    needs to say the title in its own words, with the numbers)."""
    summary = load_json(os.path.join(run, "summary.json")) or {}
    turns = [r for r in jsonl(os.path.join(run, "run.jsonl")) if r.get("member")]
    wakes = [r for r in jsonl(os.path.join(run, "engine.jsonl")) if r.get("kind") == "wake"]
    entries = TeamKB(os.path.join(run, "kb")).entries() if os.path.isdir(os.path.join(run, "kb")) else []
    by_id = {e["id"]: e for e in entries}
    kinds = {e["id"]: artifact_kind(run, e) for e in entries}
    built_on = {p for e in entries for p in e.get("parents") or []}
    out = []

    def add(code, title, numbers, suggestion, **args):
        out.append({"code": code, "title": title, "numbers": numbers, "suggestion": suggestion, "args": dict(numbers, **args)})

    # answers turned down for the same reason: the task (or the planner) did not say something plainly
    attempts, counted = [], set()
    for r in turns:
        tries = [r] + list(r.get("repairs") or [])
        named = [a.get("entry") for a in tries] + [a.get("of") for a in r.get("repairs") or []]  # "of": the version repaired
        for eid in named:
            if eid and eid not in counted:  # a repaired turn names its best version again; count each version once
                counted.add(eid)
                attempts.append(next((a for a in tries if a.get("entry") == eid), {"entry": eid, "tokens": 0}))
    reasons = collections.defaultdict(list)
    for a in attempts:
        e = by_id.get(a.get("entry"))
        if e and e["status"] == "invalid":
            head = " ".join(str(e.get("detail") or "").split())[:70]
            reasons[head].append(a)
    for head, group in reasons.items():
        if len(group) >= 2:
            add("same_failure", f"{len(group)} answers were turned down for the same reason: {head}",
                {"answers": len(group), "tokens": sum(a.get("tokens") or 0 for a in group)},
                "say it plainly in the task (if the judge checks it, the task should say it) or let members repair "
                "in the turn (loop.repairs)", reason=head)

    # results that added nothing: below the best when judged, or as good as the best without building on it (the same work
    # done twice), and no later result built on them (a skill citing it does not make it count)
    built_by_results = {p for e in entries if kinds.get(e["id"]) != "skill" for p in e.get("parents") or []}
    best, holder, idle_results, seen = None, None, [], set()
    for r in sorted(turns, key=lambda r: r.get("end") or 0):
        for a in [r] + list(r.get("repairs") or []):  # a repaired turn names its best version again: count each once
            e = by_id.get(a.get("entry"))
            if not e or e["id"] in seen or e["status"] != "valid" or kinds.get(e["id"]) == "skill":
                continue
            seen.add(e["id"])
            score = e["score"] if e["score"] is not None else float("-inf")
            if best is None or score > best or (score == best and holder in (e.get("parents") or [])):
                best, holder = score, e["id"]  # a new best, or a revision of the best that keeps its score
            elif e["id"] not in built_by_results:
                idle_results.append((r, a, e))
    if idle_results:
        spent = sum(a.get("tokens") or 0 for _, a, _ in idle_results)
        which = ", ".join("%s %s" % (e["member"], e["id"]) for _, _, e in idle_results[:4])
        add("no_gain", f"{len(idle_results)} results added nothing: they did not beat the best and nothing was built on them "
            f"({which}; {tok(spent)} tokens)",
            {"results": len(idle_results), "tokens": spent},
            "the same work was given twice, or came after a better result: give the second member another part, or have "
            "it test or review the first version", which=which, tokens_text=tok(spent))

    # skills nobody used in the run
    reads = {(r.get("agent"), r.get("entry")) for r in jsonl(os.path.join(run, "kb", "events.jsonl")) if r.get("type") == "read"}
    read_ids = {eid for _, eid in reads}
    unused = [e for e in entries if e["status"] == "valid" and kinds.get(e["id"]) == "skill"
              and e["id"] not in built_on and e["id"] not in read_ids]
    if unused:
        add("unused_skill", f"{len(unused)} skills were not used in this run ({', '.join(e['id'] for e in unused[:4])})",
            {"skills": len(unused)},
            "a skill counts when someone follows it: give it as a parent to the todos it is for, or carry it (carry: skill) "
            "to a next run", which=", ".join(e["id"] for e in unused[:4]))

    # the same pages opened by more than one member
    tools = engineview.load(run)[5] if os.path.isfile(os.path.join(run, "engine.jsonl")) else []
    opened = collections.defaultdict(set)
    fetches = 0
    for c in tools:
        if engineview.tool_class(c.get("tool")) == "fetch" and c.get("what"):
            opened[c["what"].strip().rstrip("/").lower()].add(c.get("agent"))
            fetches += 1
    shared = {u: who for u, who in opened.items() if len(who) > 1}
    if shared:
        add("page_overlap", f"{len(shared)} pages were opened by more than one member ({fetches} fetches of {len(opened)} pages)",
            {"pages": len(shared), "fetches": fetches, "unique": len(opened)},
            "have one member write down what the pages say (a skill) and give it to the others as a parent, after its todo")

    # turns after the best was first reached
    valid = [(r.get("end") or 0, r) for r in turns if r.get("status") == "valid" and r.get("score") is not None]
    if valid:
        top = max(r["score"] for _, r in valid)
        first_top = min(t for t, r in valid if r["score"] == top)
        later = [r for r in turns if (r.get("start") or 0) >= first_top]
        wrap = [r for r in later if r.get("wrap_up")]
        rest = [r for r in later if not r.get("wrap_up")]
        if rest:
            add("after_best", f"{len(rest)} member turns started after the best score ({top:g}) was first reached "
                f"({tok(sum(r.get('tokens') or 0 for r in rest))} tokens)",
                {"turns": len(rest), "tokens": sum(r.get("tokens") or 0 for r in rest), "best": top},
                f"if {top:g} is the most this task can score, set loop.target to it (and loop.wrap_up for a few turns that "
                "write down what worked)", tokens_text=tok(sum(r.get("tokens") or 0 for r in rest)), best_text=f"{top:g}")
        if wrap:
            add("after_best", f"{len(wrap)} wrap-up turns after the target, to write down what worked "
                f"({tok(sum(r.get('tokens') or 0 for r in wrap))} tokens)",
                {"turns": len(wrap), "tokens": sum(r.get("tokens") or 0 for r in wrap), "wrap_up": True},
                "they pay off when a next run carries what they wrote", tokens_text=tok(sum(r.get("tokens") or 0 for r in wrap)))

    # the budget left unused
    stopped = str(summary.get("stopped") or "")
    if "wakes are used up" in stopped and summary.get("turns", 0) < summary.get("turn_budget", 0):
        left = summary["turn_budget"] - summary["turns"]
        add("budget_left", f"the run stopped with {left} member turns unused: the planner's wakes ran out",
            {"turns_left": left, "wakes": summary.get("planner_wakes")},
            "give budget.planner_wakes about as many as budget.turns")

    # members waiting for work
    seconds = summary.get("seconds") or 0
    waited = {m: s for m, s in (summary.get("idle_seconds") or {}).items() if seconds and s / seconds >= WAIT_SHARE}
    if waited:
        add("waiting", "members waited for work: " + ", ".join(f"{m} {100 * s / seconds:.0f}%" for m, s in sorted(waited.items())),
            {m: round(s / seconds, 2) for m, s in waited.items()},
            "the planner should keep an open todo for each free member (more todos per wake, or todos that can start now)",
            who=", ".join(f"{m} {100 * s / seconds:.0f}%" for m, s in sorted(waited.items())))

    # planner replies sent back
    back = [p for w in wakes for p in w.get("problems") or []]
    if back:
        why = collections.Counter(" ".join(p.split(":", 1)[-1].split())[:60] for p in back)
        add("sent_back", f"the planner's replies were sent back {len(back)} times: "
            + "; ".join(f"{n}× {w}" for w, n in why.most_common(3)),
            {"sent_back": len(back)},
            "each reason names the rule the planner broke; if one repeats across runs, say it in the planner's notes",
            why="; ".join(f"{n}× {w}" for w, n in why.most_common(3)))

    # repairs in the turn
    repaired = [r for r in turns if r.get("repairs")]
    if repaired:
        n = sum(len(r["repairs"]) for r in repaired)
        helped = sum(1 for r in repaired for a in r["repairs"]
                     if a.get("status") == "valid" and (r.get("score") or 0) >= (a.get("score") or 0) and a.get("entry") == r.get("entry"))
        add("repairs", f"{len(repaired)} turns repaired their answer: {n} repairs, {helped} gave the turn's best version",
            {"turns": len(repaired), "repairs": n, "helped": helped, "tokens": sum(a.get("tokens") or 0 for r in repaired for a in r["repairs"])},
            "a repair keeps what the member already read; when repairs rarely help, the judge's detail may not say what to fix")

    # the members' own MCP tools: made, probed, called
    made = [r.get("tool") for r in turns for r in [r] + list(r.get("repairs") or []) if r.get("tool")]
    calls = collections.Counter(c.get("agent") for c in tools if str(c.get("tool") or "").startswith("mcp__team_"))
    if made or calls:
        answered = sum(1 for t in made if t.get("ok"))
        add("team_tools", f"the team made {len(made)} MCP tools ({answered} answered the probe); members called them "
            f"{sum(calls.values())} times" + (" (" + ", ".join(f"{m} {n}" for m, n in sorted(calls.items())) + ")" if calls else ""),
            {"made": len(made), "answered": answered, "calls": sum(calls.values())},
            "a tool pays off when teammates call it: name it in the todos it is for",
            who=", ".join(f"{m} {n}" for m, n in sorted(calls.items())) or "-")

    # turns that left nothing, and turns cancelled when the target was reached
    lost = [r for r in turns if not r.get("entry") and not any(a.get("entry") for a in r.get("repairs") or [])]
    for code, group in (("lost", [r for r in lost if not r.get("cancelled")]), ("cancelled", [r for r in lost if r.get("cancelled")])):
        if group:
            spent = sum(r.get("tokens") or 0 for r in group)
            at_least = "at least " if any(r.get("tokens_partial") for r in group) else ""  # a killed member's count stops short
            total = f"{at_least}{tok(spent)} tokens" if spent or not at_least else "tokens unknown"
            short = ("≥" if at_least else "") + tok(spent) if spent or not at_least else "?"  # for a view in any language
            def used(r):  # a member killed before its first reply said nothing: its tokens are not known
                if r.get("tokens_partial"):
                    return f"≥{tok(r['tokens'])}" if r.get("tokens") else "tokens unknown"
                return tok(r.get("tokens") or 0)
            which = ", ".join(f"{r['member']} {r.get('state') or '?'} {used(r)}" for r in group[:4])
            if code == "lost":
                add(code, f"{len(group)} member turns left nothing ({which}; {total})",
                    {"turns": len(group), "tokens": spent},
                    "a turn that runs out of time loses all it did: give one part of the work per todo, or a longer "
                    "budget.turn_timeout", which=which, tokens_text=short)
            else:
                add(code, f"{len(group)} member turns were cancelled when the target was reached ({which}; {total})",
                    {"turns": len(group), "tokens": spent},
                    "what they were making was no longer needed; the tokens before the cancel were spent all the same",
                    which=which, tokens_text=short)

    # skills that restate each other
    said, names = {}, {}
    for e in entries:
        if e["status"] == "valid" and kinds.get(e["id"]) == "skill" and e.get("artifact"):
            try:
                with open(os.path.join(run, "kb", e["artifact"]), encoding="utf-8", errors="replace") as handle:
                    text = handle.read()
            except OSError:
                continue
            said[e["id"]], names[e["id"]] = quotes(text), skill_name(text)
    pairs = []
    for a, b in itertools.combinations([e["id"] for e in entries if e["id"] in said], 2):
        both = said[a] & said[b]
        revision = names[a] == names[b] and (a in ancestors(b, by_id) or b in ancestors(a, by_id))
        if len(both) >= SHARED_QUOTES and not revision:
            pairs.append((a, b, len(both)))
    if pairs:
        which = ", ".join(f"{a} and {b} ({n})" for a, b, n in pairs[:4])
        add("skill_overlap", f"{len(pairs)} pairs of skills quote the same sentences: {which}",
            {"pairs": len(pairs), "shared": sum(n for _, _, n in pairs)},
            "one skill per subject: improve the earlier skill (with it as the todo's parent) or point to it, instead of "
            "restating it", which=which)

    # material brought from other tasks, and whether members opened it
    refdir = os.path.join(run, "board", "reference")
    brought = []
    for root, _, names in os.walk(refdir):
        brought += [os.path.relpath(os.path.join(root, n), refdir).replace(os.sep, "/") for n in names if n != "INDEX.md"]
    if brought:
        reads = collections.defaultdict(set)
        for c in tools:
            what = str(c.get("what") or "").replace(os.sep, "/")
            for f in brought:
                if what == "reference/" + f or what.endswith("/reference/" + f):
                    reads[f].add(c.get("agent"))
        who = collections.Counter(a for f in reads for a in reads[f])
        add("references", f"{len(brought)} files were brought from other tasks; members opened {len(reads)} of them"
            + (" (" + ", ".join(f"{m} {n}" for m, n in sorted(who.items())) + ")" if who else ""),
            {"files": len(brought), "opened": len(reads)},
            "what is brought pays off when someone reads it: name the files in the todos they are for",
            who=", ".join(f"{m} {n}" for m, n in sorted(who.items())) or "-")

    for item in summary.get("broken") or []:
        add("broken", f"the setup broke: {item}", {}, "fix the setup before reading the results", what=str(item))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("run")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    got = findings(a.run)
    if a.json:
        print(json.dumps(got, ensure_ascii=False, indent=1))
    else:
        for f in got:
            print(f"- [{f['code']}] {f['title']}\n  → {f['suggestion']}")
        if not got:
            print("nothing to report")
    return 0


if __name__ == "__main__":
    sys.exit(main())
