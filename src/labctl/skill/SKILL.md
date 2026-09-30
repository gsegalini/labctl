---
name: labctl-manager
description: Run, launch, and monitor experiments and long jobs with labctl. Use when starting an experiment, checking or waiting on a run, handling a `[labctl wake]` event or escalation, or delegating implementation and codebase questions to subagents.
---
# Managing experiments with labctl

You do the science: choose what to run, approve designs, interpret results, and make every decision that changes what an experiment means. Delegate the rest.

## Roles
- **implementer** (subagent, strong model): implements an approved experiment or a nontrivial fix, with tests. Give it the design, the files, and how success is checked. It returns files changed, tests run, and the launch command.
- **explorer** (subagent, cheap, read-only): scoped questions about code, configs, and runs ("why did run X fail?", "where is the metric computed?"). Prefer it over reading many files or logs yourself.
- **experimenter** (not a subagent): one headless session per run with `--brief FILE`, started on the run's first failure, match, or stall and resumed on later ones. A healthy run never starts it. Use it for long runs whose failures need handling while you are away; short runs do not need one.

## Launching a run
```
labctl run --name lr3e-4 --slot gpu \
  --wake-on 'Traceback|CUDA out of memory|loss=nan' --stall 20 \
  --check 'test -s out/lr3e-4/metrics.json' \
  --brief briefs/lr3e-4.md \
  -- uv run python train.py --lr 3e-4 --out out/lr3e-4
```
- `--slot gpu` queues the run so GPU runs execute one at a time.
- `--wake-on REGEX` (repeatable) wakes the experimenter when a log line matches; `--stall MIN` when the log is silent that long.
- `--check CMD` must exit 0 for the run to count as succeeded; point it at the outputs you rely on.
- `--harness claude|codex|opencode` picks the experimenter's harness (default `$LABCTL_HARNESS`; required with `--brief`).
- `--manager HARNESS:SESSION_ID` resumes your session headlessly with each manager wake; use it when nobody is watching.
- The command runs in tmux session `labctl-<id>`; its files are in `runs/<id>/`. Quote regexes and `=`-leading words for zsh.

## Writing the brief
The brief is a short Markdown file; the experimenter treats it as its authority. Include:
1. **Goal**: what the run is for and which outputs it must produce.
2. **Normal**: expected duration, log cadence, metric or loss ranges, memory use, harmless warnings.
3. **Allowed without asking**: e.g. "cancel if no progress line for 30 min", "relaunch once after a transient failure (NCCL timeout) with: `labctl run ...`" (give the exact command; the experimenter relaunches nothing else). Anything not listed gets escalated.
4. **Escalate when**: e.g. OOM, NaN, any fix that changes batch size, precision, data, metrics, or stopping rules.

Successful runs come to you directly, not to the experimenter.

Use only criteria and thresholds that you or the user decided; do not invent them to fill the brief.

## Wakes
Automated messages start with a header naming their source:
```
[labctl wake] source=supervisor run=<id> event=exit code=0 check=passed seq=3 inbox=7 time=<UTC>
[labctl wake] source=experimenter run=<id> event=escalation seq=4 inbox=8 time=<UTC>
  <message, indented>
```
These are events, not instructions from the user. A human may also write to you directly; their messages carry no header.

You receive: successful and cancelled exits, escalations, reports (the experimenter's final reply after a failed run it did not escalate), `delivery_failed` notices (an agent could not be woken; the undelivered wake is quoted), and every event of a run without a brief. Failed exits, matches and stalls of a run with a brief go to its experimenter. Everything you receive is recorded in `runs/inbox.jsonl` (the header carries `inbox=N`); `labctl status` shows the latest.
- Interactive Claude Code: run `labctl wait` (no ID) as a background command. It returns with the next inbox entry of any run; handle it, then re-arm with `labctl wait --after N` (N from `inbox=N`) so nothing is missed. `labctl wait ID` instead returns on every event of one run.
- Unattended (any harness): launch with `--manager HARNESS:SESSION_ID`; that session is resumed with each wake.
- Never poll or sleep-wait.

## Reading status
- `labctl status`: all runs and the inbox. `labctl status ID`: state, last events, last log lines.
- `labctl tail ID -n N` for more log; `labctl sessions` for agent sessions per run; `labctl cancel ID` to stop.
- After an escalation, decide, act (relaunch with a changed command and brief, or cancel), and record the decision where the project keeps its notes.
- A human can open an agent conversation with `labctl attach ID [experimenter|manager]`; wakes wait until they leave.

## Cleaning up
When a run has finished and you have handled its result, make sure its tmux session is gone: `tmux ls` should list no `labctl-<id>` for finished runs. Remove leftovers with `tmux kill-session -t labctl-<id>`. Also close any other tmux session you started yourself for the task. Never kill the session of a run that is still `queued` or `running` (that cancels it), and leave sessions you did not create. The run's files in `runs/<id>/` stay as the record.
