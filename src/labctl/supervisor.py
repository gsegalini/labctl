"""The supervisor: runs one command, writes its log, status and events.

Also `cancel`, which is the only other code that signals processes.
"""

import fcntl
import os
import re
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

from labctl import runs
from labctl.wake import emit

MAX_PENDING = 65536  # bytes of an unterminated line kept in memory
DRAIN_SECONDS = 1.0  # output still read after the main process exits
EXIT_POLL_SECONDS = 0.5  # how often a silent run is checked for having exited
ORPHAN_POLL_SECONDS = 1.0  # how often a slot checks whether a dead supervisor's command has exited


class Cancelled(Exception):
    pass


def supervise(run_dir: Path) -> int:
    run_dir = Path(run_dir).resolve()
    caller_env = runs.read_json(run_dir / ".env.json")  # may hold secrets: gone before anything else
    (run_dir / ".env.json").unlink(missing_ok=True)
    status_path, cancel_path = run_dir / "status.json", run_dir / "cancel"
    if runs.read_json(status_path, {}).get("state") in runs.FINAL:
        return 0  # cancelled before we even started
    meta = runs.read_json(run_dir / "run.json")
    try:  # a stopped pane (Ctrl-S) must never block the run: echo drops lines instead
        os.set_blocking(sys.stdout.fileno(), False)
    except (OSError, ValueError):
        pass
    if caller_env is not None:  # adopt it, so wake deliveries see it too
        os.environ.clear()
        os.environ.update(caller_env)
    os.environ.update(LABCTL_RUN_DIR=str(run_dir), LABCTL_RUN_ID=meta["id"],
                      LABCTL_RUNS_DIR=str(run_dir.parent))
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}

    status = {"state": "queued" if meta["slot"] else "running", "supervisor_pid": os.getpid(),
              "child_pid": None, "started": None, "finished": None}
    runs.write_json(status_path, status)
    proc, waiting = None, False
    active = [None]  # process group of what runs now: the command, then the check
    got_signal = []

    def on_signal(signum, frame):
        # SIGTERM (labctl cancel), SIGHUP (tmux session killed), SIGINT (Ctrl-C in the pane)
        got_signal.append(signal.Signals(signum).name)
        cancel_path.touch()
        if waiting:
            raise Cancelled
        if active[0] is not None:
            _killpg(active[0], signal.SIGTERM)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, on_signal)

    code, lock = None, None
    log = open(run_dir / "log", "ab")
    try:
        if meta["slot"]:
            slots = run_dir.parent / ".slots"
            slots.mkdir(exist_ok=True)
            lock = open(slots / f"{meta['slot']}.lock", "a+")  # holds the last holder's run dir
            waiting = True
            fcntl.flock(lock, fcntl.LOCK_EX)  # held until we exit
            lock.seek(0)
            prev = lock.read().strip()
            _wait_for_orphan(Path(prev) if prev else None, meta["slot"], log)
            waiting = False
            lock.truncate(0)
            lock.write(f"{run_dir}\n")
            lock.flush()
        if cancel_path.exists():
            raise Cancelled
        status.update(state="running", started=runs.now())
        try:
            proc = subprocess.Popen(meta["command"], cwd=meta["cwd"], env=env,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as e:
            _write_line(log, f"[labctl] could not start command: {e}".encode())
            code = 127
        else:
            active[0] = proc.pid
            if cancel_path.exists():  # cancel arrived between the check and Popen
                _killpg(proc.pid, signal.SIGTERM)
            status["child_pid"] = proc.pid
            runs.write_json(status_path, status)
            _stream(proc, log, meta, run_dir)
            rc = proc.wait()
            code = rc if rc >= 0 else 128 - rc
    except Cancelled:
        waiting = False

    check = None
    if code is not None:
        (run_dir / "exit-code").write_text(f"{code}\n")
        if code == 0 and meta["check"] and not cancel_path.exists():
            _write_line(log, f"[labctl] check: {meta['check']}".encode())
            p = subprocess.Popen(meta["check"], shell=True, cwd=meta["cwd"], env=env, stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            active[0] = p.pid
            if cancel_path.exists():  # cancel arrived while the check was starting
                _killpg(p.pid, signal.SIGTERM)
            status["check_pid"] = p.pid  # so that `cancel` can kill a check that ignores SIGTERM
            runs.write_json(status_path, status)
            status["check_code"] = p.wait()
            check = "passed" if p.returncode == 0 else "failed"
    log.close()
    cancelled = cancel_path.exists()
    if cancelled and check is not None:
        check = None  # cancelled during the check: it has no verdict
    ok = code == 0 and check != "failed" and not cancelled
    extra = {"cancelled": True} if cancelled else {}
    if got_signal:
        extra["signal"] = got_signal[0]
    emit(run_dir, "exit", code=code, check=check, ok=ok, **extra)
    status.update(state="cancelled" if cancelled else "succeeded" if ok else "failed",
                  finished=runs.now())
    runs.write_json(status_path, status)
    if lock:
        lock.close()
    return 0


def _orphaned_groups(prev: Path) -> list[int]:
    """Process groups of a run whose supervisor died while they still run."""
    st = runs.read_json(prev / "status.json", {})
    if st.get("state") in runs.FINAL:
        return []
    return [g for g in (st.get("child_pid"), st.get("check_pid")) if g and _killpg(g, 0)]


def _wait_for_orphan(prev: Path | None, slot: str, log) -> None:
    """The slot's lock dies with its supervisor, not with the command: wait for that."""
    told = False
    while prev and (groups := _orphaned_groups(prev)):
        if not told:
            _write_line(log, f"[labctl] slot {slot}: waiting for run {prev.name}, whose supervisor died while "
                             f"its process group {groups[0]} still runs (`labctl cancel {prev.name}` ends it)".encode())
            told = True
        time.sleep(ORPHAN_POLL_SECONDS)


def _write_line(log, line: bytes) -> None:
    log.write(line + b"\n")
    log.flush()
    try:  # echo for a human attached to the tmux session; non-blocking, so dropped if the pane is stopped
        os.write(sys.stdout.fileno(), line + b"\n")
    except (OSError, ValueError):
        pass


def _stream(proc, log, meta, run_dir: Path) -> None:
    patterns = [re.compile(p) for p in meta["wake_on"]]
    stall = meta["stall_minutes"]
    checkins = list(meta.get("checkin_minutes") or [])  # sorted; each fires once
    fd = proc.stdout.fileno()
    buf, last, stalled = b"", time.monotonic(), False
    started = last  # the command's start, not the queueing on a slot

    def match(segment: bytes):
        text = segment.decode(errors="replace")
        for p in patterns:
            if p.search(text):
                emit(run_dir, "match", pattern=p.pattern, line=text)

    def handle(line: bytes):
        # '\r' ends a progress update: match every update, log only the last one
        *updates, final = line.removesuffix(b"\r").split(b"\r")
        for u in updates:
            if u:
                match(u)
        _write_line(log, final)
        match(final)

    # The run ends when the main process exits: a daemonized grandchild that
    # keeps the pipe open gets DRAIN_SECONDS more, then its group is killed.
    drain_until, fds = None, [fd]
    while True:
        now = time.monotonic()
        deadlines = [now + EXIT_POLL_SECONDS] if drain_until is None else [drain_until]
        if stall is not None and not stalled:
            deadlines.append(last + stall * 60)
        if checkins and drain_until is None:
            deadlines.append(started + checkins[0] * 60)
        timeout = max(0.0, min(deadlines) - now)
        if fds:
            ready, _, _ = select.select(fds, [], [], timeout)
        else:  # the output closed but the command lives on: its timers still run
            ready = []
            try:
                proc.wait(timeout)
            except subprocess.TimeoutExpired:
                pass
        if drain_until is None and proc.poll() is not None:
            if not fds:
                break
            drain_until = time.monotonic() + DRAIN_SECONDS
        now = time.monotonic()
        while checkins and drain_until is None and now >= started + checkins[0] * 60:
            emit(run_dir, "checkin", minutes=checkins.pop(0), elapsed=round(now - started, 1))
        if not ready:
            if drain_until is not None and now >= drain_until:
                break
            if stall is not None and not stalled and now >= last + stall * 60:
                stalled = True  # one event per silence; re-armed by the next output
                emit(run_dir, "stall", minutes=stall)
            continue
        data = os.read(fd, 65536)
        if not data:
            if proc.poll() is not None:
                break
            fds = []
            continue
        last, stalled = time.monotonic(), False
        *lines, buf = (buf + data).split(b"\n")
        for line in lines:
            handle(line)
        cut = buf.rfind(b"\r", 0, len(buf) - 1)  # a trailing '\r' may be half of '\r\n'
        if cut >= 0:
            for u in buf[:cut].split(b"\r"):
                if u:
                    match(u)
            buf = buf[cut + 1:]
        buf = buf[-MAX_PENDING:]
    if buf:
        handle(buf)
    proc.stdout.close()
    if proc.poll() is not None:
        _killpg(proc.pid, signal.SIGTERM)  # leftovers of the run's process group, if any


def _killpg(pgid: int, sig) -> bool:
    try:
        os.killpg(pgid, sig)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def cancel(run_dir: Path, grace: float = 5.0) -> str:
    """Kill the run's process group and make sure the cancellation is recorded."""
    run_dir = Path(run_dir)
    (run_dir / ".env.json").unlink(missing_ok=True)  # a supervisor that never started leaves it
    st = runs.read_json(run_dir / "status.json", {})
    if st.get("state") in runs.FINAL:
        return f"{run_dir.name} already {st['state']}"
    (run_dir / "cancel").touch()
    sup = st.get("supervisor_pid")
    groups = [g for g in (st.get("child_pid"), st.get("check_pid")) if g and _killpg(g, signal.SIGTERM)]
    if groups:  # the command, or the check after it
        deadline = time.monotonic() + grace
        while any(_killpg(g, 0) for g in groups) and time.monotonic() < deadline:
            time.sleep(0.1)
        for g in groups:
            _killpg(g, signal.SIGKILL)
    elif runs.pid_alive(sup):
        os.kill(sup, signal.SIGTERM)  # queued: the supervisor records it and exits

    deadline = time.monotonic() + grace
    while runs.pid_alive(sup) and time.monotonic() < deadline:
        if runs.read_json(run_dir / "status.json", {}).get("state") in runs.FINAL:
            break
        time.sleep(0.1)

    st = runs.read_json(run_dir / "status.json", {})
    if st.get("state") not in runs.FINAL and not runs.pid_alive(sup):
        # Nobody is left to record it, so record it here.
        emit(run_dir, "exit", code=None, check=None, ok=False, cancelled=True)
        st.update(state="cancelled", finished=runs.now())
        runs.write_json(run_dir / "status.json", st)
    return f"{run_dir.name} {runs.state(run_dir)}"
