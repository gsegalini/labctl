# labctl

A small CLI for running long experiments so coding agents (Claude Code, Codex,
opencode) do not have to watch them. `labctl run` starts a command in tmux
under a thin supervisor that writes plain files and records a few kinds of
events: the command exited, a log line matched a pattern, output stalled, or a
check-in time was reached. Agents are woken only by those events.

Python >= 3.11, standard library only, Linux, tmux. `labctl` must be on the
agents' `PATH` (e.g. `uv tool install .`).

## Commands

```
labctl run [--name N] [--slot NAME] [--wake-on REGEX]... [--stall MINUTES]
           [--checkin MINUTES]... [--check CMD] [--brief FILE] [--harness claude|codex|opencode]
           [--manager HARNESS:SESSION_ID] -- <command...>
labctl status [ID]          # all runs + recent inbox, or one run in detail
labctl tail ID [-n N]       # last N log lines (default 40)
labctl wait ID [--after SEQ]   # the run's next event
labctl wait [--after N]        # the next manager wake of any run (inbox entry)
labctl cancel ID
labctl escalate ID [--fix] -m TEXT   # experimenter -> manager (--fix: a fix request)
labctl sessions [ID]        # agent sessions: role, harness, id, started, busy
labctl attach ID [ROLE]     # open the experimenter (default) or manager conversation
labctl install HARNESS [--project DIR]   # write role prompts and the manager skill
```

- `--slot NAME`: runs sharing a slot execute one at a time (an exclusive
  `flock` on `<runs>/.slots/NAME.lock`); waiting runs are `queued`.
- `--wake-on REGEX`: every log line matching it (`re.search`) records a
  `match` event. `\r` progress updates are matched too; only the last update
  of a line is logged. A line without a newline is kept to its last 64 KiB.
- `--stall MINUTES`: record one `stall` event after that long without output;
  re-armed when output resumes.
- `--checkin MINUTES` (repeatable, float): record one `checkin` event that long
  after the command starts (not after queueing on a slot), if it is still
  running. The launcher picks moments when the first outputs should exist
  (first progress lines, first checkpoint or result file); the brief says what
  to verify at each. A check-in where everything matches the brief ends
  silently: the experimenter does not wake the manager.
- `--check CMD`: after exit 0, run CMD through the shell in the run's cwd with
  `LABCTL_RUN_DIR` and `LABCTL_RUN_ID` set. A non-zero exit makes the run `failed`.
- `--brief FILE` (at most 64 KiB) gives the run an experimenter agent on
  `--harness` (default `$LABCTL_HARNESS`). `--manager` names an existing
  session to resume with manager wakes.
- `wait ID` blocks until an event with `seq > SEQ` exists (default: the latest
  when `wait` starts) and prints its wake; for a finished run it prints the
  final `exit` event at once. `wait` without ID does the same for inbox entries.
- `status` never relaunches anything. A run that claims to be `queued` or
  `running` without a live supervisor is `unknown (supervisor not running)`;
  one still `starting` after 30 s is `unknown (supervisor never started)`.

The command runs with the caller's environment plus `LABCTL_RUN_DIR`,
`LABCTL_RUN_ID`, `LABCTL_RUNS_DIR` and `PYTHONUNBUFFERED=1`, in its own
process group, with stdout and stderr merged. The run ends when that process
exits: output is read for 1 s more, then what is left of its process group
gets SIGTERM (a daemon that called `setsid` survives but no longer holds the
run). `cancel` sends SIGTERM to the group, then SIGKILL after 5 s.

The supervisor runs in tmux session `labctl-<id>` (`tmux attach` to watch).
Killing that session (SIGHUP), Ctrl-C in it (SIGINT) or `cancel` stops the run,
recorded as `cancelled` with the signal. If the pane is stopped (Ctrl-S), the
echo drops lines; the run and its `log` are unaffected. `labctl run` fails
before creating anything if the session name is taken, and waits until the
supervisor has started (its crash output goes to `supervisor.log`).

## Wakes and routing

Every wake starts with a header naming its source. The body is indented, so
only the first line is a header: the event's message, or for a `match` the
matched line, then the last 40 log lines (each cut at 500 characters).

```
[labctl wake] source=supervisor run=<id> event=exit code=1 check=none seq=3 time=<UTC>
[labctl wake] source=supervisor run=<id> event=checkin minutes=5 seq=2 time=<UTC>
[labctl wake] source=experimenter run=<id> event=escalation seq=4 inbox=2 time=<UTC>
[labctl wake] source=experimenter run=<id> event=fix_request seq=5 inbox=3 time=<UTC>
[labctl wake] source=experimenter run=<id> event=report code=0 check=passed seq=6 inbox=4 time=<UTC>
  <message>
```

- Cancelled exit, `escalation`, `fix_request`, `report`, `delivery_failed`: to the manager.
- Exit (successful or failed), `match`, `stall`, `checkin`: to the experimenter if the run has a brief, else to the manager.
- After an experimenter turn on an exit, the manager gets exactly one message
  for it: the `escalation` or `fix_request` sent during the turn, else the
  experimenter's final reply as `report` (with the exit's code and check).
- A harness that fails, times out (30 min, `LABCTL_HARNESS_TIMEOUT`) or prints no session id gives `delivery_failed` (source `labctl`) with the error and the undelivered wake.
- Everything for the manager is appended to `<runs>/inbox.jsonl`; with `--manager` that session is also resumed headlessly, otherwise an interactive manager runs `labctl wait` in the background.

