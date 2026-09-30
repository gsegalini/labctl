#!/bin/sh
# Live check of labctl with a real agent harness, against the fake campaign
# (tests/sim/fake_campaign.py). Makes real model calls.
#
#   scripts/live_check.sh claude|opencode|codex
#
# Scenarios (run in parallel, in a fresh temp project):
#   a  clean success, no brief, no --manager: an inbox entry
#   b  failure with a brief: the experimenter starts; the inbox gets an escalation or report
#   c  --wake-on loss=nan match, later failure: the same experimenter session resumed, two turns
#   d  stall with a brief: the experimenter is woken
#   e  cancel: recorded as cancelled, in the inbox
#   f  unattended manager: a real headless manager session gets the wake
#   g  --checkin with a brief, wrong early output: the inbox gets a fix_request (or escalation)
#   h  success with a brief: the experimenter verifies the outputs; the inbox gets one report
# Models come from a temp LABCTL_CONFIG: claude uses $CLAUDE_MODEL (sonnet) for
# every tier, opencode $OPENCODE_MODEL (opencode-go/mimo-v2.6-flash); codex keeps
# labctl's defaults. Each scenario waits up to $LIVE_TIMEOUT seconds (240).
# Run names carry a random tag, so several checks can run side by side.
# SCENARIOS="b c" runs a subset.
set -u
H=${1:?usage: live_check.sh claude|opencode|codex}
case $H in claude|opencode|codex) ;; *) echo "unknown harness: $H" >&2; exit 2 ;; esac
REPO=$(cd "$(dirname "$0")/.." && pwd)
[ -x "$REPO/.venv/bin/labctl" ] || (cd "$REPO" && uv sync -q) || exit 2
PY=$REPO/.venv/bin/python
CAMPAIGN=$REPO/tests/sim/fake_campaign.py
CLAUDE_MODEL=${CLAUDE_MODEL:-sonnet}
OPENCODE_MODEL=${OPENCODE_MODEL:-opencode-go/mimo-v2.6-flash}
TIMEOUT=${LIVE_TIMEOUT:-240}
unset CLAUDECODE  # the manager session below is a top-level session, not a nested one

WORK=$(mktemp -d "${TMPDIR:-/tmp}/labctl-live.XXXXXX")
mkdir -p "$WORK/bin" "$WORK/project"
ln -s "$REPO/.venv/bin/labctl" "$WORK/bin/labctl"  # agents call `labctl`
PATH=$WORK/bin:$PATH
LABCTL_CONFIG=$WORK/config.toml
LABCTL_HARNESS=$H
unset LABCTL_RUNS_DIR  # realistic: runs/ inside the project, the default
export PATH LABCTL_CONFIG LABCTL_HARNESS
RUNS=$WORK/project/runs
TAG=$("$PY" -c 'import secrets; print(secrets.token_hex(2))')
A=$TAG-a-success B=$TAG-b-fail CN=$TAG-c-nan-fail D=$TAG-d-stall E=$TAG-e-cancel F=$TAG-f-manager
G=$TAG-g-checkin HV=$TAG-h-verified
cat > "$LABCTL_CONFIG" <<EOF
[claude]
frontier = "$CLAUDE_MODEL"
strong = "$CLAUDE_MODEL"
cheap = "$CLAUDE_MODEL"

[opencode]
frontier = "$OPENCODE_MODEL"
strong = "$OPENCODE_MODEL"
cheap = "$OPENCODE_MODEL"
EOF
cd "$WORK/project" || exit 2
cat > brief.md <<'EOF'
Goal: a four-stage test campaign (clean, dependence, swap, replay) that writes out/<stage>/completion.json.
Normal: progress lines "<stage> i/N key=... loss=..." every fraction of a second.
Allowed without asking: nothing. Do not relaunch or cancel runs.
On a loss=nan line or a stall: look at the log, say in one line what you saw, and end your turn.
Escalate when: the run fails. Run `labctl escalate ID -m "..."` with the error line, then end your turn.
EOF
cat > brief-g.md <<'EOF'
Goal: a four-stage test campaign (clean, dependence, swap, replay). Each stage writes out/g/<stage>/completion.json when it ends.
Normal: progress lines "<stage> i/N key=... loss=..." every fraction of a second; the run takes about 40 s.
Check-ins:
- At 0.25 min: stage "clean" has ended, so out/g/clean/completion.json exists and contains "complete": true.
On success: out/g/<stage>/completion.json exists for all four stages with "complete": true.
Allowed without asking: cancel the run when a check-in fails.
Escalate when: anything else looks wrong.
EOF
cat > brief-h.md <<'EOF'
Goal: a four-stage test campaign (clean, dependence, swap, replay) that writes out/h/<stage>/completion.json.
Normal: progress lines "<stage> i/N key=... loss=..." every fraction of a second; the run takes about 4 s.
On success: out/h/<stage>/completion.json exists for each of the four stages and contains "complete": true. Name the files you checked in your verdict.
Allowed without asking: nothing.
Escalate when: an output is missing or wrong.
EOF
echo "labctl live check: harness=$H work=$WORK tag=$TAG"

