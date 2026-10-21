"""Skill ontology: what each skill eats, makes and needs, so an agent can chain skills for a task nobody wrote a skill
for, and every chain is checked before it runs. The ontology describes, checks and records; the agent composes; a
chain succeeds only when its last step, a check, exits with a success code.

    python3 -m herdr_py.ontology build  --db onto.db                          # skills + catalog + this machine -> ontology
    python3 -m herdr_py.ontology find   --db onto.db --task notes-site-builder  # shortest chains for a task
    python3 -m herdr_py.ontology check  --db onto.db --task T --steps OP [OP ...]
    python3 -m herdr_py.ontology run    --db onto.db --task T --steps OP [OP ...] --start notes-md=notes.md --work DIR
    python3 -m herdr_py.ontology verify --db onto.db                          # replay the action log against the state
    python3 -m herdr_py.ontology pin    [--catalog C] [--lib L]               # record the skills' current md5 in the catalog

Modeled on the Palantir Foundry Ontology (object types, link types, interfaces, actions with submission criteria, an
action log). Guide and example catalog: examples/ontology/README.md.

Semantic layer (OBJECT_TYPES, LINK_TYPES): Skill, Operation (one entry point of a skill), ArtifactType (what operations
pass on: a file, a folder, a report, a URL), Interface (a kind of artifact several types share), Condition (what a step
needs: Chrome, a free loopback port), Environment (where steps run and which conditions it has), Task (start, goal,
environment), Chain (ordered steps an agent proposed), ChainRun (one execution).

Kinetic layer (ACTIONS, act): actions are the only way to write. An action takes parameters, checks its submission
criteria, turns its rule into edits, and commits them in one transaction together with an action-log entry that keeps
every edit's value before and after. A rejected submission is kept with its reasons. Agents (actors "agent:...") may
register tasks and propose chains; they cannot register operations or record run results.

Functions (read-only): check_chain (the criteria of proposeChain), find_chains (breadth-first, shortest chains only),
compile_script (argv lists from the declared commands; parameters cannot become options), run_script / run_chain,
verify_log (replaying the log must give the current state).

A catalog (examples/ontology/catalog.json) declares the conditions, interfaces, artifact types, environments,
operations and tasks. Every operation names the md5 of the skill it was checked against ("declared_against"); when a
skill changes, its operations become stale and every chain using them needs a recheck. A skill's md5 is read from the
library's MANIFEST.json when there is one (skill-align format), else computed from its files.

Standard library only; Python 3.6 and later."""
import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from collections import deque

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATALOG = os.path.join(HERE, "examples", "ontology", "catalog.json")
SKILL_LIB = os.path.join(HERE, "examples", "ontology", "skills")

# ---------------------------------------------------------------- semantic layer
# object type -> {property: required}; a property not listed cannot be written
OBJECT_TYPES = {
    "Skill": {"md5": True, "dir": True},
    "Condition": {"description": True},
    "Interface": {"description": True},
    "ArtifactType": {"kind": True, "description": True, "ext": False},
    "Environment": {"source": True},
    "Operation": {"skill": True, "entry": False, "argv": True, "cwd": False, "check": True, "success": True,
                  "manual": False, "service": False, "value": False, "env_after": False, "params": True,
                  "doc": True, "skill_md5": False, "status": True},
    "Task": {"start": True, "goal": True, "env": True, "description": True},
    "Chain": {"task": True, "steps": True, "status": True},
    "ChainRun": {"chain": True, "status": True, "results": True, "t": True},
}
# link type -> (from type, to type, name on the from side, name on the to side, foreign-key property)
# many-to-many links live in the links table; one-to-many links are a foreign-key property of the "from" object
LINK_TYPES = {
    "skillOperation": ("Operation", "Skill", "skill", "operations", "skill"),
    "consumes": ("Operation", "ArtifactType", "inputs", "consumers", None),
    "consumesAny": ("Operation", "Interface", "inputs", "consumers", None),
    "produces": ("Operation", "ArtifactType", "outputs", "producers", None),
    "requires": ("Operation", "Condition", "requires", "requiredBy", None),
    "provides": ("Environment", "Condition", "provides", "providedBy", None),
    "implements": ("ArtifactType", "Interface", "interfaces", "implementers", None),
    "chainTask": ("Chain", "Task", "task", "chains", "task"),
    "runChain": ("ChainRun", "Chain", "chain", "runs", "chain"),
}
KINDS = ("file", "dir", "stdout", "value")      # value: a URL or an index location; it does not move to another machine
RUN_STATUSES = ("succeeded", "failed", "stopped-at-manual")
PLACEHOLDER = re.compile(r"\{(in|out|param):([^{}]+)\}|\{(skill_dir|lib_root|port|home|python)\}")

SCHEMA = """
CREATE TABLE IF NOT EXISTS objects(type TEXT, pk TEXT, props TEXT NOT NULL, PRIMARY KEY(type, pk));
CREATE TABLE IF NOT EXISTS links(link TEXT, src TEXT, dst TEXT, PRIMARY KEY(link, src, dst));
CREATE TABLE IF NOT EXISTS action_log(id INTEGER PRIMARY KEY, action TEXT, version INTEGER, actor TEXT,
    params TEXT, started TEXT, committed TEXT, edits TEXT);
CREATE TABLE IF NOT EXISTS log_touches(log_id INTEGER, type TEXT, pk TEXT);
CREATE TABLE IF NOT EXISTS rejected(id INTEGER PRIMARY KEY, action TEXT, version INTEGER, actor TEXT,
    params TEXT, t TEXT, reasons TEXT);
"""


