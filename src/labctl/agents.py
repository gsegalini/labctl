"""Agent sessions of a run: records, per-session locks, one wake turn, attach.

<run>/sessions.json:
  {"experimenter": {"harness", "session_id", "model", "started",    # only with --brief
                    "queued_exit"},  # seq of an exit wake queued into an open codex conversation
   "manager": {"harness", "session_id", "model"}}                   # only with --manager
"""

import fcntl
import os
import re
import shlex
import signal
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from labctl import harness, install, runs, wake

HARNESS_TIMEOUT = 1800  # seconds per harness command; LABCTL_HARNESS_TIMEOUT overrides


def new_sessions(experimenter: str | None, manager: str | None) -> dict:
    """Records for `labctl run`: experimenter harness name, manager 'HARNESS:SESSION_ID'."""
    sessions = {}
    if experimenter:
        tier = install.load_role("experimenter").tier
        sessions["experimenter"] = {"harness": experimenter, "session_id": None,
                                    "model": install.model_for(experimenter, tier), "started": False}
    if manager:
        h, _, sid = manager.partition(":")
        if h not in install.HARNESSES or not sid:
            raise ValueError(f"--manager must be HARNESS:SESSION_ID with HARNESS in {install.HARNESSES}")
        # codex forgets the model on resume, so pin the manager's
        sessions["manager"] = {"harness": h, "session_id": sid,
                               "model": install.model_for(h, "frontier") if h == "codex" else None}
    return sessions


def load(run_dir: Path) -> dict:
    return runs.read_json(run_dir / "sessions.json", {})


def lock_path(run_dir: Path, role: str, rec: dict) -> Path:
    """Experimenter locks live in the run; manager locks are shared by all runs of that session."""
    if role == "manager":
        key = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{rec['harness']}-{rec['session_id']}")
        path = run_dir.parent / ".sessions" / f"{key}.lock"
    else:
        path = run_dir / "sessions" / f"{role}.lock"
    path.parent.mkdir(exist_ok=True)
    return path


@contextmanager
def session_lock(path: Path):
    with open(path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def busy(path: Path) -> bool:
    try:
        f = open(path)
    except FileNotFoundError:
        return False
    with f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False


def in_git_repo(cwd) -> bool:
    try:
        r = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--is-inside-work-tree"], capture_output=True)
    except FileNotFoundError:
        return False
    return r.returncode == 0


def update(run_dir: Path, role: str, **fields) -> None:
    """Change one role's record. Both roles' deliveries write sessions.json, so serialize."""
    (run_dir / "sessions").mkdir(exist_ok=True)
    with session_lock(run_dir / "sessions" / ".write.lock"):
        sessions = load(run_dir)
        sessions[role].update(fields)
        runs.write_json(run_dir / "sessions.json", sessions)


def spawn_delivery(run_dir: Path, role: str, rec: dict, seq: int) -> None:
    """Start a detached `labctl _deliver`, unless one is already waiting for this session.

    The waiter holds `<run>/sessions/<role>.pending` (handed over as an inherited fd) until it
    owns the session lock, and reads pending events only after releasing it, so
    an event whose spawn is skipped here is still picked up by that waiter.
    """
    (run_dir / "sessions").mkdir(exist_ok=True)
    f = open(run_dir / "sessions" / f"{role}.pending", "a")  # per run: a waiter delivers its own run only
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return
    with f, open(run_dir / "wake.log", "ab") as err:
        subprocess.Popen([*runs.LABCTL, "_deliver", str(run_dir), str(seq), "--pending-fd", str(f.fileno())],
                         pass_fds=[f.fileno()], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=err, start_new_session=True)


def _tail(text: str, n: int = 20) -> str:
    return "\n".join(wake.cap_line(line) for line in text.splitlines()[-n:])


def _run(run_dir: Path, role: str, seqs: str, cmd: tuple[list[str], str], cwd: str) -> subprocess.CompletedProcess:
    """Run one harness command (argv, stdin text) with a time limit, and log it to wake.log."""
    argv, stdin_text = cmd
    env = {**os.environ, "LABCTL_RUNS_DIR": str(run_dir.parent)}
    env.pop("CLAUDECODE", None)  # a headless turn is its own session, not a nested one
    limit = float(os.environ.get("LABCTL_HARNESS_TIMEOUT", HARNESS_TIMEOUT))
    shown = shlex.join(a if len(a) <= 200 else a[:200] + "..." for a in argv)
    with open(run_dir / "wake.log", "a") as f:  # before the turn: a delivery that dies midway leaves this
        f.write(f"== {runs.now()} seq={seqs} role={role}\n$ {shown}  (message: {len(stdin_text)} chars on stdin)\n")
    start = time.monotonic()
    try:
        p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, errors="replace", start_new_session=True)
    except OSError as e:
        r = subprocess.CompletedProcess(argv, 127, "", f"labctl: cannot run {argv[0]}: {e}")
    else:
        try:
            out, err = p.communicate(stdin_text, timeout=limit)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            out, err = p.communicate()
            err += f"\nlabctl: harness timed out after {limit:g} s and was killed"
        r = subprocess.CompletedProcess(argv, p.returncode, out, err)
    usage = harness.usage_from(argv[0], r.stdout) if argv else None
    with open(run_dir / "wake.log", "a") as f:
        f.write(f"exit {r.returncode} after {time.monotonic() - start:.1f} s (seq={seqs} role={role})\n"
                f"usage: {usage or 'unknown'}\n")
        f.write(_tail(r.stdout + "\n" + r.stderr).strip("\n") + "\n")
    return r