FAILED=0
result() {  # NAME STATUS(0=pass) EVIDENCE
    if [ "$2" -eq 0 ]; then echo "PASS  $1  ($3)"; else echo "FAIL  $1  ($3)"; FAILED=$((FAILED + 1)); fi
}
wait_for() {  # SECONDS COMMAND...: until COMMAND succeeds
    end=$(( $(date +%s) + $1 )); shift
    until "$@" >/dev/null 2>&1; do
        [ "$(date +%s)" -ge "$end" ] && return 1
        sleep 2
    done
}
state() { labctl status "$1" 2>/dev/null | sed -n 's/^state: *//p'; }
state_is() { [ "$(state "$1")" = "$2" ]; }
count() { n=$(grep -c "$1" "$2" 2>/dev/null); echo "${n:-0}"; }  # PATTERN FILE
turns() { count "^== .* role=$2\$" "$RUNS/$1/wake.log"; }
ok_turns() { count '^exit 0 ' "$RUNS/$1/wake.log"; }
idle() { ! labctl sessions "$1" | grep -q busy=yes; }
two_turns_done() { [ "$(ok_turns "$1")" -ge 2 ] && idle "$1"; }
one_turn_done() { [ "$(ok_turns "$1")" -ge 1 ]; }
inbox_has() {  # RUN REGEX
    "$PY" - "$RUNS/inbox.jsonl" "$1" "$2" <<'EOF'
import json, re, sys
path, run, pattern = sys.argv[1:]
try:
    entries = [json.loads(line) for line in open(path)]
except OSError:
    entries = []
sys.exit(0 if any(e.get("run") == run and re.search(pattern, e.get("text", "")) for e in entries) else 1)
EOF
}
inbox_count() {  # RUN: number of inbox entries of the run
    "$PY" -c 'import json,sys; print(sum(json.loads(l).get("run") == sys.argv[2] for l in open(sys.argv[1])))' \
        "$RUNS/inbox.jsonl" "$1" 2>/dev/null || echo 0
}
session_of() {  # RUN ROLE -> session id
    "$PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], {}).get("session_id") or "")' \
        "$RUNS/$1/sessions.json" "$2" 2>/dev/null
}
json_field() {  # FILE KEY: first JSON object (whole output or per line) with KEY
    "$PY" - "$1" "$2" <<'EOF'
import json, sys
text = open(sys.argv[1]).read()
for chunk in [text, *text.splitlines()]:
    try:
        obj = json.loads(chunk)
    except ValueError:
        continue
    if isinstance(obj, dict) and obj.get(sys.argv[2]):
        print(obj[sys.argv[2]])
        break
EOF
}

want() { case " ${SCENARIOS:-a b c d e f g h} " in *" $1 "*) return 0 ;; esac; return 1; }

# --- a headless manager session for (f) -------------------------------------
MSID=
if want f; then
    HELLO="You are a test manager session for labctl. Reply with OK only."
    case $H in
    claude)
        MSID=$("$PY" -c 'import uuid; print(uuid.uuid4())')
        echo "$HELLO" | claude -p --session-id "$MSID" --model "$CLAUDE_MODEL" --output-format json \
            --permission-mode dontAsk --allowedTools "Bash(labctl:*)" Read > "$WORK/manager-start.json" 2>&1 \
            || MSID= ;;
    opencode)
        echo "$HELLO" | opencode run --format json -m "$OPENCODE_MODEL" > "$WORK/manager-start.json" 2>&1
        MSID=$(json_field "$WORK/manager-start.json" sessionID) ;;
    codex)
        echo "$HELLO" | codex exec --json --skip-git-repo-check - > "$WORK/manager-start.json" 2>&1
        MSID=$(json_field "$WORK/manager-start.json" thread_id) ;;
    esac
    [ -n "$MSID" ] || echo "note: no manager session id; see $WORK/manager-start.json"
fi

# --- launch ------------------------------------------------------------------
launch() {  # abort at once if a run cannot be started
    labctl run "$@" || { echo "ABORT: labctl run failed: $*" >&2; exit 1; }
}
C="$PY $CAMPAIGN"
want a && launch --name "$A" --check "$C check --out out/a" -- $C run --out out/a --duration 4
want b && launch --name "$B" --brief brief.md -- $C run --out out/b --duration 4 --fail-at swap
want c && launch --name "$CN" --brief brief.md --wake-on "loss=nan" -- \
    $C run --out out/c --duration 40 --nan-at clean --fail-at replay