def _now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _dump(x):
    return json.dumps(x, ensure_ascii=False, sort_keys=True)


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def open_db(path):
    conn = sqlite3.connect(path, isolation_level=None)      # transactions are explicit: BEGIN IMMEDIATE ... COMMIT
    conn.executescript(SCHEMA)
    return conn


def skill_md5(skill_dir):
    """md5 of a skill folder: from the library's MANIFEST.json when it lists the skill (skill-align format), else the
    md5 of one "relative/path md5" line per file, sorted (caches and compiled files left out)."""
    lib, name = os.path.dirname(os.path.abspath(skill_dir)), os.path.basename(os.path.abspath(skill_dir))
    manifest = os.path.join(lib, "MANIFEST.json")
    if os.path.exists(manifest):
        try:
            return read_json(manifest)["skills"][name]["md5"]
        except (KeyError, ValueError):
            return None
    lines = []
    for root, dirs, files in os.walk(skill_dir):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for f in sorted(files):
            if f.endswith(".pyc"):
                continue
            path = os.path.join(root, f)
            with open(path, "rb") as fh:
                lines.append("%s %s\n" % (os.path.relpath(path, skill_dir).replace(os.sep, "/"), hashlib.md5(fh.read()).hexdigest()))
    return hashlib.md5("".join(sorted(lines)).encode()).hexdigest() if lines else None


class View:
    """Read-only access inside the transaction. Criteria, rules and functions read only through it."""

    def __init__(self, conn):
        self.c = conn

    def get(self, typ, pk):
        r = self.c.execute("SELECT props FROM objects WHERE type=? AND pk=?", (typ, pk)).fetchone()
        return json.loads(r[0]) if r else None

    def all(self, typ):
        rows = self.c.execute("SELECT pk, props FROM objects WHERE type=? ORDER BY pk", (typ,))
        return {pk: json.loads(p) for pk, p in rows}

    def links(self, link, src=None, dst=None):
        q, a = "SELECT src, dst FROM links WHERE link=?", [link]
        if src is not None:
            q, a = q + " AND src=?", a + [src]
        if dst is not None:
            q, a = q + " AND dst=?", a + [dst]
        return [tuple(r) for r in self.c.execute(q + " ORDER BY src, dst", a)]


# ---------------------------------------------------------------- kinetic layer: the action pipeline
class EditError(Exception):
    """An edit made by a rule does not fit the types (an undefined property, a missing required one, a foreign key or
    a link end that does not exist)."""


def _apply(conn, view, edits):
    done = []
    for e in edits:
        if e["op"] == "put":
            spec = OBJECT_TYPES.get(e["type"])
            if spec is None:
                raise EditError("no object type %s" % e["type"])
            props = e["props"]
            unknown = sorted(set(props) - set(spec))
            missing = sorted(k for k, required in spec.items() if required and props.get(k) is None)
            if unknown or missing:
                raise EditError("%s %s: undefined properties %s, missing required %s" % (e["type"], e["pk"], unknown, missing))
            for src_t, dst_t, _, _, fk in LINK_TYPES.values():
                if fk and src_t == e["type"] and view.get(dst_t, props[fk]) is None:
                    raise EditError("%s %s: %s points to a %s that does not exist (%s)" % (e["type"], e["pk"], fk, dst_t, props[fk]))
            before = view.get(e["type"], e["pk"])
            if before == props:
                continue
            conn.execute("INSERT OR REPLACE INTO objects VALUES(?,?,?)", (e["type"], e["pk"], _dump(props)))
            done.append({"op": "put", "type": e["type"], "pk": e["pk"], "before": before, "after": props})
        elif e["op"] in ("link", "unlink"):
            spec = LINK_TYPES.get(e["link"])
            if spec is None or spec[4]:
                raise EditError("no many-to-many link type %s" % e["link"])
            if view.get(spec[0], e["src"]) is None or view.get(spec[1], e["dst"]) is None:
                raise EditError("%s: both ends must exist (%s %s -> %s %s)" % (e["link"], spec[0], e["src"], spec[1], e["dst"]))
            exists = bool(view.links(e["link"], e["src"], e["dst"]))
            if (e["op"] == "link") == exists:
                continue
            if e["op"] == "link":
                conn.execute("INSERT INTO links VALUES(?,?,?)", (e["link"], e["src"], e["dst"]))
            else:
                conn.execute("DELETE FROM links WHERE link=? AND src=? AND dst=?", (e["link"], e["src"], e["dst"]))
            done.append({"op": e["op"], "link": e["link"], "src": e["src"], "dst": e["dst"]})
        else:
            raise EditError("unknown edit %s" % e["op"])
    return done


def _touched(done):
    keys = set()
    for d in done:
        if d["op"] == "put":
            keys.add((d["type"], d["pk"]))
        else:
            spec = LINK_TYPES[d["link"]]
            keys |= {(spec[0], d["src"]), (spec[1], d["dst"])}
    return sorted(keys)


