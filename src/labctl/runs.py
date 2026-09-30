"""Run directory layout and the small file helpers everything else uses.

<runs>/<id>/
  run.json       what was asked for (command, cwd, git, options)
  status.json    supervisor state, written atomically
  events.jsonl   one event per line, seq starts at 1
  log            merged stdout/stderr of the command
  exit-code      the command's exit code (128+N if killed by signal N)
  brief.md       copy of --brief, if given
  sessions.json  agent sessions (experimenter with --brief, manager with --manager)
  wake.log       agent deliveries: command, exit code, output tail
<runs>/inbox.jsonl   every manager-routed wake, numbered by "n"
"""

import datetime
import fcntl
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

FINAL = ("succeeded", "failed", "cancelled")
UNKNOWN = "unknown (supervisor not running)"
NEVER_STARTED = "unknown (supervisor never started)"
START_GRACE = 30.0  # seconds a run may stay 'starting' before that is reported as unknown
MAX_BRIEF = 64 * 1024  # bytes
# How labctl re-invokes itself. -P: a project file named like a stdlib module
# (token.py, ...) in the cwd must not shadow it.
LABCTL = [sys.executable, "-P", "-m", "labctl"]
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


def now() -> str:
    t = datetime.datetime.now(datetime.timezone.utc)
    return t.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def runs_dir() -> Path:
    return Path(os.environ.get("LABCTL_RUNS_DIR") or "runs").resolve()


def tmux_session(run_id: str) -> str:
    return "labctl-" + run_id.replace(".", "_")  # tmux forbids '.' in names


def read_json(path: Path, default=None):
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, ValueError):  # missing, corrupt, or not UTF-8
        return default
    return data if isinstance(data, dict) else default


def write_json(path: Path, data) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


def git_info(cwd: Path):
    def git(*args):
        return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)

    try:
        head = git("rev-parse", "HEAD")
    except FileNotFoundError:
        return None
    if head.returncode:
        return None
    return {"commit": head.stdout.strip(), "dirty": bool(git("status", "--porcelain").stdout.strip())}


def create_run(root: Path, command: list[str], *, cwd: Path, name=None, slot=None,
               wake_on=(), stall=None, check=None, brief=None, env=None, sessions=None) -> Path:
    """Create the run directory and its initial files. Raises FileExistsError."""
    if brief and Path(brief).stat().st_size > MAX_BRIEF:
        raise ValueError(f"brief {brief} is {Path(brief).stat().st_size} bytes; the limit is {MAX_BRIEF}")
    if name is not None and not NAME_RE.fullmatch(name):
        raise ValueError(f"invalid run name {name!r}: use letters, digits, '_', '-', '.'")
    root.mkdir(parents=True, exist_ok=True)
    for attempt in range(20):
        run_id = name or datetime.datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
        run_dir = root / run_id
        try:
            run_dir.mkdir()
            break
        except FileExistsError:
            if name or attempt == 19:
                raise
    write_json(run_dir / "run.json", {
        "id": run_id,
        "command": list(command),
        "cwd": str(cwd),
        "created": now(),
        "git": git_info(cwd),
        "slot": slot,
        "wake_on": list(wake_on),
        "stall_minutes": stall,
        "check": check,
        "tmux": tmux_session(run_id),
    })
    if brief:
        shutil.copyfile(brief, run_dir / "brief.md")  # size checked by the caller
    if sessions:
        write_json(run_dir / "sessions.json", sessions)  # see agents.py
    if env is not None:
        # The tmux server does not reliably pass the caller's environment on,
        # so hand it to the supervisor in a private file it deletes on start.
        fd = os.open(run_dir / ".env.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(dict(env), f)
    write_json(run_dir / "status.json", {"state": "starting"})
    return run_dir


def _parse_jsonl(text: str) -> list[dict]:
    records = []
    for line in text.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue  # truncated or corrupt line
        if isinstance(obj, dict):
            records.append(obj)
    return records


def read_jsonl(path: Path) -> list[dict]:
    try:
        return _parse_jsonl(path.read_text(errors="replace"))
    except FileNotFoundError:
        return []


def append_numbered(path: Path, key: str, build) -> dict:
    """Append build(n) as a JSON line, n = 1 + the largest `key` so far, under an flock."""
    with open(path, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        text = f.read()
        numbers = [r[key] for r in _parse_jsonl(text) if isinstance(r.get(key), int)]
        record = build(max(numbers, default=0) + 1)
        sep = "" if not text or text.endswith("\n") else "\n"  # never glue onto a truncated line
        f.write(sep + json.dumps(record) + "\n")
    return record


def append_event(run_dir: Path, event: str, source: str = "supervisor", **fields) -> dict:
    return append_numbered(run_dir / "events.jsonl", "seq", lambda seq: {
        "seq": seq, "time": now(), "source": source, "run": run_dir.name, "event": event, **fields})


def read_events(run_dir: Path) -> list[dict]:
    return [e for e in read_jsonl(run_dir / "events.jsonl") if isinstance(e.get("seq"), int)]


def pid_alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return True
    return stat.rsplit(")", 1)[1].split()[0] != "Z"  # zombies are dead


def state(run_dir: Path) -> str:
    """The state from status.json, unless that claims a live supervisor that is gone."""
    path = run_dir / "status.json"
    st = read_json(path, {})
    s = st.get("state", "unknown")
    if s in ("queued", "running") and not pid_alive(st.get("supervisor_pid")):
        return UNKNOWN
    if s == "starting" and time.time() - path.stat().st_mtime > START_GRACE:
        return NEVER_STARTED
    return str(s)


def tail(path: Path, n: int) -> list[str]:
    """Last n lines of a file, reading backwards so huge logs stay cheap."""
    if n <= 0:
        return []
    try:
        f = open(path, "rb")
    except FileNotFoundError:
        return []
    with f:
        end = f.seek(0, os.SEEK_END)
        pos, data = end, b""
        while pos > 0 and data.count(b"\n") <= n:
            pos = max(0, pos - 65536)
            f.seek(pos)
            data = f.read(end - pos)
    return data.decode(errors="replace").splitlines()[-n:]