def deliver_turn(run_dir: Path, seq: int, pending_fd: int | None = None) -> None:
    """Body of `labctl _deliver`: deliver every undelivered event of `seq`'s role in one turn.

    Under the session lock, the role's cursor (`delivered`) is advanced before the
    turn, so no event is ever delivered twice; events that arrive during a turn
    are delivered together in the next one. A turn is logged to wake.log before it
    runs, so one cut short (the process killed) still leaves a trace.
    """
    run_dir = Path(run_dir).resolve()
    event = next((e for e in runs.read_events(run_dir) if e["seq"] == seq), None)
    role = wake.route(run_dir, event) if event else None
    rec = load(run_dir).get(role)
    if not rec or not (role == "experimenter" or rec.get("session_id")):
        return
    with session_lock(lock_path(run_dir, role, rec)):
        if pending_fd is not None:
            os.close(pending_fd)  # from now on a new event spawns a new waiter
        rec = load(run_dir)[role]
        events = runs.read_events(run_dir)
        pending = [e for e in events if e["seq"] > rec.get("delivered", 0) and wake.route(run_dir, e) == role]
        if not pending:
            return
        update(run_dir, role, delivered=pending[-1]["seq"])
        _turn(run_dir, role, rec, pending, last_seq=events[-1]["seq"])


def _message(run_dir: Path, pending: list[dict]) -> str:
    """Each event's wake, with the matches of one pattern collapsed into one wake at its last
    match, so a burst cannot grow the message; the log tail only once, after the last wake."""
    matches = {}
    for e in pending:
        if e["event"] == "match":
            matches.setdefault(e.get("pattern"), []).append(e)
    parts = []
    for e in pending:
        group = matches.get(e.get("pattern"), []) if e["event"] == "match" else []
        if len(group) > 1 and e is not group[-1]:
            continue
        lines = 40 if e is pending[-1] else 0
        parts.append(wake.format_matches(run_dir, group, lines) if len(group) > 1
                     else wake.format_wake(run_dir, e, lines))
    return "\n\n".join(parts)


