import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from labctl import runs


@pytest.fixture
def root(tmp_path, monkeypatch):
    """A temporary runs dir, also used by the CLI through LABCTL_RUNS_DIR."""
    d = tmp_path / "runs"
    monkeypatch.setenv("LABCTL_RUNS_DIR", str(d))
    monkeypatch.setenv("LABCTL_CONFIG", str(tmp_path / "no-config.toml"))  # default models
    monkeypatch.delenv("LABCTL_HARNESS", raising=False)
    return d


FAKE = Path(__file__).with_name("fake_harness.py")


@pytest.fixture
def fake_agents(tmp_path, monkeypatch):
    """Fake claude/codex/opencode first on PATH (see fake_harness.py); returns a call lister."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("claude", "codex", "opencode"):
        exe = bin_dir / name
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" {name} "$@"\n')
        exe.chmod(0o755)
    calls = tmp_path / "calls.jsonl"
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CALLS", str(calls))
    for var in ("FAKE_MODE", "FAKE_ONCE", "FAKE_SLEEP", "FAKE_REPLY", "FAKE_ESCALATE", "FAKE_ESCALATE_RUN", "FAKE_ATTACH_SLEEP"):
        monkeypatch.delenv(var, raising=False)

    def read():
        return [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
    def add_session(sid):  # a session that exists already (e.g. the manager's)
        with open(str(calls) + ".sessions", "a") as f:
            f.write(sid + "\n")
    read.bin_dir, read.add_session = bin_dir, add_session
    return read


def inbox(root: Path) -> list[dict]:
    return runs.read_jsonl(root / "inbox.jsonl")


def make_run(root: Path, script: str, **kw) -> Path:
    return runs.create_run(root, ["sh", "-c", script], cwd=root.parent, **kw)


def start(run_dir: Path) -> subprocess.Popen:
    """Start the supervisor as a subprocess, without tmux."""
    return subprocess.Popen([*runs.LABCTL, "_supervise", str(run_dir)],
                            stdout=subprocess.DEVNULL)


def supervise(run_dir: Path) -> None:
    assert start(run_dir).wait(timeout=20) == 0


def events(run_dir: Path, kind: str | None = None) -> list[dict]:
    return [e for e in runs.read_events(run_dir) if kind is None or e["event"] == kind]


def status(run_dir: Path) -> dict:
    return runs.read_json(run_dir / "status.json", {})


def wait_until(cond, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)
