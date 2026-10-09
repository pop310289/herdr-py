# herdr-py

Drive several [OpenCode](https://opencode.ai) agents from one place, with each agent's live state taken from OpenCode's own
event stream: who is working, who needs an answer, who is done. A permission policy answers what it can and leaves the
rest to you (terminal dashboard, phone-friendly web page, or CLI). A small team mode adds a supervisor that checks the
work when an agent stops and tells it where to continue.

An unofficial Python take on ideas from [herdr](https://github.com/herdrdev/herdr) (Apache-2.0); not affiliated with it.
No herdr code is used. 中文說明：[README.zh-TW.md](README.zh-TW.md).

- Python 3.6 or newer, standard library only (runs on RHEL 8's built-in `platform-python`).
- Talks to `opencode serve` over HTTP + Server-Sent Events. Tested with OpenCode 1.18.32.

## How it differs from herdr

| | herdr | herdr-py |
|---|---|---|
| What it is | A terminal multiplexer: every agent's real TUI in a pane | A daemon that drives agents through `opencode serve` |
| Agent state | Integration plugins when installed, otherwise screen-reading rules | OpenCode's events (`session.status`, `permission.asked`, ...) |
| Headless `opencode serve` | Not tracked | The only mode it uses |
| Permission answers | A person in the pane (or keys sent by another agent) | A policy (allow / always / deny / ask) per agent and command; "ask" goes to a person |
| Agents supported | 22 (Claude Code, Codex, OpenCode, ...) | OpenCode only |
| A person typing into an agent's TUI | Yes | No (prompts only) |
| Supervisor / stall watchdog | No | Team mode (below) |
| Size | ~250k lines of Rust + a vendored terminal emulator | ~2k lines of Python |

## Install (no curl needed)

```bash
git clone https://github.com/pop310289/herdr-py
cd herdr-py && ./scripts/install.sh          # puts a `herdr-py` wrapper in ~/.local/bin
```

Or run it in place: `python3 -m herdr_py ...` from the repository folder.

## Quick start

```bash
# 1. an OpenCode server (any folder you want the agents to work in), set to ask before bash, edits and the rest:
#    with OpenCode's defaults those calls run without asking, and herdr-py's policy never sees them
OPENCODE_SERVER_PASSWORD=change-me OPENCODE_CONFIG_CONTENT='{"permission": {"edit": "ask", "bash": "ask", "webfetch": "ask", "external_directory": "ask", "doom_loop": "ask"}}' \
    opencode serve --port 4096 &
echo change-me > ~/.config/opencode-password && chmod 600 ~/.config/opencode-password

# 2. the herdr-py daemon
herdr-py serve --opencode http://127.0.0.1:4096 --password-file ~/.config/opencode-password \
    --policy examples/policy.json --http 127.0.0.1:8765 &
# it prints:  web UI: http://127.0.0.1:8765/#token=...

# 3. agents
herdr-py start fixer "Fix the failing test in test_stats.py" --budget 600
herdr-py start docs  "Write a README for this folder" --followup "Now add a usage example"
herdr-py tui        # up/down select, a approve, A always, r reject, p prompt, x abort, q quit (agents keep running)
herdr-py list | pending | read fixer | approve per_... | reject per_... --message "no network"
herdr-py prompt fixer "Also handle empty lists" --wait --timeout 900
herdr-py start fixer "Now add a test for it" --fresh   # same name, a new session once the last turn has finished
```

## Permission policy

OpenCode sends herdr-py a permission request only for the kinds its own config sets to "ask". With OpenCode's defaults
(checked with OpenCode 1.18.32) bash commands and edits just run: a curl the policy denies runs anyway. Start OpenCode
with `"permission": {"edit": "ask", "bash": "ask", "webfetch": "ask", "external_directory": "ask", "doom_loop": "ask"}`
(Quick start). `herdr-py serve` reads OpenCode's config and warns when any of these is not "ask"; `herdr-py status` shows
them as `opencode_does_not_ask`.

Rules are checked in order; the first match wins. `match` is a regular expression that must match the whole target
(the shell command for `bash`, otherwise the requested paths). `agent` is a shell-style glob.

```json
{"default": "ask",
 "rule": [
   {"permission": "bash", "match": "(python3 [\\w./-]+\\.py|ls( -\\w+)*)", "action": "allow"},
   {"permission": "external_directory", "action": "deny", "message": "Stay inside the project folder."},
   {"agent": "reviewer*", "permission": "edit", "action": "deny"}
 ]}
```

Actions: `allow` (once), `always`, `deny` (the agent reads the message), `ask` (waits for a person). JSON works on every
Python version; TOML (`.toml`) needs Python 3.11+. Keep shell operators (`; | & $`) out of allowed patterns.

## Team mode (supervisor)

`herdr-py team task.json --condition T --workdir DIR` runs one task with three roles: the executor (edits files), a
read-only verifier (explains why the acceptance check fails) and a read-only validator (compares the result with the task).
The supervisor is code, not a model: when the executor stops it runs the task's check; unless the check passes and the
validator accepts, it sends a "continue from here" prompt built from the check output, the roles' evidence, the
executor's `NOTES.md` and its recent tool log. If the executor makes no progress for `--stall` seconds, it is replaced by
a new session that receives a hand-off (that is how memory carries across sessions). `--condition S` (single agent) and
`N` (generic "check your work" reminders) exist for comparison. Bench: [`bench/p23`](bench/p23).

## Cooperative loops and the team knowledge base

`python3 -m herdr_py.coop` turns a rule-guided loop (one agent, fixed rules, a check after every step) into a team:
the rules become the **judge**, a program that scores every answer, and each member's next prompt also carries what
its teammates found and the judge verified. Members can be any mix of OpenCode agents (through the daemon), Codex CLI,
Claude Code CLI, or **any program** that reads the prompt on stdin and prints its reply, such as a loop you already have.

```sh
# 26 circles in a square (examples/coop): two program members, no model needed
python3 -m herdr_py.coop --task examples/coop/packing_task.md --judge "python3 examples/coop/packing_judge.py" \
    --mode C --rounds 3 --out runs/c1 \
    --member 'a=command:python3 examples/coop/packing_member.py --seed 1' \
    --member 'b=command:python3 examples/coop/packing_member.py --seed 2'
# the same with agents: --member x=codex  --member y=claude:haiku  --member z=opencode:ollama/qwen3-8b-32k:latest --socket SOCK
python3 -m herdr_py.teamkb runs/c1/kb     # every entry: score, who built on whom, repeats
```

- **Modes** with the same number of member turns, so whether sharing helps is measured, not assumed: `S` one member
  gets every turn, `I` members work independently (each sees only its own answers), `C` members see the team's verified
  answers and failures. Rounds are waves: briefs are built before the round starts, members run at the same time.
- **The judge** gets the answer file as its last argument and prints `{"status": "valid"|"invalid", "score": ...}`;
  `--judge-mode exit` takes a pass/fail check's exit code instead. A judge that crashes or hangs is recorded as a judge
  error, never as an invalid answer, and a member's claimed score is never used.
- **The knowledge base** (`herdr_py/teamkb.py`, an append-only log plus artifacts stored by hash) keeps every answer,
  failure and verdict. The same answer sent twice is recorded once and not judged again; a member can only name as a
  parent an entry it was shown; an artifact changed after it was proposed is not judged as the same one; several
  processes can write at once. Numbers: adoption rate, improvement after adoption, duplicate rate.
- Every turn is in `run.jsonl` (state, seconds, tokens, entry, verdict, and why a turn produced nothing); a turn that
  timed out or failed never contributes an answer. `summary.json` has the totals and the best score after each round.
- **One page per run**: `view.html` in the run folder, rewritten after every round (reload it while the run goes on;
  `python3 -m herdr_py.coopview RUN_DIR` writes it again). A cell per round and member shows how the turn ended
  (valid, invalid with the judge's reason, FAILED, no answer, timeout, backend error), seconds, tokens, how many
  commands a Codex member ran, whose entry it built on, and, folded, the end of the reply and the backend's stderr.
- `--stop-on-infra-error` ends a run after the round in which a member's backend or the judge broke (exit code 3);
  timeouts and invalid answers are the members' own problems and do not stop it.

### Improving herdr-py with its own loop

[`examples/self_improve`](examples/self_improve) points the same loop at herdr-py. A **scenario** is a test that fails
today and says what should happen; members hand in a unified diff; `patch_judge.py` applies it to a clean export of
a commit, refuses patches that touch the scenarios, the judge or an existing test, runs the existing suites (one
`--test` per platform) and scores the patch by how many scenarios pass. Before judging, it checks the base: its tests
must pass and every scenario must fail, or the judge reports itself broken instead of blaming the patch.
`make_task.py` builds the task text with the scenarios and the source files they name, for members without tools.
The scenarios in the folder are candidates (`pending-review`): a person reads and freezes a scenario before members
are run on it, and a person reviews the best patch before it is merged.

```sh
python3 examples/self_improve/patch_judge.py --repo . --base HEAD --scenarios examples/self_improve/scenarios --allow-pending --check
python3 examples/self_improve/make_task.py --repo . --base HEAD --scenarios examples/self_improve/scenarios --allow-pending > /tmp/task.md
```

## Event-driven team: a shared todo list and a planner woken by events

`python3 -m herdr_py.engine` replaces coop's rounds with events (definitions §23). A planner (any member backend)
keeps a shared todo list in the team knowledge base. When an answer has been judged or a todo has ended, the planner
is woken with the team's state, built by code from the records, and replies with todos to add or drop, or says the
task is done; a reply that names an unknown member or entry, or opens too many todos, is sent back with every reason.
The planner is not woken by every result: only when todos have ended and a member is free with nothing it can take,
or when as many todos have ended as there are members (a whole lap), and its prompt says who works on what and who is
free. A todo can come after others ("after": an id, or "#2" for the second todo of the same reply): it cannot be
taken until they have ended. A todo with "review": true is never given to whoever made what it reviews (the members
of its parents, and whoever did the todos it comes after), so nobody reviews their own work; a review meant for its own
author is sent back, and when nobody can take the open todos the planner is woken (the run stops once its wakes are
used up). `--time-limit S` starts no new work after S seconds. Any backend can plan or work; a planner's turn only
answers: a claude planner gets no tools (`--tools ""`), an opencode planner gets OpenCode's per-message tool switches
all off (`{"*": false}`; checked with OpenCode 1.18.32, where the same prompt with the tools left on called webfetch).
A member that is free takes the oldest open todo meant for it or for anyone, so a fast member never waits for a slow
one, and two members never take the same todo (the take is chosen and recorded under the knowledge base's file lock).
While members work, `TEAM_BOARD.md` in their folder (read-only) shows the latest verified results, failures and todos,
rewritten after every event. As in coop, only the judge's verdicts are shared: the planner's todos are plans, not facts.

```
 team knowledge base: entries, verdicts, todos (append-only)
    | an answer judged, a todo ended
    v
 planner woken -> todos added or dropped (checked by the program)
    v
 a free member takes a todo -> answers -> judged -> written back
    | (TEAM_BOARD.md shows the latest state while it works)
    +----> the next event
```

```sh
python3 -m herdr_py.engine --task examples/coop/packing_task.md --judge "python3 examples/coop/packing_judge.py" \
    --planner plan=claude --member a=claude --member b=claude --turns 4 --out runs/e1
python3 -m herdr_py.teamkb runs/e1/kb     # entries, verdicts and todos

# no model at all: a planner and members that are programs (about 20 s)
python3 -m herdr_py.engine --task examples/coop/packing_task.md --judge "python3 examples/coop/packing_judge.py" \
    --planner 'plan=command:python3 examples/engine/planner.py' \
    --member 'a=command:python3 examples/coop/packing_member.py --seed 1 --steps 40000' \
    --member 'b=command:python3 examples/coop/packing_member.py --seed 2 --steps 120000' \
    --member 'c=command:python3 examples/coop/packing_member.py --seed 3 --steps 300000' --turns 9 --out /tmp/e-demo
open /tmp/e-demo/view.html

# an all-OpenCode team through the daemon of the Quick start (its default socket): members work read-only in the run's
# board folder (--member-access research: plus web fetches, as the daemon's policy allows); an OpenCode in a container
# needs the run folder mounted at the same path
python3 -m herdr_py.engine --task examples/coop/packing_task.md --judge "python3 examples/coop/packing_judge.py" \
    --planner plan=opencode --member a=opencode --member b=opencode:ollama/qwen3-8b-32k:latest \
    --socket ~/.local/state/herdr-py/herdr-py.sock --turns 4 --out runs/e2
```

`view.html` (rewritten after every event, so it can be watched while the run goes on) draws the loop with this
run's numbers, a timeline with time running down the page (a column for the planner and one for each member, every
turn a bar, every result that woke the planner a dotted line), the best verified score over time, every todo from
added to ended and every planner turn. It is complete without JavaScript; with JavaScript a player replays the run
from the first event to the end: drag the time, and the loop lights up the stage that is working while its counts
follow; tap a bar for its todo, verdict and score. The page also shows the tools each turn used (a dot in its bar for
every web search, page fetched and file read, from the Claude and OpenCode logs) and what the team made, entry by
entry, with a line from every entry to each one it built on; an answer whose first line is `ARTIFACT: <kind>` is
grouped by that kind.

The run stops when the member turns are used up, the target score (`--target`) is reached, the planner says done,
its wakes are used up with nothing left to do, or `--patience` judged answers in a row did not beat the best.
`summary.json` counts todos taken twice (must be 0), turns that can be traced to their todo, board version and prompt
hash (must be all of them), each member's time free with nothing to take, the planner's share of the tokens, and how
often members read the board (from the Codex, Claude and OpenCode logs; programs keep none). Every verified result is
also a file in the board folder (`artifacts/<id>.txt`), so a teammate can open one too long for a prompt.
`--member-access research` lets Claude members search the web as well (WebSearch and WebFetch, allowed by a settings
rule and nothing else; checked with the real CLI: without the rule dontAsk refused WebSearch, and with it a read
outside the folder was still refused). Codex members stay read-only.

## DAG dispatch: steps that wait for each other

`python3 -m herdr_py.dag` runs a **plan**: steps, each done by one member in **its own git clone** and passed or
failed by a **judge** program. A step starts only when every step it needs has passed, and it sees only what those
steps produced. The plan is a JSON file, so how a team is organised (who does what, who waits for whom, who sees whose
work) is data you can change and compare.

```
mul ─┐                     a box is a step, done by one member in its own clone;
sub ─┴─> together ─┐       an arrow: start after that step passed, with its commit
div ───────────────┴─> docs
```

```sh
python3 examples/dag/make_demo.py /tmp/dag-demo          # a small repository and a plan for it (program members, no model)
python3 -m herdr_py.dag /tmp/dag-demo/plan.json --check  # the steps, level by level
python3 -m herdr_py.dag /tmp/dag-demo/plan.json --out /tmp/dag-demo/run1 --parallel 3
open /tmp/dag-demo/run1/view.html                         # the plan drawn as a graph, and every attempt
python3 -m herdr_py.dag --recheck /tmp/dag-demo/run1      # judge every passed step again in a fresh clone
```

- **Isolation is the program's job**: every attempt gets its own `git clone --shared`, with no remote; a step's clone
  gets the output commit of each step it needs (`refs/dag/<id>`) and nothing from any other step, and its prompt shows
  only those steps' summaries and diffs. Codex members work there in the `workspace-write` sandbox; Claude members get
  `--permission-mode acceptEdits` with file tools only (a read step: `dontAsk` with read tools). Checked against the
  real Claude Code: writes, reads, Glob and Grep outside the clone are refused; `--allowedTools` must not be used, as it
  let the member read and write anywhere. OpenCode members (through the daemon, `--socket`) get a new session working
  in the clone (OpenCode's `?directory=`); the daemon refuses their calls outside it and, in a read step, every edit
  and command, before the policy is asked. That holds only when OpenCode asks before those calls (Permission policy),
  so a step checks first that the daemon says it does and that OpenCode lists the clone as this machine does (an
  OpenCode in a container needs the run folder mounted at the same path); otherwise the run stops and says why.
  Checked with OpenCode 1.18.32: a read step's edit and a read of a file outside the clone were both refused (the
  file left as it was; OpenCode asked about the outside read as external_directory). Codex's sandbox does not
  limit reads, so a run counts the member tool events (Codex, Claude and OpenCode logs) that name another step's clone
  (`out_of_bounds` in `summary.json`).
- **Where a step's files start**: a step that needs nothing starts at the base commit; one need: that step's output;
  several: the base (two ways of doing one thing usually touch the same files, so the member compares and combines),
  or `"start": "merge"` (merged by the program; a conflict fails the step and names the files) or one of the needs. A
  retry continues from the failed attempt's commit, with the judge's words in the prompt.
- **The output is a commit made by the program**, not by the member: everything in the clone is committed when the turn
  ends, and the judge checks that commit (the contract of `coop`: the reply file is the last argument; a JSON verdict,
  or the exit code with `"judge_mode": "exit"`). Judge files resolve against the plan's folder, outside every clone, and
  are hashed when the run starts: a judge changed during the run is a judge error.
- **Failures**: a failed step blocks every step that needs it, and independent steps go on. A broken backend, a broken
  judge or a clone that cannot be made stops new steps (exit code 3; `--keep-going` fails only that step); a timeout or
  an invalid answer is the member's own failure. A step's time limit is the member's own time: for opencode members,
  the time the model provider lost (OpenCode reported a failed call and retried: the silence before the report and the
  retry itself) is added to it, up to one more limit; a provider that takes longer ends the turn as `provider_stall`,
  a broken setup (exit code 3), not the member's failure. A model that is merely slow to answer is the member's time.
  Seen with OpenCode 1.18.32 and a free model: a provider timeout cost one step 300 s of its 900 s.
- **Receipts and resume**: every dispatch, return, commit and verdict is in `events.jsonl` before the run goes on;
  `--resume` rebuilds the state from it, never runs a passed step again, runs again a step whose setup broke, and
  refuses if the plan, a task or a judge changed. `summary.json` counts, from the events alone, steps started before
  their needs passed and passed steps dispatched again (both must be 0), and the parallelism of the run.
- Nothing is merged into your repository or pushed: outputs are commits in the run folder, and merging is up to you.

## Example: a slide team

[`examples/slide_team`](examples/slide_team) rebuilds one infographic, TheAiEdge.io's "LLM Serving: When to Split Prefill
and Decode" (bring your own copy of the picture; it is not in this repo). Two drawers take turns, an art director
compares our picture with the original, and the supervisor program decides what is kept. Two architectures were tried:

- **code** (`slide_team.py`): drawers write a Python program that builds the slide. With qwen3 8B drawers this never
  produced a usable slide: syntax errors, misused helpers, finished work thrown away, and success reported anyway.
- **layout** (`layout_team.py`): a drawer only turns a checklist ([`layout/SPEC_portrait.md`](examples/slide_team/layout/SPEC_portrait.md))
  into a JSON list of components. `components.py` checks the list (errors say how to fix them) and draws it as SVG;
  `imgcmp.py` compares the render with the original square by square (standard-library PNG reader; PSNR is reported
  too); a revision is kept only when the score rises. Every mistake the program catches goes into a lessons list at the
  top of every later prompt. `--backend codex` runs the members as Codex CLI sessions instead of OpenCode agents;
  `--sessions fresh` (the default) starts every turn in a new session.

One run of each (score: share of colour squares that match the original; 1 means identical):

| Team | Whole slide | Rows: draft → kept |
|---|---|---|
| qwen3 8B drawers, Qwen3-VL 8B art director (OpenCode) | 0.667 | 0.763 → 0.763, 0.728 → 0.728, 0.513 → 0.513 |
| Codex drawers and art director | 0.781 | 0.775 → 0.864, 0.834 → 0.853, 0.562 → 0.634 |
| The same Codex run, drafts only | 0.720 | |
| A layout made by hand from the checklist | 0.720 | |

What it showed: the representation (JSON components drawn by tested code) made the difference between no slide and a
usable one. Keep-best turned away every revision that made things worse (all four valid qwen revisions, three of six
Codex ones). Revisions helped only when the art director's notes were specific and right; the local vision model mostly
gave none (it spent its 4,096 output tokens on reasoning). One run per team is not enough to call this general, and
the score has a blind spot: a box whose thin border turned the wrong colour still raised it.

Run it with `python3 examples/slide_team/run_demo.py --reference original.png --open-slide /path/to/open-slide-py`
(add `--backend codex` for Codex members). Renders use headless Chrome; the OpenCode team needs the image from
`examples/slide_team/Dockerfile` and the two Ollama models named in `run_demo.py`.

The score now also sees borders and text colour. `layout_team.py --score strict` (the default) keeps a revision only when
strict = (fill + stroke + text) / 3 from `scoring.py` rises: fill is the square match above, stroke and text are measured
on edge pixels only. On the Codex run's row 3, the draft scores strict 0.843 and the revision that turned a Prefill box's
border and title blue 0.777 (match said 0.562 → 0.634), so strict keeps the draft; putting only that border and title
back to orange moves match 0.6345 → 0.6368 but strict 0.777 → 0.861. `--score match` is the old rule.
Revisers still get the fill notes (`--notes match`, the default): with strict's colour notes instead, 12 Codex revisions
lost 0.072 strict on average and none improved, while 4 of 6 improved with the fill notes (one night, one-sided Fisher
p = 0.005; the revisions of one run are not independent, so treat it as a strong hint).
Drawers can also set border widths, title colours, dashed boxes, outlined tokens, taller grid cells, curved arrows and
italic text, and use an `svg` component: its markup is checked before it is drawn (SVG drawing elements only, no
scripts, events or animation, links only to `#id` or embedded png/jpeg/gif) and re-written from the parsed tree.

To compare teams, describe a run in a spec and repeat it: `python3 examples/slide_team/teamrun.py spec.json --repeat 5`.
The spec names the original, the members (drawers and one art director, each with its own backend: `opencode`,
`codex`, `claude` or `fake`, model, and fresh or kept sessions), the rounds, the OpenCode server (teamrun starts a
herdr-py daemon of its own for each run; it never starts OpenCode), the score that keeps a revision (`rounds.score`:
`strict` or `match`) and the output folder; the format is at the top of
[`teamrun.py`](examples/slide_team/teamrun.py). Every run gets `report.md` (rows draft -> kept, accepted and rejected
revisions, fix-ups, lessons, whole-slide match and PSNR, tokens and time per member) and the repeats get
`aggregate.md` (mean, min and max); `teamrun.py --compare RUN_A RUN_B` prints two aggregates side by side. The
numbers are computed from each run's files (`runreport.py`), never typed in. Claude Code members
([`claude_agents.py`](examples/slide_team/claude_agents.py)) run one `claude -p` per turn without the machine's
CLAUDE.md, hooks or MCP servers (`--safe-mode`) and without tools, except Read for the art director's pictures;
`fake` members answer from a script, for a dry run that calls no model.

## Debugging a run

`python3 -m herdr_py.diagnose RUN_DIR` reads what a run leaves behind (`state/events.jsonl` from the daemon,
`work/codex/events.jsonl` from Codex members, `work/chat.jsonl` from the team) and says what went wrong with each member
and what to do about it: a table per member (turns, tokens, valid outputs, accepted revisions) and findings, each with
the log lines that prove it and one suggestion. The rules come from real runs: output stopped at the token limit with no
text; a turn that ended with reasoning only; a session compacted mid-turn whose reply was the summary; a reply without
the asked format; the turn time limit; a turn that failed with an error; the same tool error or permission rejection
again and again; success claimed while the program's check of the same round failed; revisions rejected again and
again; a member that stopped without changing anything. Each turn gets at most one of the turn outcomes, the most
specific cause first. On the qwen3 run in the table above:

```
[1] output_limit  art: output stopped at the token limit with no text (2 turns)
    art turn 1  22:48:20  state/events.jsonl:52  step-finish reason length after 4,096 output tokens; 15,598 chars of reasoning, no text
    do: Raise the model's output limit (limit.output in the OpenCode model config) or ask for a shorter answer.
[5] compacted_reply  drawA: the session was compacted during the turn and the reply after it is not the asked format
    drawA turn 4  23:33:03  state/events.jsonl:316, state/events.jsonl:309  compacted at 23:42:13; the reply after it is the compaction summary: '## Objective ...
    do: Start every turn in a fresh session (agent.start with fresh=true; layout_team.py --sessions fresh) and put ...
```

`--member drawA --turn 4` prints that turn in full (prompt head, reply, reasoning length, tool calls, tokens, finish
reason, compactions, errors, the checks that followed); `--json` is for programs, and `herdr_py.diagnose.findings(run_dir)`
returns the findings as a list of dicts.

## Live team dashboard

One read-only HTML page that shows a team run while it happens: member cards (state, tokens, turns,
sessions, last words), the conversation as a timeline you can filter by member and kind, a score chart per row (the
draft and every revision, accepted or rejected, and the version kept), our picture next to the original with a slider
over the kept versions, the lessons list and the final numbers. Changes arrive over Server-Sent Events.

```bash
python3 -m herdr_py.dashboard ~/.cache/herdr-slides/<run> [--port 8770] [--socket <daemon socket>]
# prints  open: http://127.0.0.1:8770/#token=...
python3 -m herdr_py.dashboard ~/.cache/herdr-slides/<run> --html run.html
# the same page as one file, the run as it is now: open it in any browser, no server or token; it does not update
```

On a wide screen the conversation, the score charts and the pictures sit side by side. A saved page carries its
pictures inside (a finished run with six renders is about 2 MB) and only image files, never `state/token`.

RUN_DIR is a run folder from `run_demo.py` (or a `layout_team.py --workdir`). Member states come from
`work/codex/agents.json` (Codex members) or, with `--socket`, from the herdr-py daemon. Until `summary.json` is written
at the end, the scores are read from the conversation, so the chart moves during the run. Like the daemon's web page it
listens on 127.0.0.1 and needs the token in the printed link; it serves only image files inside RUN_DIR and follows no
symlinks (a run folder also holds the daemon's `state/token`), and it has no write endpoints.

## Programmatic use

The daemon listens on a Unix socket (default `~/.local/state/herdr-py/herdr-py.sock`) speaking newline-delimited JSON:

```json
{"id": "1", "method": "agent.start", "params": {"name": "a", "prompt": "...", "budget_s": 600}}
{"id": "1", "result": {"name": "a", "state": "starting", ...}}
```

Methods: `ping`, `agent.list`, `agent.get`, `agent.start` (`wait`, `fresh`, `directory`, `deny`, `tools`), `agent.prompt`
(`wait`, `timeout_s`), `agent.abort`, `agent.wait`, `agent.read`, `permission.list`, `permission.reply`, `folder.list`,
`events.subscribe`, `server.stop`. `tools` (OpenCode's tool switches, `{"*": false}` for none) goes with every prompt
to that agent. `agent.prompt`
with `wait` returns when the turn it started has finished; if OpenCode shows no activity within 5 s it fails with
`prompt_stalled` (the prompt may still arrive: read before resending). A prompt to an agent whose working turn was just
aborted first waits (up to 5 s) for OpenCode to report that turn's end, which can come late: read after the prompt, it
was taken for the end of the new turn. `--max-agents` and `--max-prompts` stop a runaway
manager agent. The HTTP API behind the web page needs the token from `<state dir>/token`.

`agent.start` with `fresh: true` and a name that already exists gives that agent a new OpenCode session once its turn
has finished. OpenCode compacts a long session at a moment nobody chooses (in one of our runs a drawer's reply after
compaction was a summary instead of the JSON it was asked for), so a caller that puts everything a turn needs into the
prompt can start every turn clean. Tokens, turns and decisions carry on; the old session's late events are ignored.

`agent.start` with `directory` makes the session work in that folder instead of OpenCode's own (OpenCode's
`?directory=`; its events come on OpenCode's `/global/event`, which the daemon follows), and `deny` lists permission
kinds refused for that agent before the policy is asked (`["edit", "bash"]` for an agent that only reads).
`folder.list` returns what OpenCode lists in a folder, an error when it cannot see it.

## RHEL 8 notes

- `python3` on RHEL 8 is 3.6 (`/usr/libexec/platform-python` is always there): fine for herdr-py; use JSON config files.
- Put the socket on a local filesystem (the default under `~/.local/state` is fine). Unix sockets fail on some network or
  VM-shared filesystems.
- OpenCode's glibc build runs on RHEL 8 (tested: 1.18.32, x86_64 and aarch64, a full agent turn); the musl build does not.
- A systemd user unit example is in [`examples/herdr-py.service`](examples/herdr-py.service). The web UI binds to
  127.0.0.1 by default; reach it through an SSH tunnel rather than opening a port.

## Checking a real OpenCode server

The tests use a fake OpenCode server. `scripts/check_opencode.py` runs eight scenarios against a real one through a
running daemon (connect, finish, follow-up, a command the policy allows, one it denies, one that asks a person, abort, a
fresh session) and keeps herdr-py's verdict apart from the model's (a model that ignores the prompt is not a herdr-py
fault). OpenCode 1.18.32 with the free model opencode/big-pickle: 8 of 8 (2026-10-09).

```sh
python3 scripts/check_opencode.py --socket SOCK --opencode http://127.0.0.1:4096 --password-file PASSFILE \
    --workdir THE_OPENCODE_FOLDER --model opencode/big-pickle      # daemon policy: scripts/check_opencode_policy.json
```

## Tests

```bash
python3 -m unittest discover -s tests        # 438 tests; fake OpenCode server, fake Codex and Claude Code CLIs, no model needed
python3 bench/p23/validate.py                # checks the bench graders inside the RHEL 8 image (needs Docker)
```

GitHub Actions (`.github/workflows/tests.yml`) runs the suite on RHEL 8's own Python 3.6 (UBI 8 container) and on the
newest Python, and `scripts/ci_privacy.py` fails a push whose commits carry Claude attribution trailers, home-directory
paths or e-mail addresses other than GitHub noreply ones.

## Limitations

The daemon drives OpenCode only (the slide team example can also drive Codex CLI and Claude Code members itself). Questions agents ask
can only be dismissed, not answered. The dashboard and web page show the latest
activity, not full transcripts (`herdr-py read`). Team mode is an experiment: see the bench results before relying on it.

MIT License.