def _turn(run_dir: Path, role: str, rec: dict, pending: list[dict], last_seq: int) -> None:
    cwd = runs.read_json(run_dir / "run.json")["cwd"]
    in_git = in_git_repo(cwd)
    seqs = ",".join(str(e["seq"]) for e in pending)
    message = _message(run_dir, pending)
    h, sid = rec["harness"], rec.get("session_id")
    starting = role == "experimenter" and not rec.get("started")
    prompt = install.load_role(role).prompt if role == "experimenter" else None  # resumes: see harness.resume

    def resume():
        return harness.resume(h, sid, message, model=rec.get("model"), role=prompt, in_git=in_git)

    exits = [e for e in pending if e["event"] == "exit"]
    r, queued = None, False
    try:
        if starting and sid:
            # an earlier start may have created the session before failing: resume it if it exists
            r = _run(run_dir, role, seqs, resume(), cwd)
            if r.returncode and harness.session_not_found(r.stdout + r.stderr):
                r = None
        if r is None and starting:
            sid = str(uuid.uuid4()) if h == "claude" else None
            if sid:
                update(run_dir, role, session_id=sid)  # recorded before the turn, so it is never lost
            brief = (run_dir / "brief.md").read_text(errors="replace")
            rid = run_dir.name
            text = (f"Brief for labctl run {rid}:\n\n{brief}\n\nYour commands for this run (use your shell tool):\n"
                    f"  labctl status {rid}\n  labctl tail {rid} -n 100\n  labctl cancel {rid}\n"
                    f'  labctl escalate {rid} -m "<what happened, evidence, decision needed>"\n'
                    f'  labctl escalate {rid} --fix -m "<observed, evidence, cause, what the fix must achieve>"\n'
                    f"\n{message}")
            r = _run(run_dir, role, seqs, harness.start(h, model=rec["model"], role=prompt,
                                                        message=text, session_id=sid, in_git=in_git), cwd)
        elif r is None:
            r = _run(run_dir, role, seqs, resume(), cwd)
            if h == "codex" and r.returncode and "already has an active writer" in r.stderr:
                # a person has the session open: queue the wake into their conversation
                if role == "experimenter" and exits:
                    message += (f"\n\nlabctl: this wake was queued into a conversation a person has open, so your "
                                f"reply is not forwarded. Send your verdict with `labctl report {run_dir.name} "
                                f'-m "..."` (or escalate).')
                r, queued = _run(run_dir, role, seqs, harness.codex_queue(sid, message), cwd), True
    except ValueError as e:  # a command that cannot be built (e.g. an oversized role prompt)
        r = subprocess.CompletedProcess([], 2, "", f"labctl: {e}")

    reply, is_error = harness.reply_from(h, r.stdout)
    problem = f"exit {r.returncode}" if r.returncode else "harness reported an error" if is_error else None
    if not problem and not queued and not harness.json_objects(r.stdout):
        problem = "no JSON in the harness output"
    if starting:
        found = harness.session_id_from(h, r.stdout)
        if found and h != "claude" and found != sid:
            update(run_dir, role, session_id=found)  # known even if the turn failed: resumed next time
        if not problem and not found:
            problem = "no session id in the harness output; the session stays unstarted"
        if not problem:
            update(run_dir, role, session_id=sid if h == "claude" else found, started=True)

    if problem:
        text = f"{problem}\n{_tail(r.stdout + chr(10) + r.stderr)}".strip()
        if role == "experimenter":
            text += "\n\nUndelivered wake(s), forwarded to the manager:\n" + message
        wake.emit(run_dir, "delivery_failed", source="labctl", role=role, for_seq=seqs, message=text)
        return
    if role == "experimenter" and exits:
        if queued:  # the verdict comes from `labctl report`; attach follows up if it does not
            update(run_dir, role, queued_exit=exits[-1]["seq"])
        elif not _verdict_after(run_dir, last_seq):  # the run is over: its outcome must reach the manager
            wake.emit(run_dir, "report", source="experimenter", code=exits[-1].get("code"),
                      check=exits[-1].get("check"), message=reply or _tail(r.stdout + r.stderr))


def _verdict_after(run_dir: Path, seq: int) -> bool:
    return any(e["event"] in ("escalation", "fix_request", "report") and e["seq"] > seq
               for e in runs.read_events(run_dir))


def _follow_up_queued_exit(run_dir: Path, role: str, path: Path) -> None:
    """After a codex attach: an exit wake queued into the conversation still needs its verdict."""
    with session_lock(path):
        rec = load(run_dir)[role]
        seq = rec.get("queued_exit")
        if not seq:
            return
        update(run_dir, role, queued_exit=None)
        events = runs.read_events(run_dir)
        if _verdict_after(run_dir, seq):
            return
        print("labctl attach: the run's exit reached the conversation but no verdict was sent; "
              "asking the experimenter for it...", flush=True)
        _turn(run_dir, role, rec, [e for e in events if e["seq"] == seq], last_seq=events[-1]["seq"])


def attach(run_dir: Path, role: str) -> int:
    """Open the session for a human. Wakes wait until they leave, except for codex,
    which queues them into the open conversation."""
    rec = load(run_dir).get(role)
    if not rec:
        raise SystemExit(f"labctl attach: run {run_dir.name} has no {role}"
                         + (" (it was started without --brief)" if role == "experimenter" else ""))
    if not rec.get("session_id"):  # an id without `started`: a failed first turn, worth a look
        raise SystemExit(f"labctl attach: no session yet: no event has woken the {role} of {run_dir.name}")
    argv = harness.attach(rec["harness"], rec["session_id"], rec.get("model"))
    path = lock_path(run_dir, role, rec)
    if busy(path):
        print("labctl attach: waiting for the agent's current turn to finish...", flush=True)
    cwd = runs.read_json(run_dir / "run.json")["cwd"]
    with session_lock(path):
        if rec["harness"] != "codex":
            return subprocess.run(argv, cwd=cwd).returncode
    code = subprocess.run(argv, cwd=cwd).returncode
    if role == "experimenter":
        _follow_up_queued_exit(run_dir, role, path)
    return code
