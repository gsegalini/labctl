"""Command line: labctl run | status | tail | wait | cancel | escalate | report | sessions | attach | install."""

import argparse
import datetime
import math
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

from labctl import agents, runs, supervisor, wake
from labctl.install import HARNESSES

SUPERVISOR_START_TIMEOUT = 10.0  # seconds


def _run_dir(run_id: str) -> Path:
    d = runs.runs_dir() / run_id
    if not (d / "run.json").exists():
        sys.exit(f"labctl: no run {run_id!r} in {runs.runs_dir()}")
    return d


def cmd_run(args) -> int:
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        sys.exit("labctl run: missing command (use: labctl run [options] -- <command...>)")
    for p in args.wake_on:
        try:
            re.compile(p)
        except re.error as e:
            sys.exit(f"labctl run: bad --wake-on regex {p!r}: {e}")
    if any(not math.isfinite(m) or m < 0 for m in args.checkin):
        sys.exit("labctl run: --checkin MINUTES must be a number >= 0")
    if args.stall is not None and not (math.isfinite(args.stall) and args.stall > 0):
        sys.exit("labctl run: --stall MINUTES must be a number > 0")
    if args.slot is not None and not runs.NAME_RE.fullmatch(args.slot):
        sys.exit(f"labctl run: invalid --slot {args.slot!r}: use letters, digits, '_', '-', '.'")
    harness = args.harness or os.environ.get("LABCTL_HARNESS")
    if args.brief and harness not in HARNESSES:
        sys.exit(f"labctl run: --brief starts an experimenter, so it needs --harness or $LABCTL_HARNESS "
                 f"(one of {', '.join(HARNESSES)}; got {harness!r})")
    if args.name and _tmux_has(runs.tmux_session(args.name)):
        sys.exit(f"labctl run: tmux session {runs.tmux_session(args.name)!r} already exists; choose another --name")
    try:
        sessions = agents.new_sessions(harness if args.brief else None, args.manager)
        run_dir = runs.create_run(runs.runs_dir(), command, cwd=Path.cwd(), name=args.name,
                                  slot=args.slot, wake_on=args.wake_on, stall=args.stall, checkin=args.checkin,
                                  check=args.check, brief=args.brief, env=os.environ, sessions=sessions)
    except FileExistsError:
        sys.exit(f"labctl run: run {args.name!r} already exists")
    except (ValueError, OSError) as e:
        sys.exit(f"labctl run: {e}")
    # supervisor stdout: the pane; stderr (crashes): supervisor.log
    sup = shlex.join([*runs.LABCTL, "_supervise", str(run_dir)]) + " 2>>" + shlex.quote(str(run_dir / "supervisor.log"))
    session = runs.tmux_session(run_dir.name)
    try:
        r = subprocess.run(["tmux", "new-session", "-d", "-s", session, "-c", str(Path.cwd()), sup],
                           capture_output=True, text=True)
        error = r.stderr.strip() if r.returncode else None
    except FileNotFoundError:
        error = "tmux is not installed"
    # The supervisor deletes .env.json first thing: that is the sign that it started.
    deadline = time.monotonic() + SUPERVISOR_START_TIMEOUT
    while not error and (run_dir / ".env.json").exists():
        if time.monotonic() > deadline:
            subprocess.run(["tmux", "kill-session", "-t", f"={session}"], capture_output=True)
            log = runs.tail(run_dir / "supervisor.log", 5)
            error = f"supervisor did not start within {SUPERVISOR_START_TIMEOUT:g} s" + "".join("\n  " + x for x in log)
        time.sleep(0.05)
    if error:
        (run_dir / ".env.json").unlink(missing_ok=True)
        runs.write_json(run_dir / "status.json", {"state": "failed", "error": error})
        sys.exit(f"labctl run: {error}")
    print(run_dir.name)
    return 0


def _tmux_has(session: str) -> bool:
    try:
        return subprocess.run(["tmux", "has-session", "-t", f"={session}"], capture_output=True).returncode == 0
    except FileNotFoundError:
        return False


def _age(created: str | None) -> str:
    if not created:
        return "?"
    try:
        t = datetime.datetime.fromisoformat(created.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return "?"
    s = (datetime.datetime.now(datetime.timezone.utc) - t).total_seconds()
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= size:
            return f"{int(s // size)}{unit}"
    return f"{int(s)}s"


def _table(rows: list[list[str]], indent: str = "") -> None:
    """Left-aligned columns, the last one unpadded."""
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]) - 1)]
    for r in rows:
        print(indent + "  ".join([c.ljust(w) for c, w in zip(r, widths)] + [r[-1]]).rstrip())


def _what(event: dict) -> str:
    """One line: 'exit code=1 check=none', or 'escalation: <first message line>'."""
    text = wake.summary(event).removeprefix("event=")
    lines = str(event.get("message", "")).splitlines()
    if lines:
        text += ": " + lines[0] + (" ..." if len(lines) > 1 else "")
    return text if len(text) <= 120 else text[:117] + "..."


