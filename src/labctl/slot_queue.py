"""Order live waiters for an exclusive execution slot."""

import fcntl
from contextlib import contextmanager
from pathlib import Path

from labctl import runs


@contextmanager
def locked(root: Path, slot: str):
    directory = root / ".slots" / "queue"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f"{slot}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield directory / f"{slot}.json"


def waiting(root: Path, slot: str, order_file: Path, *, inspect=False):
    """Read while holding the queue lock. Return ordered native and legacy IDs."""
    preferred = runs.read_json(order_file, {}).get("first", [])
    positions = {name: index for index, name in enumerate(preferred)}
    native, legacy = [], []
    for path in root.glob("*/run.json"):
        directory = path.parent
        meta = runs.read_json(path, {})
        if meta.get("slot") != slot or (directory / "cancel").exists():
            continue
        status = runs.read_json(directory / "status.json", {})
        if status.get("admitted") or status.get("state") != "queued":
            continue
        if runs.namespace_issue(status):
            if not inspect:
                continue
        elif not runs.pid_alive(status.get("supervisor_pid")):
            continue
        name = directory.name
        if "submitted_ns" not in meta:
            legacy.append(name)
            continue
        estimate = meta.get("estimate_minutes")
        native.append((positions.get(name, len(positions)),
                       estimate if estimate is not None else float("inf"),
                       meta["submitted_ns"], name))
    return [entry[-1] for entry in sorted(native)], sorted(legacy)