The experimenter session starts on its first event (brief + wake) and is
resumed on later ones; a healthy run costs one turn per check-in plus one to
verify its outputs at exit. Delivery runs in a
detached `labctl _deliver`, so a slow agent never stalls the run. One turn at
a time per session (`flock`; the manager lock is shared by all runs), and all
events not yet delivered go into one turn, so a burst of matches costs a few
turns, not one each. Messages go to the harness on stdin. `labctl attach`
holds the session lock while a human is in the conversation; wakes queue
until they leave.

## Run directory

Runs live in `$LABCTL_RUNS_DIR`, or `./runs` relative to where `labctl` is invoked.

```
runs/
  inbox.jsonl      manager wakes {"n", "time", "run", "seq", "text"}, newest last
  .slots/ .sessions/   slot and manager-session locks
  <id>/
    run.json       command, cwd, created time, git commit + dirty flag, options
    status.json    state (starting|queued|running|succeeded|failed|cancelled), pids, times
    events.jsonl   {"seq", "time", "source", "run", "event", ...} per line
    log            merged stdout/stderr (plus --check output)
    exit-code      exit code; 128+N if killed by signal N
    brief.md       copy of --brief
    sessions.json  experimenter / manager: harness, session id, model, started, delivered
    wake.log       each agent delivery: command, exit code, output tail
    supervisor.log supervisor errors, if any
```

The caller's environment reaches the supervisor through a mode-0600
`.env.json` that the supervisor deletes first thing (and `run`/`cancel`
delete on failure).

## Configuration

### Setup per project

```
uv tool install --editable /path/to/labctl   # puts `labctl` on PATH
cd <project>
labctl install claude      # and/or: codex, opencode
```

`install` writes the three roles and the manager skill where each harness
looks for them, with the model for each role taken from the config below.
Rerun it after changing the config. It refuses to overwrite an agent file it
did not write. Add `runs/` to the project's `.gitignore`.

| Harness | Roles | Manager skill |
| --- | --- | --- |
| claude | `.claude/agents/{implementer,experimenter,explorer}.md` | `.claude/skills/labctl-manager/SKILL.md` |
| codex | `.codex/agents/*.toml` | `.agents/skills/labctl-manager/SKILL.md` |
| opencode | `.opencode/agents/*.md` | `.agents/skills/labctl-manager/SKILL.md` |

A harness reads agent files when a session starts, so restart open sessions
after `install`.

### Models: `~/.config/labctl/config.toml`

Each role has a tier, and the config maps tiers to model ids per harness.

| Tier | Used by |
| --- | --- |
| `frontier` | the manager (only used by labctl to resume a Codex manager, which does not remember its model) |
| `strong` | implementer |
| `cheap` | explorer and experimenter |

```toml
[claude]            # aliases (fable, opus, sonnet, haiku) or full model ids
frontier = "fable"
strong = "opus"
cheap = "sonnet"

[codex]             # bare model ids
frontier = "gpt-6-astra"
strong = "gpt-6.1-sol"
cheap = "gpt-6-luna"

[opencode]          # provider/model, as listed by `opencode models`
frontier = "opencode-go/kimi-k3"
strong = "opencode-go/mimo-v2.6-pro"
cheap = "opencode-go/mimo-v2.6-flash"
```

The file is optional. Any key it sets overrides the packaged default in
`src/labctl/defaults.toml`; missing keys keep the default. The packaged
opencode defaults name an `anthropic/` provider, so set `[opencode]` if your
opencode does not have that provider.

The model is fixed where it is used: `install` writes it into the role files,
and `labctl run --brief` records the experimenter's model in the run's
`sessions.json` when the run is created.

### Environment variables

| Variable | Meaning | Default |
| --- | --- | --- |
| `LABCTL_RUNS_DIR` | where runs and the inbox live | `./runs` |
| `LABCTL_HARNESS` | harness for the experimenter when `--harness` is not given | none (`--harness` required with `--brief`) |
| `LABCTL_CONFIG` | path of the model config file | `~/.config/labctl/config.toml` |
| `LABCTL_HARNESS_TIMEOUT` | seconds before a hung agent turn is killed and reported as `delivery_failed` | 1800 |

Set by labctl for the supervised command and its `--check`: `LABCTL_RUN_ID`,
`LABCTL_RUN_DIR`.

### Fixed limits

A brief is at most 64 KiB. Log lines quoted in a wake are cut at 500
characters, and a wake carries the last 40 log lines.

### What the experimenter may do

The experimenter runs headless with a narrow permission set: for Claude Code,
only `labctl` and `nvidia-smi` commands and file reads and searches (`Read`,
`Grep`, `Glob`; `--permission-mode dontAsk`); for Codex, the `workspace-write`
sandbox; for opencode, its default in-project permissions. Its prompt limits it
to the files the brief names, and it changes nothing: for a bug in the
experiment it sends `labctl escalate ID --fix` and the manager has it fixed.
Its authority beyond that comes from the run's brief.

## Example

```
$ labctl run --name demo --wake-on "step 3" -- sh -c 'for i in 1 2 3 4; do echo step $i; sleep 1; done'
demo
$ labctl wait demo
[labctl wake] source=supervisor run=demo event=match pattern=step 3 seq=1 time=...
  matched: step 3
  step 1
  step 2
  step 3
$ labctl wait demo --after 1
[labctl wake] source=supervisor run=demo event=exit code=0 check=none seq=2 time=...
...
```

## Development

```
uv run pytest                      # fake harnesses, no model calls (~30 s)
scripts/live_check.sh claude       # real harness calls against tests/sim/fake_campaign.py
```