def cmd_status(args) -> int:
    if args.id is None:
        root = runs.runs_dir()
        dirs = [d for d in root.iterdir() if (d / "run.json").exists()] if root.is_dir() else []
        if not dirs:
            print(f"no runs in {root}")
            return 0
        metas = {d: runs.read_json(d / "run.json", {}) for d in dirs}
        dirs.sort(key=lambda d: (str(metas[d].get("created", "")), d.name))
        rows = [["RUN", "STATE", "AGE", "LAST EVENT"]]
        for d in dirs:
            events = runs.read_events(d)
            own = [e for e in events if e.get("source") == "supervisor"]  # the run's own outcome
            last = _what(own[-1]) if own else "-"
            failures = sum(e.get("event") == "delivery_failed" for e in events)
            if failures:
                last += f"  [{failures} delivery failure{'s' * (failures > 1)}]"
            rows.append([d.name, runs.state(d), _age(metas[d].get("created")), last])
        print(f"runs in {root}\n")
        _table(rows)
        entries = runs.read_jsonl(wake.inbox_path(root))
        if entries:
            shown = f"last 5 of {len(entries)}, " if len(entries) > 5 else ""
            print(f"\ninbox ({shown}full text in {wake.inbox_path(root)}):")
            rows = []
            for e in entries[-5:]:
                ev = next((x for x in runs.read_events(root / str(e.get("run")))
                           if x.get("seq") == e.get("seq")), None)
                what = _what(ev) if ev else str(e.get("text", "")).partition("\n")[0]
                rows.append([f"#{e.get('n')}", _age(e.get("time")) + " ago", str(e.get("run")), what])
            _table(rows, "  ")
        return 0
    d = _run_dir(args.id)
    meta = runs.read_json(d / "run.json", {})
    print(f"run:     {d.name}")
    print(f"state:   {runs.state(d)}")
    print(f"command: {shlex.join(meta.get('command', []))}")
    print(f"cwd:     {meta.get('cwd')}")
    print(f"tmux:    {meta.get('tmux')}")
    st = runs.read_json(d / "status.json", {})
    pids = [f"{name} {st[key]} ({'alive' if runs.pid_alive(st[key]) else 'not running'})"
            for name, key in (("supervisor", "supervisor_pid"), ("command", "child_pid"), ("check", "check_pid"))
            if st.get(key)]
    if pids:
        print(f"pids:    {', '.join(pids)}")
    events = runs.read_events(d)
    print(f"events:  {len(events)}" + (", last 5:" if len(events) > 5 else ""))
    if events:
        _table([[f"#{e.get('seq')}", _age(e.get("time")) + " ago", str(e.get("source")), _what(e)]
                for e in events[-5:]], "  ")
    print("log (last 10 lines):")
    for line in runs.tail(d / "log", 10):
        print("  " + wake.cap_line(line))
    return 0


def cmd_tail(args) -> int:
    for line in runs.tail(_run_dir(args.id) / "log", args.n):  # long lines capped: the whole log is in runs/<id>/log
        print(wake.cap_line(line))
    return 0


def cmd_wait(args) -> int:
    if args.id is None:
        return _wait_inbox(args)
    d = _run_dir(args.id)
    after = args.after
    if after is None:
        after = max((e["seq"] for e in runs.read_events(d)), default=0)
    while True:
        s = runs.state(d)  # read before events: the exit event is written before the final state
        events = runs.read_events(d)
        new = [e for e in events if e["seq"] > after]
        if new:
            print(wake.format_wake(d, new[0]))
            return 0
        if s in runs.FINAL or s.startswith("unknown"):  # nothing newer can arrive
            exits = [e for e in events if e["event"] == "exit"]
            if not exits:
                sys.exit(f"labctl wait: run {d.name} is {s} and has no exit event")
            print(wake.format_wake(d, exits[-1]))
            return 0
        time.sleep(args.poll)


def _wait_inbox(args) -> int:
    """Block until the next manager-routed wake of any run, then print it."""
    path = wake.inbox_path(runs.runs_dir())
    after = args.after
    if after is None:
        after = max((e.get("n", 0) for e in runs.read_jsonl(path)), default=0)
    while True:
        new = [e for e in runs.read_jsonl(path) if isinstance(e.get("n"), int) and e["n"] > after]
        if new:
            print(min(new, key=lambda e: e["n"])["text"])
            return 0
        time.sleep(args.poll)


def cmd_cancel(args) -> int:
    print(supervisor.cancel(_run_dir(args.id)))
    return 0


def cmd_escalate(args) -> int:
    d = _run_dir(args.id)
    ev = wake.emit(d, "fix_request" if args.fix else "escalation", source="experimenter", message=args.m)
    print(f"{ev['event']} for {d.name} seq={ev['seq']}")
    return 0


def cmd_report(args) -> int:
    d = _run_dir(args.id)
    exits = [e for e in runs.read_events(d) if e["event"] == "exit"]
    if not exits:
        sys.exit(f"labctl report: run {d.name} has not exited; a report is the verdict on a finished run")
    ev = wake.emit(d, "report", source="experimenter", code=exits[-1].get("code"), check=exits[-1].get("check"),
                   message=args.m)
    print(f"report for {d.name} seq={ev['seq']}")
    return 0


