"""Fake claude / codex / opencode for tests: `fake_harness.py NAME ARGS...`.

Parses its arguments the way the real CLI does (claude: `-p` is a flag, the
prompt is the first positional or else stdin, `--allowedTools` is variadic and
swallows following words, an unknown `-x` is an error; codex: `-` reads the
prompt from stdin; opencode run: stdin is appended to the message), prints the
JSON the real one prints, and appends one record per call to $FAKE_CALLS:
argv, the prompt, cwd, start/end times. Sessions it created are kept in
$FAKE_CALLS.sessions; resuming any other id fails like the real CLI does.

Environment knobs:
  FAKE_MODE   ok | auth | ratelimit | hang | garbage | nosid | busy (codex resume)
              | create_fail (create the session, then fail)
  FAKE_ONCE   if set, FAKE_MODE applies to the first call only (marker file $FAKE_CALLS.once)
  FAKE_SLEEP  seconds to sleep inside every turn
  FAKE_REPLY  the agent's final reply (default "all good")
  FAKE_ESCALATE_RUN / FAKE_ESCALATE  run `labctl escalate RUN -m TEXT` during the turn
  FAKE_FIX    if set, that escalation is a fix request (`--fix`)
  FAKE_ATTACH_SLEEP  seconds an interactive (attach) session stays open
"""

import json
import os
import subprocess
import sys
import time

CLAUDE_VALUE = {"--session-id", "--model", "--output-format", "--append-system-prompt",
                "--permission-mode", "--resume", "-r"}
CLAUDE_VARIADIC = {"--allowedTools", "--disallowedTools"}
CLAUDE_FLAGS = {"-p", "--print"}


def die(msg: str, code: int = 1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def parse_claude(args):
    opts, positional, i = {}, [], 0
    while i < len(args):
        a = args[i]
        if a in CLAUDE_FLAGS:
            opts[a], i = True, i + 1
        elif a in CLAUDE_VALUE:
            if i + 1 >= len(args):
                die(f"error: option '{a}' argument missing")
            opts[a], i = args[i + 1], i + 2
        elif a in CLAUDE_VARIADIC:
            i += 1
            values = []
            while i < len(args) and not args[i].startswith("-"):
                values.append(args[i])
                i += 1
            opts[a] = values
        elif a.startswith("-"):
            die(f"error: unknown option '{a.splitlines()[0]}'")
        else:
            positional.append(a)
            i += 1
    return opts, positional


def known_sessions(calls):
    try:
        return open(calls + ".sessions").read().split()
    except FileNotFoundError:
        return []


RECORD = {}


def main():
    name, args = sys.argv[1], sys.argv[2:]
    calls = os.environ["FAKE_CALLS"]
    RECORD.update(argv=[name, *args], prompt=None, cwd=os.getcwd(), runs_dir=os.environ.get("LABCTL_RUNS_DIR"),
                  start=time.time())
    mode = os.environ.get("FAKE_MODE", "ok")
    if os.environ.get("FAKE_ONCE"):
        try:
            os.close(os.open(calls + ".once", os.O_CREAT | os.O_EXCL))
        except FileExistsError:
            mode = "ok"
    reply = os.environ.get("FAKE_REPLY", "all good")
    RECORD["mode"] = mode

    interactive, prompt, sid, resuming = False, None, None, False
    if name == "claude":
        opts, positional = parse_claude(args)
        interactive = "-p" not in opts
        if not interactive:
            prompt = positional[0] if positional else sys.stdin.read()
            if not prompt:
                die("Error: Input must be provided either through stdin or as a prompt argument when using --print")
        sid = opts.get("--session-id") or opts.get("--resume")
        resuming = "--resume" in opts
    elif name == "codex":
        interactive = args[:1] == ["resume"]
        if args[:1] == ["exec"]:
            if args[-1].startswith("-") and args[-1] != "-":
                die(f"error: unexpected argument '{args[-1]}' found")
            prompt = sys.stdin.read() if args[-1] == "-" else args[-1]
            resuming = args[1] == "resume"
            sid = args[-2] if resuming else "thread-fake"
        elif args[:1] == ["queue"]:
            prompt = next(a.split("=", 1)[1] for a in args if a.startswith("--message="))
    elif name == "opencode":
        interactive = args[:1] != ["run"]
        if not interactive:
            resuming = "-s" in args
            sid = args[args.index("-s") + 1] if resuming else "ses_fake"
            prompt = sys.stdin.read()
            if not prompt.strip():
                die("You must provide a message")

    RECORD["prompt"] = prompt
    if interactive:
        time.sleep(float(os.environ.get("FAKE_ATTACH_SLEEP", "0")))
    else:
        time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
        if mode == "hang":
            time.sleep(3600)
        if os.environ.get("FAKE_ESCALATE"):
            fix = ["--fix"] if os.environ.get("FAKE_FIX") else []
            subprocess.run([sys.executable, "-P", "-m", "labctl", "escalate", os.environ["FAKE_ESCALATE_RUN"], *fix,
                            "-m", os.environ["FAKE_ESCALATE"]], check=True, stdout=subprocess.DEVNULL)
    if interactive:
        return
    if resuming and sid not in known_sessions(calls) and mode != "busy":
        die(f"No conversation found with session ID: {sid}")
    if not resuming and mode in ("ok", "create_fail", "busy", "garbage", "ratelimit") and sid:
        with open(calls + ".sessions", "a") as f:
            f.write(sid + "\n")
    if mode == "create_fail":
        if name == "codex":
            print(json.dumps({"type": "thread.started", "thread_id": sid}))
        die("API Error: 500 internal server error")
    if mode == "auth":
        die("Invalid API key · Please run /login")
    if mode == "garbage":
        print("Segmentation fault (core dumped)?! <html>not json</html>")
        return
    if mode == "busy" and name == "codex" and args[:2] == ["exec", "resume"]:
        die("error: thread thread-x already has an active writer")

    if name == "claude":
        if mode == "ratelimit":  # reported in the JSON, exit code 0
            print(json.dumps({"type": "result", "is_error": True, "session_id": sid,
                              "result": "Claude AI usage limit reached|1760000000"}))
            return
        out = {"type": "result", "subtype": "success", "is_error": False, "result": reply}
        if mode != "nosid":
            out["session_id"] = sid
        print(json.dumps(out))
    elif name == "codex" and args[:1] == ["exec"]:
        if mode == "ratelimit":
            print(json.dumps({"type": "turn.failed", "error": {"message": "rate limit exceeded"}}))
            sys.exit(1)
        if mode != "nosid":
            print(json.dumps({"type": "thread.started", "thread_id": sid}))
        print(json.dumps({"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": reply}}))
        print(json.dumps({"type": "turn.completed", "usage": {}}))
    elif name == "opencode":
        if mode == "ratelimit":
            print(json.dumps({"type": "error", "sessionID": sid, "error": {"name": "APIError", "data": {"message": "rate limit"}}}))
            return
        base = {} if mode == "nosid" else {"sessionID": sid}
        print(json.dumps({"type": "step_start", **base, "part": {"type": "step-start"}}))
        print(json.dumps({"type": "text", **base, "part": {"type": "text", "text": reply}}))


if __name__ == "__main__":
    code = 0
    try:
        main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else int(bool(e.code))
        raise
    finally:  # one record per call, with its exit code (a killed call leaves none)
        if RECORD:
            with open(os.environ["FAKE_CALLS"], "a") as f:
                f.write(json.dumps({**RECORD, "code": code, "end": time.time()}) + "\n")
