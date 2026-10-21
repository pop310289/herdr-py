---
name: build-ontology
description: Teach an agent to build a skill ontology - a typed catalog of what each skill eats, makes and needs, an engine whose only writes are actions with submission criteria and an action log, and checks that refuse a broken chain of skills before it runs. Use when agents must chain skills for tasks no single skill covers, when asked to build or extend an ontology, or when a rule must be enforced by code instead of remembered.
---

# Build a skill ontology

You will build three things and prove them:

1. an **engine**: one Python file, standard library only;
2. a **catalog**: one JSON file that declares the skills;
3. **tests** that show the engine refuses everything it must refuse.

Reference implementation: `herdr_py/ontology.py` (919 lines) and `tests/test_ontology.py` (42 tests). Example catalog
and five demonstration skills (three with scripts): `catalog.json` and `skills/` next to this file. Read this file first. Open the
reference only when a step tells you to, and use it to compare, not to copy blindly: your domain may need other types.

## The spirit: eight principles

Each principle prevents a failure that really happened while the reference was built. If you remember only this
section, remember it.

1. **A rule in code holds; a rule in memory does not.** The agent that built the reference had "numbers must come from
   programs" in its own notes and still typed four wrong numbers by hand in one day. On the same day a commit hook that
   enforced a different rule caught that agent every time. Put each rule you care about into a submission criterion or
   a check, never only into a prompt.
2. **Describe, check, record; let the agent compose; let an exit code decide.** The ontology says how skills connect
   and refuses wrong chains. The agent picks the chain. Success is the exit code of the chain's last step, a check.
   The agent that ran a chain never reports its result: a checker's exit code 2 means "could not check", and an agent
   will happily call that "no problems".
3. **Define before you build.** Write what the ontology must answer and how you will judge it before you choose any
   type. Otherwise the types follow the data you happen to have, not the questions.
4. **One way to write: actions.** An action takes parameters, checks its submission criteria, turns its rule into
   edits, and commits them in one transaction with a log entry that keeps every value before and after. Replaying the
   log must give the current state; anything written another way shows up as a difference.
5. **Model the world, not the data.** One type stands for one kind of thing. An entity and an observation of it are
   different types (a run is not a field of its chain). Every link answers one question you can say in a sentence.
   Counts are computed, never stored.
6. **Types are not enough; record where things came from.** A search once made a valid chain that probed the wrong
   web page: every type matched, the subject did not. Each artifact carries the types it was made from, and a goal
   names its source ("a probe report *from these notes*").
7. **Some things do not travel.** A URL of a server started in a container is gone after you hand the work to another
   machine; files in git are not. Give every artifact type a kind (file, dir, stdout, value) and drop values when the
   chain changes environment.
8. **Check your checks.** For every rule, write a test that fails when the rule is removed, then remove each rule one
   at a time (a mutation check) and see the tests fail. A check that cannot fail proves nothing. The mutation check of
   the reference found a search bug that every test had missed.

## The operation

Do the phases in order. Each ends with something you can run; do not start the next phase until it passes.

### Phase 0 - Define (no code)

- Write one paragraph: the question the ontology answers, who composes chains, and how success is decided.
- Write two or three predictions that could turn out false, each with how you will test it. Examples: "every broken
  chain is refused before it runs and no good chain is refused"; "the catalog matches the skills as shipped".
- Inventory the skills. For every entry point, read its SKILL.md and write down: the command, what it reads, what it
  writes, what it needs from the machine (a browser, a free port, git), which exit codes mean success, and the line
  where the SKILL.md says so.

Done when: the paragraph, the predictions and the inventory exist as a file.

### Phase 1 - Model the types (the semantic layer)

- Start from the reference's nine object types: Skill, Operation (one entry point of a skill), ArtifactType, Interface,
  Condition, Environment, Task, Chain, ChainRun. Add a type only for something none of them stands for.
- For each type write its key, its required properties, and the single action allowed to write it.
- For each link type write the question it answers. Many-to-many links go in a links table; a one-to-many link is a
  foreign-key property on the "many" side, checked on every write. A chain's step order is a property: links have no
  order.
