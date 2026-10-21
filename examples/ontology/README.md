# Skill ontology: chains of skills, checked before they run

`herdr_py/ontology.py` lets an agent chain skills for a task no single skill covers. The ontology **describes** what
each skill eats, makes and needs; **checks** a proposed chain before anything runs; and **records** every chain and
run. The agent **composes**. A chain succeeds only when its last step, a check, exits with a success code: the agent
never says so itself.

The design follows the Palantir Foundry Ontology: object types, link types, interfaces, actions with submission
criteria, and an action log (see [Palantir mapping](#palantir-mapping)). Standard library only, Python 3.6 and later.

The skills in this folder are a demonstration: they turn a notes file into a one-page site, lint it, serve it, and
probe its URL. Replace them with your own skills and catalog.

## Quick start

```
python3 -m herdr_py.ontology build --db onto.db                        # skills + catalog + this machine -> ontology
python3 -m herdr_py.ontology find  --db onto.db --task notes-site-builder
python3 -m herdr_py.ontology check --db onto.db --task notes-site-builder \
        --steps notes2site.build site-lint.check builtin.serve-local url-probe.check
python3 -m herdr_py.ontology run   --db onto.db --task notes-site-workstation \
        --steps notes2site.build site-lint.check builtin.serve-local url-probe.check \
        --start notes-md=examples/ontology/notes.md --work /tmp/onto-run
python3 -m herdr_py.ontology verify --db onto.db                       # the state must equal the replayed action log
```

`find` for a task that starts in a build container that cannot open ports returns the build and the lint first, then
the handoff, then the steps that need the workstation:

```
[["notes2site.build", "site-lint.check", "handoff.handoff", "builtin.serve-local", "url-probe.check"],
 ["notes2site.build", "site-lint.check", "handoff.handoff", "site-publish.publish", "url-probe.check"]]
```

`check` on the same chain without the handoff refuses it before anything runs (exit code 1):

```
step 3: builtin.serve-local needs loopback, which builder does not have
step 4: url-probe.check needs loopback, which builder does not have
```

## What you define

Seven things, in this order: the object types and link types (fixed in `ontology.py`), then in the catalog
(`catalog.json`) the artifact types and interfaces, the conditions and environments, one declaration per entry point
of each skill, and the tasks; the action types with their submission criteria are fixed in `ontology.py`. Before any
of it, write down what the ontology must answer and how you will judge it: here, "can an agent compose a chain that
runs and whose last check passes", judged by exit codes, not by the agent.

### 1. Object types

Each type has a key, required properties, and the one action that may write it. A missing required property or an
undefined one refuses the whole action.

| Type | Stands for | Key | Required properties | Written by | In the example |
| --- | --- | --- | --- | --- | --- |
| Skill | a skill folder (plus `builtin` for steps the ontology itself provides) | name | md5, dir | syncSkill | 6 |
| Condition | something a step needs, such as a free loopback port | name | description | registerCondition | 3 |
| Interface | a kind of artifact several types share | name | description | registerInterface | 2 |
| ArtifactType | what operations pass on: a file, a folder, a report, a URL | name | kind, description | registerArtifactType | 6 |
| Environment | where steps run, and which conditions it has | name | source | registerEnvironment | 2 |
| Operation | one entry point of a skill: what it eats, makes and needs, and its command | skill.operation | skill, argv, check, success, params, doc, status | registerOperation (syncSkill marks it stale) | 6 |
| Task | what to do: start, goal, environment | id | start, goal, env, description | registerTask | 2 |
| Chain | ordered steps an agent proposed for a task | hash of task + steps (12 hex) | task, steps, status | proposeChain (syncSkill marks it for a recheck) | made at run time |
| ChainRun | one execution of a chain | chain id + -rN | chain, status, results, t | recordChainRun | made at run time |

One type stands for one kind of thing; an entity and an observation of it are separate types (a ChainRun is not a
field of its Chain); counts are computed, not stored.

### 2. Link types

Every link type answers one question; a link nobody can phrase as a question is not made. Many-to-many links live in
the links table; one-to-many links are a foreign-key property, checked on every write.

| Link | From -> to | Side names | Answers | Stored as | In the example |
| --- | --- | --- | --- | --- | --- |
| skillOperation | Operation -> Skill | skill / operations | Which skill is this operation part of? | foreign key `skill` | - |
| consumes | Operation -> ArtifactType | inputs / consumers | What does this operation eat? | links table | 1 |
| consumesAny | Operation -> Interface | inputs / consumers | What kind of artifact does it eat? | links table | 4 |
| produces | Operation -> ArtifactType | outputs / producers | What does this operation make? | links table | 5 |
| requires | Operation -> Condition | requires / requiredBy | What does this operation need? | links table | 8 |
| provides | Environment -> Condition | provides / providedBy | What does this environment have? | links table | 5 |
| implements | ArtifactType -> Interface | interfaces / implementers | What kind of artifact is this? | links table | 3 |
| chainTask | Chain -> Task | task / chains | Which task is this chain for? | foreign key `task` | - |
| runChain | ChainRun -> Chain | chain / runs | Which chain did this run execute? | foreign key `chain` | - |

The order of a chain's steps is the Chain's `steps` property: links have no order.

### 3. Artifact types and interfaces

Each artifact type has a kind: where it lives and whether it survives a move to another environment.

| Kind | Is | On a handoff | Example types |
| --- | --- | --- | --- |
| file | one file | goes along (git) | notes-md |
| dir | one folder | goes along (git) | site |
| stdout | a command's output saved to a file (most check reports) | goes along (git) | lint-report, probe-report |
| value | a value such as a URL | **stays behind**; make it again there | local-url, public-url |

An interface lets an operation eat a whole kind of artifact, so a new skill that makes a static site needs no change
to the operations that serve or lint one.

| Interface | Means | Implemented by | Eaten by |
| --- | --- | --- | --- |
| static-site | a folder with index.html that a static server can serve | site | builtin.serve-local, site-lint.check, site-publish.publish |
| web-url | a URL a browser can open | local-url, public-url | url-probe.check |

A server started in the build container gives a `local-url` that is a value: after the handoff it is gone, so "serve
in the container, hand off, then probe" is refused.

### 4. Conditions and environments

A condition is what an operation requires and an environment provides; at every step of a chain, the environment the
chain is in at that point must have that step's conditions. An environment either declares its conditions
(`"provides"`) or has them probed on this machine when the ontology is built (`"probe"`).

| Condition | Means | builder (declared) | workstation (probed) |
| --- | --- | --- | --- |
| python3 | a Python 3.6+ interpreter (the one running the ontology) | yes | yes |
| loopback | can open a port on 127.0.0.1 and connect to it | no | yes |
| git | git, to hand files to another machine | yes | yes on a machine with git |

The probe knows `python3`, `python310`, `loopback`, `chrome`, `node22`, `git`, `tailscale` and `docker`; a name it does
not know is never assumed.

### 5. Operation declarations

Every entry point of every skill is one operation in `catalog.json`. Adding a skill means writing these, not code.

| Field | Means | Required |
| --- | --- | --- |
| id | skill.operation | always |
| skill | the skill's folder name; `builtin` for steps the ontology provides | always |
| entry | the entry-point file, relative to the skill folder; checked to exist | for a skill's operation |
| argv | the command as a list, run without a shell | for an operation that runs |
| cwd | where it runs; may use placeholders | no |
| inputs, outputs | artifact types it eats and makes (inputs may be interfaces) | always |
| requires | conditions it needs | always |
| check, success | whether it is a check; the exit codes that count as success | success when check is true |
| params | the only parameters an agent may give | no |
| value | for an output of kind value, how to make it (a URL template) | when an output is a value |
| service | runs in the background (a local server) and stops when the chain ends | no |
| manual | a person does this step; a run stops here | no |
| env_after | the environment the chain is in after this step (a handoff) | no |
| doc | where the command and its exit codes are written (SKILL.md lines) | always |

`declared_against` holds, per skill, the md5 the skill had when its declarations were checked. When a skill changes,
its operations become stale and are not used, and chains using them need a recheck: check the declarations against the
changed skill, then run `python3 -m herdr_py.ontology pin`. A skill's md5 is read from the library's `MANIFEST.json`
when there is one, else computed from its files.

Placeholders: `{in:TYPE}`, `{out:TYPE}`, `{param:NAME}`, `{skill_dir}`, `{lib_root}`, `{port}`, `{home}`,
`{python}` (the interpreter running the ontology). Each `{in:}`, `{out:}` and `{param:}` must match an input, output
or parameter of the same operation.

```json
{"id": "url-probe.check",
 "skill": "url-probe",
 "entry": "probe.py",
 "argv": ["{python}", "{skill_dir}/probe.py", "{in:web-url}"],
 "inputs": ["web-url"], "outputs": ["probe-report"], "requires": ["loopback", "python3"],
 "check": true, "success": [0],
 "doc": "url-probe/SKILL.md lines 8, 10"}
```

`success` is `[0]`, so exit code 2 ("could not fetch") never counts as a pass.

### 6. Tasks

A task names its start, its goal, and the environment it starts in.

| Task | Start | Goal | Environment |
| --- | --- | --- | --- |
| notes-site-builder | notes-md | lint-report (from notes-md), probe-report (from notes-md) | builder |
| notes-site-workstation | notes-md | lint-report (from notes-md), probe-report (from notes-md) | workstation |

A goal names where it must come from. Every artifact remembers the types it was made from, so a probe report about
some other site does not meet "probe-report (from notes-md)". Without this, a search happily probes the wrong page:
the types are right and the subject is wrong.

### 7. Actions and submission criteria

Actions are the only way to write. Each one has who may do it, criteria that refuse it, and a rule that makes the edits.

| Action | Who | Refused when |
| --- | --- | --- |
| syncSkill | programs, the owner | the md5 is not the skill's md5; the folder does not exist |
| registerCondition, registerInterface | programs, the owner | the name is not lower-case letters, digits and - |
| registerArtifactType | programs, the owner | the kind is not one of the four; an interface it implements is not registered |
| registerEnvironment | programs, the owner | a condition it provides is not registered |
| registerOperation | programs, the owner | the id is not skill.operation; it was checked against an older skill md5; the entry point is missing; no source; an input, output or condition is not registered; a placeholder matches nothing; a check without success codes |
| registerTask | programs, the owner, agents | a start, goal or goal source is not a registered type; no goal; unknown environment |
| proposeChain | programs, the owner, agents | no such task; `check_chain` finds a violation (missing parameters included) |
| recordChainRun | programs, the owner | the chain is not accepted; results do not match its steps; "succeeded" with an exit code that is not a success code |

One action, in order:

1. Take the single write lock (`BEGIN IMMEDIATE`), so the criteria see the state the edits apply to.
2. Check the actor.
3. Run the criteria; collect every reason that fails.
4. Make the edits inside a savepoint; an edit that does not fit the types refuses the whole action.
5. Refused: write only the rejected submission and its reasons.
6. Nothing changed: write no log entry, so building again adds nothing.
7. Commit objects, links and the log entry, which keeps every edit's value before and after; an unexpected error rolls
   everything back.

## Functions

- **check_chain** (the criteria of proposeChain): step by step, the operation exists, its skill is current, no
  parameter it does not take (and, for a proposal, none missing), every input made by the start or an earlier step
  (an interface counts), and the conditions are there; a handoff leaves values behind. Then every goal is made from
  the right start, and the last step is a check. Returns the violations; empty means it passes.
- **find_chains**: breadth-first from the start, level by level, keeping only the shortest chains (a shortest chain
  has no step that can be dropped). Of the paths that reach the same state, the one with fewer steps that need a
  person is kept. A set of operations counts once and is ordered to hand off as late as possible and check as early
  as possible. Chains with fewer manual steps come first; at most three are returned, each checked again. Stale
  operations are not used.
- **compile_script**: a chain that fails the check is not compiled. Commands come only from the declarations; an
  agent can give parameters only, each one line that does not start with `-`. Each step's output paths flow into the
  next; when several artifacts fit, the one made last is used.
- **run_script / run_chain**: steps run in order; a service starts in the background and stops when the chain ends; a
  manual step or an exit code that is not a success code stops the chain. The run is recorded by the program, as a
  ChainRun.
- **verify_log**: replaying the action log onto an empty state must give the current state; any difference is a write
  that went around the actions.

## Adding a skill

1. Read the skill's SKILL.md: for each entry point, its command, what it eats and makes, what it needs, and which exit
   codes mean success.
2. Register any new artifact type, interface or condition in `catalog.json`.
3. Add one operation per entry point, with `doc` pointing at the SKILL.md lines.
4. Run `python3 -m herdr_py.ontology pin` to record the skill's md5.
5. `build`: every refused declaration is listed with its reasons; fix until there are none.
6. Add a task; `find` should return a chain that uses the skill, and `check` should refuse a broken one.
7. `run` it once on the skill's own example; the exit codes must match.
8. `python3 -m unittest tests.test_ontology`: one test checks that the catalog matches the skills as shipped.

## What is checked

| What | Result | Where |
| --- | --- | --- |
| Tests | 41, on Python 3.6 (UBI 8.10) and current Python 3 | tests/test_ontology.py |
| Mutation check | 39 mutants of `ontology.py`, 36 caught; the 3 left change no behaviour (equivalent) | run with a throwaway copy, one edit at a time |
| The example chain | runs for real: build, lint, serve, probe (HTTP 200) | `test_the_example_chain_runs_for_real` |
| Broken chains | 8 kinds refused before running, plus a missing parameter and a changed skill | `CheckChain`, `CatalogMatchesSkills` |

Not measured yet: whether an agent given these tools composes better chains than an agent given only the skills'
descriptions. That needs model calls.

## Not defined yet

- The types themselves are code (9 object types, 9 link types, 9 actions). Letting agents propose new types means
  moving them into data, with the owner approving each one.
- Promoting a chain that keeps succeeding into a reusable skill: how many successes, on how many different tasks.
- Lessons attached to the operation they are about, handed to the agent when it picks that step.
- Typed parameters (paths, enumerations, ranges); today a parameter is one line of text.
- Choosing which of several artifacts of one type a step uses; today it is the one made last.

## Palantir mapping

| Here | Palantir Foundry Ontology documentation |
| --- | --- |
| Writes go only through actions | new object types allow edits only through actions by default ([action permissions](https://www.palantir.com/docs/foundry/action-types/permissions/)) |
| All criteria must hold | "Actions can only be submitted if all the submission criteria are met." ([submission criteria](https://www.palantir.com/docs/foundry/action-types/submission-criteria/)) |
| The action log | "The action log models successful action submissions as object types" ([action log](https://www.palantir.com/docs/foundry/action-types/action-log/)) |
| One transaction, seen by what starts after | "any action or query that starts afterward sees all edits" ([consistency guarantees](https://www.palantir.com/docs/foundry/action-types/consistency-guarantees/)) |
| Counts are computed | "Store each fact once." ([structural guidance](https://www.palantir.com/docs/foundry/ontology/ontology-structural-guidance/)) |
| Operations eat interfaces | "Build actions, functions, and applications against interfaces" ([structural guidance](https://www.palantir.com/docs/foundry/ontology/ontology-structural-guidance/)) |
| Chains and runs are recorded | decisions and their outcomes are kept in the ontology ([why ontology](https://www.palantir.com/docs/foundry/ontology/why-ontology/)) |
| Actions as tools for agents | "actions can be automatically surfaced as tools for all types of agents" ([why ontology](https://www.palantir.com/docs/foundry/ontology/why-ontology/)) |
| Not implemented: revert | "can only be reverted if the action is the most recent edit to an object" ([action reverts](https://www.palantir.com/docs/foundry/action-types/action-reverts/)) |
