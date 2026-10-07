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
# 1. an OpenCode server (any folder you want the agents to work in)
OPENCODE_SERVER_PASSWORD=change-me opencode serve --port 4096 &
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

One read-only page that shows a team run while it happens, on a phone or a desktop: member cards (state, tokens, turns,
sessions, last words), the conversation as a timeline you can filter by member and kind, a score chart per row (the
draft and every revision, accepted or rejected, and the version kept), our picture next to the original with a slider
over the kept versions, the lessons list and the final numbers. Changes arrive over Server-Sent Events.

```bash
python3 -m herdr_py.dashboard ~/.cache/herdr-slides/<run> [--port 8770] [--socket <daemon socket>]
# prints  open: http://127.0.0.1:8770/#token=...
```

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

Methods: `ping`, `agent.list`, `agent.get`, `agent.start` (`wait`, `fresh`), `agent.prompt` (`wait`, `timeout_s`), `agent.abort`,
`agent.wait`, `agent.read`, `permission.list`, `permission.reply`, `events.subscribe`, `server.stop`. `agent.prompt`
with `wait` returns when the turn it started has finished; if OpenCode shows no activity within 5 s it fails with
`prompt_stalled` (the prompt may still arrive: read before resending). `--max-agents` and `--max-prompts` stop a runaway
manager agent. The HTTP API behind the web page needs the token from `<state dir>/token`.

`agent.start` with `fresh: true` and a name that already exists gives that agent a new OpenCode session once its turn
has finished. OpenCode compacts a long session at a moment nobody chooses (in one of our runs a drawer's reply after
compaction was a summary instead of the JSON it was asked for), so a caller that puts everything a turn needs into the
prompt can start every turn clean. Tokens, turns and decisions carry on; the old session's late events are ignored.

## RHEL 8 notes

- `python3` on RHEL 8 is 3.6 (`/usr/libexec/platform-python` is always there): fine for herdr-py; use JSON config files.
- Put the socket on a local filesystem (the default under `~/.local/state` is fine). Unix sockets fail on some network or
  VM-shared filesystems.
- OpenCode's glibc build runs on RHEL 8 (tested: 1.18.32, x86_64 and aarch64, a full agent turn); the musl build does not.
- A systemd user unit example is in [`examples/herdr-py.service`](examples/herdr-py.service). The web UI binds to
  127.0.0.1 by default; reach it through an SSH tunnel rather than opening a port.

## Tests

```bash
python3 -m unittest discover -s tests        # 257 tests; fake OpenCode server, fake Codex and Claude Code CLIs, no model needed
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