def act(conn, name, params, actor):
    """The only way to write. Returns {"ok", "log_id", "reasons"}; a rejected submission writes nothing but itself."""
    version, actors, criteria, rule = ACTIONS[name]
    started = _now()
    conn.execute("BEGIN IMMEDIATE")                # one write lock: the criteria see the state the edits apply to
    try:
        view = View(conn)
        reasons = [] if actor.startswith(actors) else ["%s can only be done by %s (this was %s)" % (name, ", ".join(actors), actor)]
        if not reasons:
            reasons = criteria(view, params, actor)
        done = []
        if not reasons:
            conn.execute("SAVEPOINT edits")
            try:
                done = _apply(conn, view, rule(view, params, actor))
            except EditError as exc:
                conn.execute("ROLLBACK TO edits")
                reasons = ["edits do not fit the types: %s" % exc]
            conn.execute("RELEASE edits")
        if reasons:
            conn.execute("INSERT INTO rejected(action, version, actor, params, t, reasons) VALUES(?,?,?,?,?,?)",
                         (name, version, actor, _dump(params), started, _dump(reasons)))
            conn.execute("COMMIT")
            return {"ok": False, "log_id": None, "reasons": reasons}
        if not done:                               # nothing changed: no log entry, so building again adds nothing
            conn.execute("COMMIT")
            return {"ok": True, "log_id": None, "reasons": []}
        cur = conn.execute("INSERT INTO action_log(action, version, actor, params, started, committed, edits) "
                           "VALUES(?,?,?,?,?,?,?)", (name, version, actor, _dump(params), started, _now(), _dump(done)))
        conn.executemany("INSERT INTO log_touches VALUES(?,?,?)", [(cur.lastrowid, t, pk) for t, pk in _touched(done)])
        conn.execute("COMMIT")
        return {"ok": True, "log_id": cur.lastrowid, "reasons": []}
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def verify_log(conn):
    """Replay the action log onto an empty state and compare it with the current one, row by row. Returns the
    differences; an empty list means no write went around the actions and every value traces back to one."""
    objs, links = {}, set()
    for (edits,) in conn.execute("SELECT edits FROM action_log ORDER BY id"):
        for d in json.loads(edits):
            if d["op"] == "put":
                objs[(d["type"], d["pk"])] = d["after"]
            elif d["op"] == "link":
                links.add((d["link"], d["src"], d["dst"]))
            else:
                links.discard((d["link"], d["src"], d["dst"]))
    now_objs = {(t, pk): json.loads(p) for t, pk, p in conn.execute("SELECT type, pk, props FROM objects")}
    now_links = {tuple(r) for r in conn.execute("SELECT link, src, dst FROM links")}
    diff = ["object %s %s differs from the replayed log" % k for k in sorted(set(objs) | set(now_objs)) if objs.get(k) != now_objs.get(k)]
    diff += ["link %s %s -> %s differs from the replayed log" % l for l in sorted(links ^ now_links)]
    return diff


# ---------------------------------------------------------------- functions (read-only)
class Catalog:
    """A read-only snapshot of the ontology for check_chain, find_chains and compile_script."""

    def __init__(self, view):
        self.types = view.all("ArtifactType")
        self.interfaces = view.all("Interface")
        self.skills = view.all("Skill")
        self.tasks = view.all("Task")
        self.impl = {}
        for t, i in view.links("implements"):
            self.impl.setdefault(t, set()).add(i)
        self.envs = {e: set() for e in view.all("Environment")}
        for e, c in view.links("provides"):
            self.envs[e].add(c)
        self.ops = {}
        for oid, p in view.all("Operation").items():
            self.ops[oid] = dict(p, id=oid,
                                 inputs=[d for _, d in view.links("consumes", oid) + view.links("consumesAny", oid)],
                                 outputs=[d for _, d in view.links("produces", oid)],
                                 requires=[d for _, d in view.links("requires", oid)])

    def satisfies(self, typ, need):
        return typ == need or need in self.impl.get(typ, ())


def _latest(cat, insts, need):
    for inst in reversed(insts):                   # several fit: the one made last (compile_script does the same)
        if cat.satisfies(inst["type"], need):
            return inst
    return None


def _step(cat, insts, env, op):
    """Apply one operation. An artifact is {"type", "origin"}; origin is every type it was made from on the way, so a
    goal can say which start it must come from ("a report about this page, not some page")."""
    problems, origin = [], set()
    for need in op["inputs"]:
        src = _latest(cat, insts, need)
        if src is None:
            problems.append("needs %s, which neither the start nor an earlier step makes" % need)
        else:
            origin |= src["origin"]
    for cond in op["requires"]:
        if cond not in cat.envs[env]:
            problems.append("needs %s, which %s does not have" % (cond, env))
    new = insts + [{"type": t, "origin": frozenset(origin | {t})} for t in op["outputs"]]
    if op.get("env_after") and op["env_after"] != env:
        # moving to another environment: values such as URLs stay behind on the old machine
        env = op["env_after"]
        new = [i for i in new if cat.types[i["type"]]["kind"] != "value"]
    return new, env, problems


def _goal_met(cat, insts, g):
    return any(i["type"] == g["type"] and (not g.get("of") or g["of"] in i["origin"]) for i in insts)


def _goal_text(g):
    return g["type"] + (" (from %s)" % g["of"] if g.get("of") else "")


