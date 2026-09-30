"""Wake messages: formatting, routing, the manager inbox, and handing off delivery.

Routing: successful or cancelled exits, and everything not from the supervisor
(escalation, report, delivery_failed), go to the manager. Other supervisor
events (failed exit, match, stall) go to the run's experimenter if it has one,
else to the manager. Every manager-routed event is appended to
<runs>/inbox.jsonl, whether or not a manager session exists.

A wake is one header line, then indented body lines (the message, or the log
tail). Only an unindented first line is a header, so a message or a log line
cannot forge one.
"""

import sys
from pathlib import Path

from labctl import runs

_BASE = ("seq", "time", "source", "run", "event", "message", "line")
MAX_LINE = 500  # characters per log line in a wake


def _fmt(value) -> str:
    if value is None:
        return "none"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float):
        return f"{value:g}"
    return " ".join(str(value).split())  # header fields stay on one line


def summary(event: dict) -> str:
    """'event=<kind> k=v ...' with the fields that matter for this kind of event."""
    kind = event.get("event")
    if kind == "exit":
        keys = ["code", "check"] + [k for k in ("cancelled", "signal") if event.get(k)]
    elif kind == "match":
        keys = ["pattern"]
    elif kind == "stall":
        keys = ["minutes"]
    else:
        keys = [k for k in event if k not in _BASE]
    return " ".join([f"event={_fmt(kind)}"] + [f"{k}={_fmt(event.get(k))}" for k in keys])


def header(event: dict, inbox: int | None = None) -> str:
    n = f" inbox={inbox}" if inbox is not None else ""
    return (f"[labctl wake] source={_fmt(event.get('source'))} run={_fmt(event.get('run'))} "
            f"{summary(event)} seq={event.get('seq')}{n} time={event.get('time')}")


def cap_line(line: str) -> str:
    return line if len(line) <= MAX_LINE else line[:MAX_LINE] + " [labctl: line truncated]"


def format_wake(run_dir: Path, event: dict, lines: int = 40, inbox: int | None = None) -> str:
    """Header, then the event's message, or its matched line and the run's log tail (lines=0: none)."""
    if "message" in event:
        body = str(event["message"]).splitlines()
    else:
        body = [f"matched: {cap_line(str(event['line']))}"] if "line" in event else []
        body += [cap_line(line) for line in runs.tail(Path(run_dir) / "log", lines)]
    return "\n".join([header(event, inbox)] + ["  " + line for line in body])


def route(run_dir: Path, event: dict) -> str:
    """'experimenter' or 'manager'."""
    if event.get("source") != "supervisor":
        return "manager"
    if event.get("event") == "exit" and (event.get("ok") or event.get("cancelled")):
        return "manager"
    has_experimenter = "experimenter" in runs.read_json(Path(run_dir) / "sessions.json", {})
    return "experimenter" if has_experimenter else "manager"


def inbox_path(root: Path) -> Path:
    return Path(root) / "inbox.jsonl"


def append_inbox(run_dir: Path, event: dict) -> dict:
    run_dir = Path(run_dir)
    return runs.append_numbered(inbox_path(run_dir.parent), "n", lambda n: {
        "n": n, "time": runs.now(), "run": run_dir.name, "seq": event.get("seq"),
        "text": format_wake(run_dir, event, inbox=n)})


def emit(run_dir: Path, event: str, **fields) -> dict:
    """Record an event and deliver it. Delivery problems never propagate."""
    ev = runs.append_event(Path(run_dir), event, **fields)
    try:
        deliver(run_dir, ev)
    except Exception as e:
        print(f"[labctl] wake delivery failed: {e!r}", file=sys.stderr)
        try:
            with open(Path(run_dir) / "wake.log", "a") as f:
                f.write(f"== {runs.now()} seq={ev['seq']} deliver() raised {e!r}\n")
        except OSError:
            pass
    return ev


def deliver(run_dir: Path, event: dict) -> None:
    """Route an event; hand agent delivery to a detached `labctl _deliver`. Never blocks."""
    run_dir = Path(run_dir)
    role = route(run_dir, event)
    if role == "manager":
        append_inbox(run_dir, event)
        if event.get("event") == "delivery_failed" and event.get("role") == "manager":
            return  # that session just failed; the inbox has it and the next manager turn includes it
    rec = runs.read_json(run_dir / "sessions.json", {}).get(role)
    if not rec or not (role == "experimenter" or rec.get("session_id")):
        return  # no manager session: the inbox is the delivery (`labctl wait` without ID)
    from labctl import agents  # agents imports this module

    agents.spawn_delivery(run_dir, role, rec, event["seq"])