def cmd_sessions(args) -> int:
    root = runs.runs_dir()
    if args.id:
        dirs = [_run_dir(args.id)]
    else:
        dirs = sorted(d for d in root.iterdir() if (d / "run.json").exists()) if root.is_dir() else []
    for d in dirs:
        for role, rec in agents.load(d).items():
            started = rec.get("started", True) and bool(rec.get("session_id"))  # manager: always
            busy = agents.busy(agents.lock_path(d, role, rec))
            print(f"{d.name:<24} {role:<12} {rec.get('harness', '?'):<8} {rec.get('session_id') or '-':<36} "
                  f"started={'yes' if started else 'no'} busy={'yes' if busy else 'no'}")
    return 0


def cmd_attach(args) -> int:
    return agents.attach(_run_dir(args.id), args.role)


def cmd_install(args) -> int:
    from labctl.install import install

    for path in install(args.harness, Path(args.project).resolve()):
        print(path)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="labctl", description="Run experiments; wake agents on events.")
    sub = p.add_subparsers(dest="cmd", required=True,
                           metavar="{run,status,tail,wait,cancel,escalate,report,sessions,attach,install}")

    r = sub.add_parser("run", help="start a command under the supervisor in tmux")
    r.add_argument("--name", help="run id (default: timestamp + random hex)")
    r.add_argument("--slot", help="run exclusively in this slot (queue behind other runs)")
    r.add_argument("--wake-on", action="append", default=[], metavar="REGEX",
                   help="record a match event for log lines matching REGEX (repeatable)")
    r.add_argument("--stall", type=float, metavar="MINUTES", help="record a stall event after this long without output")
    r.add_argument("--checkin", action="append", type=float, default=[], metavar="MINUTES",
                   help="record a checkin event this long after the command starts, if still running (repeatable)")
    r.add_argument("--check", metavar="CMD", help="shell command run after exit 0; its exit code decides ok")
    r.add_argument("--brief", metavar="FILE", help="gives the run an experimenter agent; copied to brief.md")
    r.add_argument("--harness", choices=HARNESSES, help="experimenter harness (default: $LABCTL_HARNESS)")
    r.add_argument("--manager", metavar="HARNESS:SESSION_ID",
                   help="resume this session headlessly with manager wakes (default: inbox only)")
    r.add_argument("command", nargs=argparse.REMAINDER, help="-- <command...>")
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("status", help="list runs, or show one run")
    s.add_argument("id", nargs="?")
    s.set_defaults(func=cmd_status)

    t = sub.add_parser("tail", help="print the last lines of a run's log")
    t.add_argument("id")
    t.add_argument("-n", type=int, default=40)
    t.set_defaults(func=cmd_tail)

    w = sub.add_parser("wait", help="block until the run's next event (no ID: the next inbox entry), print it")
    w.add_argument("id", nargs="?")
    w.add_argument("--after", type=int, metavar="N",
                   help="wait for an event with seq > N (no ID: inbox entry n > N); default: the latest")
    w.add_argument("--poll", type=float, default=0.5, help=argparse.SUPPRESS)
    w.set_defaults(func=cmd_wait)

    c = sub.add_parser("cancel", help="kill the run's process group")
    c.add_argument("id")
    c.set_defaults(func=cmd_cancel)

    e = sub.add_parser("escalate", help="experimenter -> manager: record an escalation")
    e.add_argument("id")
    e.add_argument("--fix", action="store_true", help="a fix request: a bug in the experiment's code or config")
    e.add_argument("-m", required=True, metavar="TEXT")
    e.set_defaults(func=cmd_escalate)

    rp = sub.add_parser("report", help="experimenter -> manager: the verdict on a finished run")
    rp.add_argument("id")
    rp.add_argument("-m", required=True, metavar="TEXT")
    rp.set_defaults(func=cmd_report)

    se = sub.add_parser("sessions", help="agent sessions of all runs or one run")
    se.add_argument("id", nargs="?")
    se.set_defaults(func=cmd_sessions)

    a = sub.add_parser("attach", help="open an agent's conversation (waits for its current turn)")
    a.add_argument("id")
    a.add_argument("role", nargs="?", default="experimenter", choices=("experimenter", "manager"))
    a.set_defaults(func=cmd_attach)

    i = sub.add_parser("install", help="wire labctl into an agent harness")
    i.add_argument("harness")
    i.add_argument("--project", default=".", metavar="DIR")
    i.set_defaults(func=cmd_install)

    sv = sub.add_parser("_supervise")
    sv.add_argument("run_dir")
    sv.set_defaults(func=lambda a: supervisor.supervise(Path(a.run_dir)))

    dv = sub.add_parser("_deliver")
    dv.add_argument("run_dir")
    dv.add_argument("seq", type=int)
    dv.add_argument("--pending-fd", type=int)
    dv.set_defaults(func=lambda a: agents.deliver_turn(Path(a.run_dir), a.seq, a.pending_fd) or 0)

    args = p.parse_args(argv)
    return args.func(args)