def check_chain(cat, steps, start, goal, env, require_params=False):
    """The check before anything runs. Returns the violations; only an empty list may be proposed (it is the
    submission criterion of proposeChain)."""
    if env not in cat.envs:
        return ["no environment %s" % env]
    insts = [{"type": t, "origin": frozenset([t])} for t in start]
    out = []
    for i, step in enumerate(steps, 1):
        op = cat.ops.get(step["op"])
        if op is None:
            out.append("step %d: no operation %s" % (i, step["op"]))
            continue
        if op["status"] != "current":
            out.append("step %d: the skill of %s changed; its declaration needs a recheck" % (i, op["id"]))
        given = set(step.get("params") or {})
        out += ["step %d: %s has no parameter %s" % (i, op["id"], k) for k in sorted(given - set(op["params"]))]
        if require_params:
            out += ["step %d: %s is missing parameter %s" % (i, op["id"], k) for k in op["params"] if k not in given]
        insts, env, problems = _step(cat, insts, env, op)
        out += ["step %d: %s %s" % (i, op["id"], p) for p in problems]
    out += ["no goal %s is made" % _goal_text(g) for g in goal if not _goal_met(cat, insts, g)]
    last = cat.ops.get(steps[-1]["op"]) if steps else None
    if last is None or not last["check"]:
        out.append("the last step is not a check: success must come from a check's exit code")
    return out


def _key(insts):
    return frozenset((i["type"], i["origin"]) for i in insts)


def _orderings(cat, left, insts, env, prefix):
    if not left:
        yield prefix
        return
    for o in sorted(left):
        new, ne, problems = _step(cat, insts, env, cat.ops[o])
        if not problems:
            for order in _orderings(cat, left - {o}, new, ne, prefix + [o]):
                yield order


def _best_order(cat, steps, start, goal, env):
    """One order for a set of operations: hand off as late as possible (do in the starting environment whatever can be
    done there), then checks as early as possible (a failure stops the chain sooner)."""
    first = [{"type": t, "origin": frozenset([t])} for t in start]

    def score(order):
        switch = next((i for i, s in enumerate(order) if cat.ops[s].get("env_after")), len(order))
        return (-switch, sum(i for i, s in enumerate(order) if cat.ops[s]["check"]), order)

    valid = [o for o in _orderings(cat, frozenset(steps), first, env, [])
             if not check_chain(cat, [{"op": s} for s in o], start, goal, env)]
    return min(valid, key=score) if valid else list(steps)


def find_chains(cat, start, goal, env, max_steps=8, limit=3):
    """Breadth-first from the start, level by level, keeping only the shortest chains (a shortest chain has no step
    that can be dropped). A set of operations counts once, ordered by _best_order; chains with fewer steps that need a
    person come first. Each chain passes check_chain again. Stale operations (their skill changed) are not used.
    Returns at most `limit` chains as lists of operation ids."""
    first = [{"type": t, "origin": frozenset([t])} for t in start]
    level, seen = [(first, env, [])], {(_key(first), env)}
    ops = [cat.ops[k] for k in sorted(cat.ops) if cat.ops[k]["status"] == "current"]
    for _ in range(max_steps):
        found, best = {}, {}
        for insts, e, path in level:
            for op in ops:
                new, ne, problems = _step(cat, insts, e, op)
                if problems:
                    continue
                k = (_key(new), ne)
                if k == (_key(insts), e):          # nothing new made and no move: a useless step
                    continue
                steps = path + [op["id"]]
                if op["check"] and all(_goal_met(cat, new, g) for g in goal):
                    found.setdefault(frozenset(steps), steps)
                    continue
                if k in seen:
                    continue
                manual = sum(1 for s in steps if cat.ops[s].get("manual"))
                if k not in best or (manual, steps) < best[k][:2]:     # of the paths reaching one state in this level keep the one with fewest steps that need a person
                    best[k] = (manual, steps, new, ne)
        if found:
            plans = [_best_order(cat, s, start, goal, env) for s in found.values()]
            plans = [p for p in plans if not check_chain(cat, [{"op": s} for s in p], start, goal, env)]
            plans.sort(key=lambda p: (sum(1 for s in p if cat.ops[s].get("manual")), p))
            return plans[:limit]
        seen |= set(best)
        level = [(new, ne, steps) for _, steps, new, ne in best.values()]
    return []


class CompileError(Exception):
    pass