want d && launch --name "$D" --brief brief.md --stall 0.1 -- \
    $C run --out out/d --duration 4 --stall-at dependence 20
want e && launch --name "$E" -- $C run --out out/e --duration 300
want g && launch --name "$G" --brief brief-g.md --checkin 0.25 -- \
    $C run --out out/g --duration 40 --bad-output-at clean
want h && launch --name "$HV" --brief brief-h.md -- $C run --out out/h --duration 4
want f && [ -n "$MSID" ] && launch --name "$F" --manager "$H:$MSID" -- $C run --out out/f --duration 3
if want e; then sleep 3; labctl cancel "$E"; fi

# --- check -------------------------------------------------------------------
if want a; then
    wait_for 60 state_is "$A" succeeded
    inbox_has "$A" 'event=exit code=0 check=passed'
    result "a clean success -> inbox" $? "$RUNS/inbox.jsonl"
fi
if want b; then  # the escalation arrives during the turn: wait for the turn to end too
    wait_for "$TIMEOUT" inbox_has "$B" 'event=(escalation|report)'
    wait_for 120 one_turn_done "$B"
    wait_for 60 idle "$B"
    inbox_has "$B" 'event=(escalation|report)' && state_is "$B" failed && [ -n "$(session_of "$B" experimenter)" ]
    result "b failure -> experimenter -> escalation/report in inbox" $? "$RUNS/$B/wake.log"
fi
if want c; then
    wait_for "$TIMEOUT" two_turns_done "$CN"
    SID_C=$(session_of "$CN" experimenter)
    RESUMED=$(grep '^\$ ' "$RUNS/$CN/wake.log" | sed -n '2,$p' | grep -c -F -- "$SID_C")
    [ -n "$SID_C" ] && [ "$(ok_turns "$CN")" -ge 2 ] && [ "$RESUMED" -ge 1 ] && state_is "$CN" failed
    result "c match then failure -> session $SID_C resumed, $(turns "$CN" experimenter) turns" $? "$RUNS/$CN/wake.log"
fi
if want d; then
    wait_for "$TIMEOUT" one_turn_done "$D"
    grep -q '"event": "stall"' "$RUNS/$D/events.jsonl" && one_turn_done "$D"
    result "d stall -> experimenter woken" $? "$RUNS/$D/wake.log"
fi
if want e; then
    wait_for 30 state_is "$E" cancelled
    state_is "$E" cancelled && inbox_has "$E" 'cancelled=true'
    result "e cancel -> cancelled, in inbox" $? "$RUNS/$E/events.jsonl"
fi
if want f && [ -n "$MSID" ]; then
    wait_for "$TIMEOUT" one_turn_done "$F"
    ASK="Quote verbatim the most recent line you received that starts with [labctl wake]. Reply with that line only."
    case $H in
    claude) echo "$ASK" | claude -p --resume "$MSID" --output-format json > "$WORK/manager-ask.json" 2>&1 ;;
    opencode) echo "$ASK" | opencode run --format json -s "$MSID" > "$WORK/manager-ask.json" 2>&1 ;;
    codex) echo "$ASK" | codex exec resume --json --skip-git-repo-check "$MSID" - > "$WORK/manager-ask.json" 2>&1 ;;
    esac
    grep -q "run=$F" "$WORK/manager-ask.json" && grep -q 'event=exit' "$WORK/manager-ask.json"
    result "f unattended manager ($MSID) received the wake" $? "$WORK/manager-ask.json"
elif want f; then
    result "f unattended manager: could not start a manager session" 1 "$WORK/manager-start.json"
fi

if want g; then
    wait_for "$TIMEOUT" inbox_has "$G" 'event=(fix_request|escalation)'
    inbox_has "$G" 'event=(fix_request|escalation)' && grep -q '"event": "checkin"' "$RUNS/$G/events.jsonl"
    result "g check-in finds wrong output -> fix_request/escalation in inbox" $? "$RUNS/$G/wake.log"
fi
if want h; then
    wait_for "$TIMEOUT" inbox_has "$HV" 'event=report'
    sleep 5  # a second message would arrive right after the first
    inbox_has "$HV" 'event=report code=0 check=none' && inbox_has "$HV" 'completion\.json' \
        && [ "$(inbox_count "$HV")" -eq 1 ]
    result "h success -> experimenter verifies -> one report in inbox" $? "$RUNS/inbox.jsonl"
fi

echo "labctl status:"
labctl status
echo "$FAILED scenario(s) failed; evidence in $WORK"
[ "$FAILED" -eq 0 ]
