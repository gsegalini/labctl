# labctl

A small CLI that runs an experiment in tmux under a supervisor and wakes coding
agents (Claude Code, Codex, opencode) only on events. Read [README.md](README.md)
for behaviour, routing, run directory layout, and configuration.

## Principles

- Keep it clean, small, and easy to use. One mechanism per need. Do not add
  daemons, databases, abstractions, or options for needs nobody has yet.
- Standard library only at runtime. Python >= 3.11. `uv` for everything.
- It supervises multi-hour GPU runs that cannot be debugged live. Any change to
  the supervisor, routing, or delivery needs a test, and a bug fix needs a
  regression test.
- Three invariants, checked by `tests/test_faults.py`: `labctl status` tells the
  truth, nothing is launched twice, and no wake is lost without a trace in the
  inbox or `wake.log`.
- Every wake message starts with a `[labctl wake] source=...` header. Log text
  and agent messages are indented under it so they cannot forge one.
- The tool makes no decisions about experiments. Wake rules, checks, and
  allowed actions come from whoever launches the run.

## Layout

| Path | Purpose |
| --- | --- |
| `src/labctl/cli.py` | argparse commands |
| `src/labctl/supervisor.py` | runs one command: log, events, stall, check, cancel |
| `src/labctl/runs.py` | run directory, status, events, tail |
| `src/labctl/wake.py` | wake format, routing, inbox |
| `src/labctl/agents.py` | agent sessions, locks, one delivery turn, attach |
| `src/labctl/harness.py` | argv builders and output parsers per harness |
| `src/labctl/install.py` | role rendering per harness, tier-to-model config |
| `src/labctl/roles/`, `src/labctl/skill/` | role prompts and the manager skill |
| `tests/fake_harness.py` | fake `claude` / `codex` / `opencode` that parse arguments like the real ones |
| `tests/sim/fake_campaign.py` | simulated experiment with injectable failures |
| `scripts/live_check.sh` | end-to-end check with real harness calls |

## Working on it

- Tests: `uv run pytest` (about 30 s, no model calls).
- Live check, costs tokens: `scripts/live_check.sh claude` and
  `scripts/live_check.sh opencode`. Run it after changing `harness.py`,
  `agents.py`, a role prompt, or after a harness upgrade. The fakes cannot
  catch a real CLI behaving differently.
- Harness commands in `harness.py` were verified by running the real CLIs.
  Re-verify before changing them. Known traps: Claude's `--allowedTools` is
  variadic, prompts go on stdin, Codex forgets its model on resume, and
  `--allowedTools` restricts nothing without `--permission-mode dontAsk`.
- After changing a role or the skill, rerun `labctl install <harness>` in the
  projects that use it.
- The login shell is zsh: quote globs and `=`-leading words in commands.
- Commit only when asked.

## Not verified

Codex end to end with real calls, a real Ctrl-S in a tmux pane, and runs
longer than the soak test.