def _check_param(op, k, v):
    if k not in op["params"]:
        raise CompileError("%s has no parameter %s" % (op["id"], k))
    for x in (v if isinstance(v, list) else [v]):
        if not isinstance(x, str) or not x or x.startswith("-") or "\n" in x or "\x00" in x or len(x) > 200:
            raise CompileError("parameter %s of %s must be one line that does not start with - (%r)" % (k, op["id"], x))


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def compile_script(cat, task_id, steps, start_paths, workdir):
    """Turn a checked chain into a script: one argv list per step, run without a shell. Commands come only from the
    declarations; an agent can only give parameters. A chain that fails check_chain is not compiled."""
    task = cat.tasks[task_id]
    bad = check_chain(cat, steps, task["start"], task["goal"], task["env"], require_params=True)
    if bad:
        raise CompileError("the chain does not pass the check: " + "; ".join(bad))
    if sorted(start_paths) != sorted(task["start"]):
        raise CompileError("give paths for the start %s (got %s)" % (task["start"], sorted(start_paths)))
    insts = [{"type": t, "path": os.path.abspath(p)} for t, p in start_paths.items()]
    env, script = task["env"], []
    for i, step in enumerate(steps, 1):
        op, params = cat.ops[step["op"]], step.get("params") or {}
        for k, v in params.items():
            _check_param(op, k, v)
        skill_dir = cat.skills[op["skill"]]["dir"]
        port = _free_port() if "{port}" in _dump([op["argv"], op.get("value"), op.get("cwd")]) else None
        bound = {need: _latest(cat, insts, need) for need in op["inputs"]}
        produced, mkdirs, stdout_to = {}, [], None
        for t in op["outputs"]:
            kind, base = cat.types[t]["kind"], os.path.join(workdir, "%02d-%s" % (i, t))
            produced[t] = {"file": base + (cat.types[t].get("ext") or ""), "dir": base, "stdout": base + ".out"}.get(kind)
            if kind == "dir":
                mkdirs.append(base)
            if kind == "stdout":
                stdout_to = produced[t]

        def sub(s, bound=bound, produced=produced, params=params, skill_dir=skill_dir, port=port):
            def one(m):
                if m.group(1) == "in":
                    src = bound[m.group(2)]
                    return src["path"] if src.get("path") else src["value"]
                if m.group(1) == "out":
                    return produced[m.group(2)]
                if m.group(1) == "param":
                    return params[m.group(2)]
                return {"skill_dir": skill_dir, "lib_root": os.path.dirname(skill_dir), "port": str(port),
                        "home": os.path.expanduser("~"), "python": sys.executable}[m.group(3)]
            return PLACEHOLDER.sub(one, s)

        argv = []
        for a in op["argv"]:
            m = re.fullmatch(r"\{param:([^{}]+)\}", a)
            argv += list(params[m.group(1)]) if m and isinstance(params.get(m.group(1)), list) else [sub(a)]
        value = sub(op["value"]) if op.get("value") else None
        for t in op["outputs"]:
            insts.append({"type": t, "path": produced[t], "value": value})
        if op.get("env_after") and op["env_after"] != env:
            env = op["env_after"]
            insts = [x for x in insts if cat.types[x["type"]]["kind"] != "value"]
        script.append({"op": op["id"], "argv": argv, "cwd": sub(op["cwd"]) if op.get("cwd") else workdir,
                       "mkdirs": mkdirs, "stdout_to": stdout_to, "service": bool(op.get("service")), "port": port,
                       "manual": op.get("manual"), "success": op["success"], "check": op["check"], "value": value})
    return script


def _wait_port(port, seconds):
    end = time.time() + seconds
    while True:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return True
        except OSError:
            if time.time() >= end:
                return False
            time.sleep(0.1)