- Give each artifact type a kind: `file`, `dir`, `stdout` (a command's output saved to a file) or `value`.

Done when: the three tables (object types, link types, artifact kinds) are written down.

### Phase 2 - The engine core (the kinetic layer)

Five tables:

```sql
objects(type, pk, props JSON, PRIMARY KEY(type, pk))
links(link, src, dst, PRIMARY KEY(link, src, dst))
action_log(id, action, version, actor, params, started, committed, edits)   -- edits keep before and after
log_touches(log_id, type, pk)                                                 -- which objects an action changed
rejected(id, action, version, actor, params, t, reasons)
```

`act(conn, action, params, actor)` is the only function that writes:

1. `BEGIN IMMEDIATE`: one write lock, so the criteria see the state the edits apply to.
2. Refuse if the actor may not do this action (agents: `agent:...`; programs: `program:...`; the owner).
3. Run the submission criteria; collect every reason, not just the first.
4. Run the rule inside a `SAVEPOINT`; refuse the whole action if any edit has an undefined property, misses a required
   one, or points a foreign key at nothing.
5. Refused: roll back to the savepoint and insert only the rejected row with its reasons.
6. Nothing changed: write no log entry, so building twice adds nothing.
7. Commit objects, links and the log entry together; on any unexpected error, roll everything back and raise.

`verify_log(conn)` replays the log onto an empty state and returns every difference from the current state.

Done when these tests pass: an action writes its object and log entry together; a refused action writes only the
rejection; a bad edit rolls back the action's earlier edits; an unexpected error rolls back and is raised; a foreign key
must point at an existing object; an undefined property is refused; a no-op is not logged; `verify_log` finds a direct
`UPDATE` and a direct `INSERT`; an agent cannot register operations or record runs.

### Phase 3 - Catalog and build

The catalog needs all of these keys: `declared_against`, `conditions`, `interfaces`, `artifact_types`, `environments`,
`operations`, `tasks`. A missing key stops the build.

- One operation per entry point. Its fields are listed in `README.md` ("Operation declarations"). The command is an
  argv list, never a shell string. Placeholders: `{in:TYPE}`, `{out:TYPE}`, `{param:NAME}`, `{skill_dir}`,
  `{lib_root}`, `{port}`, `{home}`, `{python}`.
- `declared_against` holds each skill's md5 at the moment you checked its declarations. When a skill changes, its
  operations turn stale and the chains using them need a recheck, in the same transaction. Check the declarations
  again, then run `pin`.
- An environment either declares its conditions (`"provides"`) or has them probed on this machine (`"probe"`). The
  probe must never assume a condition it cannot test.
- `build` registers everything through actions and stores the skill library as an absolute path.

Done when: `build` reports no refused submission; building again writes nothing; a declaration checked against an older
skill is refused; a changed skill marks its operations stale and its chains for a recheck; a relative library path still
runs from another folder.

### Phase 4 - The functions

- **Applying an operation** (`_step`): bind each input to the artifact made last that fits it (an interface counts);
  check every required condition against the current environment; record each output's origin as the union of its
  inputs' origins; when the operation moves the chain to another environment, drop every `value` artifact.
- **check_chain**: apply the steps in order and report, per step, an unknown operation, a stale one, a parameter it does
  not take (and, for a proposal, a missing one), an input nobody made, or a missing condition. Then report every goal
  not made from its source, and a last step that is not a check. An empty list means it passes. This function is also
  the submission criterion of `proposeChain`, so there is one definition of a valid chain, not two.
- **find_chains**: breadth-first, level by level, keeping only the shortest chains. Key a state by its artifacts (type
  and origin) and its environment. When several paths reach the same state in one level, keep the one with the fewest
  steps that need a person. Count a set of operations once and order it to hand off as late as possible and to check
  as early as possible. Rank chains with fewer manual steps first, check each again, and return at most three.
- **compile_script**: refuse a chain that fails the check; build argv lists only from the declarations; accept a
  parameter only if it is one line that does not start with `-`; pass each step's output paths to the next step.
- **run_script / run_chain**: propose, compile, run, record. Run services in the background and stop them when the
  chain ends; stop at a step that needs a person or at an exit code that is not a success code. The program records
  the run; the agent cannot.

Done when: every good chain passes (include one with two artifacts of the same interface, so "the one made last"
matters); each broken chain is refused with its own reason (wrong order, missing producer, missing condition, a value
carried across a handoff, a report about the wrong source, a last step that is not a check, an unknown operation, an
unknown parameter, a missing parameter); every chain `find_chains` returns passes the check and has no step that can be
dropped; a chain with a manual step whose name sorts first still ranks after the automatic one; parameters cannot become
options; a real chain runs end to end.

### Phase 5 - Prove it

- Run the whole suite on the oldest Python you support. The reference supports Python 3.6: no
  `from __future__ import annotations`, no `capture_output` or `text=` in `subprocess.run`, and `http.server` has no
  `--directory` (run it with `cwd` instead).
- Mutation check: remove or flip each rule, one at a time, in a throwaway copy, and run the tests. The reference has
  40 mutants; 37 make a test fail. The other 3 change no behaviour, and the reason is written next to each: a filter
  repeated by a later check, a state that is already in the seen set, and a value drop that the check has already
  enforced. Any other survivor is a missing test: write the test, see it fail on the mutant, run the check again.
- Run one chain for real, on a skill's own example. Then `verify_log` must return nothing, and a rebuild must add no
  log entry.

### Phase 6 - Give it to agents

- Agents get `find_chains`, `check_chain` and `proposeChain`, acting as `agent:<name>`. They never get
  `registerOperation` or `recordChainRun`.
- Read the `rejected` table now and then: the same refusal again and again means a skill's documentation or a
  declaration is missing something.

## Worked example: a new ontology from nothing

One skill, one task, run from an empty folder (with this repository on `PYTHONPATH`):

```
my-ontology/skills/word-count/SKILL.md     python3 count.py TEXT  -> prints the word count; exit 0 if any words, 1 if none
my-ontology/skills/word-count/count.py
my-ontology/catalog.json:
{"declared_against": {},
 "conditions": {"python3": "a Python 3 interpreter"},
 "interfaces": {},
 "artifact_types": {"text": {"kind": "file", "ext": ".txt", "description": "a text file"},
                    "count-report": {"kind": "stdout", "description": "the word count"}},
 "environments": {"laptop": {"probe": ["python3"], "source": "probed when the ontology is built"}},
 "operations": [{"id": "word-count.check", "skill": "word-count", "entry": "count.py",
                 "argv": ["{python}", "{skill_dir}/count.py", "{in:text}"],
                 "inputs": ["text"], "outputs": ["count-report"], "requires": ["python3"],
                 "check": true, "success": [0], "doc": "word-count/SKILL.md line 3"}],
 "tasks": {"count-words": {"start": ["text"], "goal": [{"type": "count-report", "of": "text"}],
                           "env": "laptop", "description": "count the words of a text"}}}
```

```
python3 -m herdr_py.ontology pin   --catalog my-ontology/catalog.json --lib my-ontology/skills
python3 -m herdr_py.ontology build --db my.db --catalog my-ontology/catalog.json --lib my-ontology/skills
python3 -m herdr_py.ontology find  --db my.db --task count-words          # [["word-count.check"]]
python3 -m herdr_py.ontology run   --db my.db --task count-words --steps word-count.check \
        --start text=hello.txt --work out                                 # succeeded; out/01-count-report.out holds the count
```

Without `pin`, `build` refuses the operation: "word-count.check was checked against skill md5 n/a, the skill is now
...: check the declaration again". That is the intended behaviour: nobody has confirmed that the declaration matches
the skill.

## Pitfalls that happened (symptom, cause, fix)

| Symptom | Cause | Fix |
| --- | --- | --- |
| The search returned a chain with a manual step and dropped the automatic one | states were deduplicated by "first path wins", and the manual step's name sorted first | per level, keep the path with the fewest manual steps; test with a manual step whose name sorts first |
| A valid chain probed the wrong page | goals named only a type | artifacts carry their origin; goals name their source |
| A chain used a local URL after handing the work to another machine | values were treated like files | drop `value` artifacts when the environment changes |
| A chain built from a relative library path failed when run | skill folders were stored as given and steps run in another folder | store absolute paths; test from another working folder |
| A ranking rule could be deleted with no test failing | in the example, alphabetical order happened to agree with the rule | add a case where the two disagree |
| A privacy scan missed the new files | `git diff HEAD` ignores untracked files | stage first, then scan `git diff --cached` |
| Tests broke on Python 3.6 | 3.7+ features | see Phase 5 |

## Done means

- [ ] Phase 0 file exists: the question, how success is decided, predictions, the skill inventory.
- [ ] `build` refuses nothing, and building again writes nothing.
- [ ] Every broken chain in Phase 4 is refused with its own reason; every good chain passes.
- [ ] The mutation check leaves only survivors whose reason you have written down.
- [ ] One chain ran for real; `verify_log` is empty.
- [ ] The whole suite passes on the oldest Python you support.
- [ ] Agents can propose chains but cannot record results or change declarations.

Use this folder as a skill: copy or link it as `build-ontology` into your agent's skills folder (for example
`~/.claude/skills/build-ontology` or `~/.agents/skills/build-ontology`).