def run_script(script, timeout=300):
    """Run the steps in order. A service (a local HTTP server) starts in the background, the next step waits for its
    port, and it is stopped when the chain ends. A step that needs a person stops the chain (stopped-at-manual), and so
    does an exit code that is not a success code (failed). Returns (status, one result per step run)."""
    results, services, status = [], [], "succeeded"
    try:
        for s in script:
            t0 = time.time()
            if s["manual"]:
                results.append({"op": s["op"], "exit": None, "seconds": 0.0, "note": s["manual"]})
                status = "stopped-at-manual"
                break
            for d in s["mkdirs"]:
                os.makedirs(d, exist_ok=True)
            if s["service"]:
                p = subprocess.Popen(s["argv"], cwd=s["cwd"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                services.append(p)
                ok = _wait_port(s["port"], 10)
                results.append({"op": s["op"], "exit": None if ok else p.poll(), "seconds": round(time.time() - t0, 2),
                                "note": "service started at %s" % s["value"] if ok else "the service did not start"})
                if not ok:
                    status = "failed"
                    break
                continue
            out = open(s["stdout_to"], "w") if s["stdout_to"] else subprocess.DEVNULL
            try:
                r = subprocess.run(s["argv"], cwd=s["cwd"], stdout=out, stderr=subprocess.PIPE,
                                   universal_newlines=True, timeout=timeout)
                code, err = r.returncode, r.stderr
            except subprocess.TimeoutExpired:
                code, err = None, "longer than %d s" % timeout
            finally:
                if s["stdout_to"]:
                    out.close()
            res = {"op": s["op"], "exit": code, "seconds": round(time.time() - t0, 2)}
            if code not in s["success"]:
                res["stderr"] = (err or "")[-400:]
                results.append(res)
                status = "failed"
                break
            results.append(res)
    finally:
        for p in services:
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
    return status, results


def chain_id(task, steps):
    return hashlib.sha1(_dump([task, steps]).encode()).hexdigest()[:12]


def run_chain(conn, task, steps, start_paths, workdir, actor="owner", timeout=300):
    """Propose (proposeChain) -> compile -> run -> record (recordChainRun, by this program, never by the agent)."""
    steps = [{"op": s["op"], "params": s.get("params") or {}} for s in steps]
    r = act(conn, "proposeChain", {"task": task, "steps": steps}, actor)
    if not r["ok"]:
        return {"proposed": r, "status": None, "results": []}
    cid = chain_id(task, steps)
    script = compile_script(Catalog(View(conn)), task, steps, start_paths, workdir)
    status, results = run_script(script, timeout)
    rec = act(conn, "recordChainRun", {"chain": cid, "status": status, "results": results}, "program:ontology-run")
    return {"chain": cid, "status": status, "results": results, "recorded": rec, "script": script}


# ---------------------------------------------------------------- action types: criteria and rules
def _crit_sync_skill(v, p, actor):
    if p["name"] == "builtin":
        return [] if p["md5"] == "n/a" else ["the md5 of builtin can only be n/a"]
    if not os.path.isdir(p["dir"]):
        return ["skill folder %s does not exist" % p["dir"]]
    real = skill_md5(p["dir"])
    return [] if real == p["md5"] else ["md5 %s of %s is not the skill's md5 %s" % (p["md5"][:8], p["name"], str(real)[:8])]


def _rule_sync_skill(v, p, actor):
    """A skill changed: its operations declared against another version become stale, and accepted chains using them
    need a recheck (in the same transaction)."""
    edits = [{"op": "put", "type": "Skill", "pk": p["name"], "props": {"md5": p["md5"], "dir": p["dir"]}}]
    stale = set()
    for oid, op in v.all("Operation").items():
        if op["skill"] == p["name"] and p["name"] != "builtin" and op.get("skill_md5") != p["md5"]:
            stale.add(oid)
            if op["status"] != "stale":
                edits.append({"op": "put", "type": "Operation", "pk": oid, "props": dict(op, status="stale")})
    for cid, ch in v.all("Chain").items():
        if ch["status"] == "accepted" and any(s["op"] in stale for s in ch["steps"]):
            edits.append({"op": "put", "type": "Chain", "pk": cid, "props": dict(ch, status="needs-recheck")})
    return edits


def _crit_named(typ):
    def crit(v, p, actor):
        return [] if re.fullmatch(r"[a-z0-9-]+", p.get("name") or "") else ["a %s name uses lower-case letters, digits and -" % typ]
    return crit


def _rule_put(typ, fields):
    def rule(v, p, actor):
        return [{"op": "put", "type": typ, "pk": p["name"], "props": {k: p.get(k) for k in fields}}]
    return rule


def _crit_artifact(v, p, actor):
    out = _crit_named("ArtifactType")(v, p, actor)
    if p.get("kind") not in KINDS:
        out.append("kind of %s must be one of %s" % (p.get("name"), KINDS))
    out += ["interface %s of %s is not registered" % (i, p.get("name")) for i in p.get("implements") or [] if v.get("Interface", i) is None]
    return out


def _sync_links(v, src, link_names, want):
    """Make src's links of these many-to-many types exactly `want` ({(link type, other end)})."""
    have = {(l, d) for l in link_names for _, d in v.links(l, src)}
    return ([{"op": "unlink", "link": l, "src": src, "dst": d} for l, d in sorted(have - want)] +
            [{"op": "link", "link": l, "src": src, "dst": d} for l, d in sorted(want - have)])


def _rule_artifact(v, p, actor):
    put = {"op": "put", "type": "ArtifactType", "pk": p["name"],
           "props": {"kind": p["kind"], "description": p["description"], "ext": p.get("ext")}}
    return [put] + _sync_links(v, p["name"], ["implements"], {("implements", i) for i in p.get("implements") or []})


def _crit_env(v, p, actor):
    out = _crit_named("Environment")(v, p, actor)
    return out + ["condition %s of %s is not registered" % (c, p.get("name")) for c in p.get("provides") or [] if v.get("Condition", c) is None]


def _rule_env(v, p, actor):
    put = {"op": "put", "type": "Environment", "pk": p["name"], "props": {"source": p["source"]}}
    return [put] + _sync_links(v, p["name"], ["provides"], {("provides", c) for c in p.get("provides") or []})


def _crit_operation(v, p, actor):
    oid, out = p.get("id") or "", []
    if not re.fullmatch(r"[a-z0-9-]+\.[a-z0-9-]+", oid) or not oid.startswith("%s." % p.get("skill")):
        out.append("an operation id is skill.operation (%s)" % oid)
    skill = v.get("Skill", p.get("skill"))
    if skill is None:
        return out + ["no skill %s" % p.get("skill")]
    if p["skill"] != "builtin":
        if p.get("skill_md5") != skill["md5"]:
            out.append("%s was checked against skill md5 %s, the skill is now %s: check the declaration again"
                       % (oid, str(p.get("skill_md5"))[:8], skill["md5"][:8]))
        if not p.get("entry") or not os.path.isfile(os.path.join(skill["dir"], p["entry"])):
            out.append("entry point %s of %s is not in the skill folder" % (p.get("entry"), oid))
    if not (p.get("doc") or "").strip():
        out.append("%s needs a source (doc)" % oid)
    out += ["input %s of %s is not a registered artifact type or interface" % (t, oid) for t in p.get("inputs") or []
            if v.get("ArtifactType", t) is None and v.get("Interface", t) is None]
    out += ["output %s of %s is not a registered artifact type" % (t, oid) for t in p.get("outputs") or [] if v.get("ArtifactType", t) is None]
    out += ["condition %s of %s is not registered" % (c, oid) for c in p.get("requires") or [] if v.get("Condition", c) is None]
    if p.get("env_after") and v.get("Environment", p["env_after"]) is None:
        out.append("environment %s that %s moves to is not registered" % (p["env_after"], oid))
    if p.get("check") and not p.get("success"):
        out.append("%s is a check: give its success exit codes" % oid)
    if not p.get("manual") and not p.get("argv"):
        out.append("%s runs, so it needs a command" % oid)
    for t in p.get("outputs") or []:
        a = v.get("ArtifactType", t)
        if a and a["kind"] == "value" and not p.get("value") and not p.get("manual"):
            out.append("%s makes the value %s: give its value template" % (oid, t))
    allowed = ({("in", t) for t in p.get("inputs") or []} | {("out", t) for t in p.get("outputs") or []}
               | {("param", k) for k in p.get("params") or []})
    for a in (p.get("argv") or []) + [p.get("value") or "", p.get("cwd") or ""]:
        out += ["%s in %s matches no input, output or parameter" % (m.group(0), oid)
                for m in PLACEHOLDER.finditer(a) if m.group(1) and (m.group(1), m.group(2)) not in allowed]
    if p.get("service") and "{port}" not in _dump(p.get("argv")):
        out.append("%s is a service: its command must use {port}" % oid)
    return out


def _rule_operation(v, p, actor):
    props = {k: p.get(k) for k in OBJECT_TYPES["Operation"]}
    props.update(status="current", params=p.get("params") or [], success=p.get("success") or [0],
                 check=bool(p.get("check")))
    want = ({("consumesAny" if v.get("Interface", t) else "consumes", t) for t in p.get("inputs") or []}
            | {("produces", t) for t in p.get("outputs") or []} | {("requires", c) for c in p.get("requires") or []})
    links = _sync_links(v, p["id"], ["consumes", "consumesAny", "produces", "requires"], want)
    return [{"op": "put", "type": "Operation", "pk": p["id"], "props": props}] + links


def _crit_task(v, p, actor):
    out = [] if re.fullmatch(r"[a-z0-9-]+", p.get("id") or "") else ["a task id uses lower-case letters, digits and -"]
    out += ["start %s is not a registered artifact type" % t for t in p.get("start") or [] if v.get("ArtifactType", t) is None]
    for g in p.get("goal") or []:
        if v.get("ArtifactType", g.get("type")) is None:
            out.append("goal %s is not a registered artifact type" % g.get("type"))
        if g.get("of") and v.get("ArtifactType", g["of"]) is None:
            out.append("the source %s of a goal is not a registered artifact type" % g["of"])
    if not p.get("goal"):
        out.append("a task needs a goal")
    if v.get("Environment", p.get("env")) is None:
        out.append("no environment %s" % p.get("env"))
    return out


def _rule_task(v, p, actor):
    return [{"op": "put", "type": "Task", "pk": p["id"],
             "props": {k: p.get(k) for k in ("start", "goal", "env", "description")}}]


def _crit_propose(v, p, actor):
    task = v.get("Task", p.get("task"))
    if task is None:
        return ["no task %s" % p.get("task")]
    if not p.get("steps"):
        return ["a chain needs at least one step"]
    return check_chain(Catalog(v), p["steps"], task["start"], task["goal"], task["env"], require_params=True)


def _rule_propose(v, p, actor):
    steps = [{"op": s["op"], "params": s.get("params") or {}} for s in p["steps"]]
    return [{"op": "put", "type": "Chain", "pk": chain_id(p["task"], steps),
             "props": {"task": p["task"], "steps": steps, "status": "accepted"}}]


def _crit_record(v, p, actor):
    ch = v.get("Chain", p.get("chain"))
    if ch is None:
        return ["no chain %s" % p.get("chain")]
    out = [] if ch["status"] == "accepted" else ["chain %s is %s; no run can be recorded" % (p["chain"], ch["status"])]
    if p.get("status") not in RUN_STATUSES:
        out.append("a run status is one of %s" % (RUN_STATUSES,))
    res = p.get("results") or []
    if len(res) > len(ch["steps"]) or any(r.get("op") != s["op"] for r, s in zip(res, ch["steps"])):
        out.append("the results must match the chain's steps one by one")
    if p.get("status") == "succeeded":
        if len(res) != len(ch["steps"]):
            out.append("a run that succeeded has a result for every step")
        ops = v.all("Operation")
        out += ["exit code %s of %s is not a success code" % (r.get("exit"), r["op"]) for r in res
                if not ops[r["op"]].get("service") and r.get("exit") not in ops[r["op"]]["success"]]
    return out


def _rule_record(v, p, actor):
    n = sum(1 for r in v.all("ChainRun").values() if r["chain"] == p["chain"])
    return [{"op": "put", "type": "ChainRun", "pk": "%s-r%d" % (p["chain"], n + 1),
             "props": {"chain": p["chain"], "status": p["status"], "results": p["results"], "t": _now()}}]


SETUP = ("program:", "owner")
# action type -> (version, who may do it, submission criteria, rule)
ACTIONS = {
    "syncSkill": (1, SETUP, _crit_sync_skill, _rule_sync_skill),
    "registerCondition": (1, SETUP, _crit_named("Condition"), _rule_put("Condition", ["description"])),
    "registerInterface": (1, SETUP, _crit_named("Interface"), _rule_put("Interface", ["description"])),
    "registerArtifactType": (1, SETUP, _crit_artifact, _rule_artifact),
    "registerEnvironment": (1, SETUP, _crit_env, _rule_env),
    "registerOperation": (1, SETUP, _crit_operation, _rule_operation),
    "registerTask": (1, SETUP + ("agent:",), _crit_task, _rule_task),
    "proposeChain": (1, SETUP + ("agent:",), _crit_propose, _rule_propose),
    "recordChainRun": (1, SETUP, _crit_record, _rule_record),     # an agent cannot record its own results
}


# ---------------------------------------------------------------- building the ontology
def _probe(cond):
    """Whether this machine has a condition. Names the probe does not know are never assumed."""
    if cond == "python3":
        return True
    if cond == "python310":
        return sys.version_info >= (3, 10)
    if cond == "loopback":
        try:
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            s.close()
            return True
        except OSError:
            return False
    if cond == "chrome":
        return bool(os.environ.get("CHROME") or shutil.which("google-chrome") or shutil.which("chromium")
                    or os.path.exists("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))
    if cond == "node22":
        node = shutil.which("node")
        if not node:
            return False
        try:
            r = subprocess.run([node, "--version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               universal_newlines=True, timeout=10)
            return r.returncode == 0 and int(r.stdout.strip().lstrip("v").split(".")[0]) >= 22
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return False
    if cond in ("git", "tailscale", "docker"):
        return shutil.which(cond) is not None
    return False


def build(conn, catalog=CATALOG, lib=SKILL_LIB, probed=None, actor="program:ontology-build"):
    """Skills + catalog + this machine -> the ontology, every write through an action; building again changes nothing
    when nothing changed. An environment with "probe": [...] gets the conditions this machine has (`probed`, a list,
    replaces the probe in tests). Returns the rejected submissions [(action, name, reasons)]."""
    cat = read_json(catalog)
    rejected = []

    def do(name, params):
        r = act(conn, name, params, actor)
        if not r["ok"]:
            rejected.append((name, params.get("name") or params.get("id"), r["reasons"]))

    for name in sorted(d for d in os.listdir(lib) if os.path.isdir(os.path.join(lib, d))):
        do("syncSkill", {"name": name, "md5": skill_md5(os.path.join(lib, name)) or "", "dir": os.path.join(lib, name)})
    do("syncSkill", {"name": "builtin", "md5": "n/a", "dir": os.path.dirname(os.path.abspath(__file__))})
    for name, d in sorted(cat["conditions"].items()):
        do("registerCondition", {"name": name, "description": d})
    for name, d in sorted(cat["interfaces"].items()):
        do("registerInterface", {"name": name, "description": d})
    for name, a in sorted(cat["artifact_types"].items()):
        do("registerArtifactType", dict(a, name=name))
    for name, e in sorted(cat["environments"].items()):
        if "probe" in e:
            provides = [c for c in e["probe"] if (c in probed if probed is not None else _probe(c))]
        else:
            provides = e["provides"]
        do("registerEnvironment", {"name": name, "provides": sorted(provides), "source": e["source"]})
    for op in cat["operations"]:
        do("registerOperation", dict(op, skill_md5=cat["declared_against"].get(op["skill"], "n/a")))
    for tid, t in sorted(cat["tasks"].items()):
        do("registerTask", dict(t, id=tid))
    return rejected


def pin(catalog=CATALOG, lib=SKILL_LIB):
    """Write each declared skill's current md5 into the catalog's declared_against. Run it after checking the
    declarations against the changed skills, not instead of checking them."""
    cat = read_json(catalog)
    skills = sorted({op["skill"] for op in cat["operations"]} - {"builtin"})
    cat["declared_against"] = {s: skill_md5(os.path.join(lib, s)) for s in skills}
    with open(catalog, "w", encoding="utf-8") as f:
        json.dump(cat, f, ensure_ascii=False, indent=1)
        f.write("\n")
    return cat["declared_against"]


# ---------------------------------------------------------------- command line
def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m herdr_py.ontology", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    for name in ("build", "find", "check", "run", "verify", "pin"):
        sp = sub.add_parser(name)
        if name != "pin":
            sp.add_argument("--db", required=True)
        if name in ("build", "pin"):
            sp.add_argument("--catalog", default=CATALOG)
            sp.add_argument("--lib", default=SKILL_LIB)
        if name in ("find", "check", "run"):
            sp.add_argument("--task", required=True)
        if name in ("check", "run"):
            sp.add_argument("--steps", nargs="+", required=True, help="operation ids; parameters as op:key=value,key=value")
            sp.add_argument("--actor", default="owner")
        if name == "run":
            sp.add_argument("--start", nargs="+", required=True, help="type=path")
            sp.add_argument("--work", required=True)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 2
    if a.cmd == "pin":
        print(json.dumps(pin(a.catalog, a.lib), indent=1))
        return 0
    conn = open_db(a.db)
    if a.cmd == "build":
        rejected = build(conn, a.catalog, a.lib)
        counts = {t: n for t, n in conn.execute("SELECT type, count(*) FROM objects GROUP BY type")}
        print(json.dumps({"objects": counts, "links": conn.execute("SELECT count(*) FROM links").fetchone()[0],
                          "actions": conn.execute("SELECT count(*) FROM action_log").fetchone()[0],
                          "rejected": rejected}, ensure_ascii=False, indent=1))
        return 1 if rejected else 0
    if a.cmd == "verify":
        diff = verify_log(conn)
        print("\n".join(diff) if diff else "consistent: the state equals the replayed action log")
        return 1 if diff else 0
    cat = Catalog(View(conn))
    task = cat.tasks.get(a.task)
    if task is None:
        print("no task %s" % a.task, file=sys.stderr)
        return 2
    if a.cmd == "find":
        plans = find_chains(cat, task["start"], task["goal"], task["env"])
        print(json.dumps(plans, indent=1))
        return 0 if plans else 1
    steps = []
    for s in a.steps:
        op, _, kv = s.partition(":")
        params = dict(x.split("=", 1) for x in kv.split(",")) if kv else {}
        steps.append({"op": op, "params": params})
    if a.cmd == "check":
        bad = check_chain(cat, steps, task["start"], task["goal"], task["env"])
        print("\n".join(bad) if bad else "passes")
        return 1 if bad else 0
    os.makedirs(a.work, exist_ok=True)
    out = run_chain(conn, a.task, steps, dict(x.split("=", 1) for x in a.start), a.work, a.actor)
    print(json.dumps({k: out[k] for k in out if k != "script"}, ensure_ascii=False, indent=1))
    return 0 if out.get("status") == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())
