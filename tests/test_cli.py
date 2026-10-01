import os
import shutil
import subprocess
import uuid

import pytest
from conftest import events, make_run, start, status, supervise, wait_until

from labctl import runs, supervisor
from labctl.cli import main


def test_wait_returns_on_new_event(root, capsys):
    d = make_run(root, "echo warmup; sleep 0.5; echo target hit; sleep 30", wake_on=["target"])
    p = start(d)
    try:
        assert main(["wait", d.name, "--poll", "0.05"]) == 0
        out = capsys.readouterr().out.splitlines()
        assert out[0].startswith(f"[labctl wake] source=supervisor run={d.name} event=match pattern=target seq=1 ")
        assert "  matched: target hit" in out[1:] and "  target hit" in out[1:]
    finally:
        supervisor.cancel(d, grace=1)
        p.wait(timeout=10)


def test_wait_on_finished_run_returns_immediately(root, capsys):
    d = make_run(root, "echo hi; exit 4", wake_on=["hi"])
    supervise(d)
    assert main(["wait", d.name]) == 0
    out = capsys.readouterr().out.splitlines()
    assert "event=exit code=4 check=none seq=2" in out[0]
    assert main(["wait", d.name, "--after", "0"]) == 0
    assert "event=match" in capsys.readouterr().out.splitlines()[0]


def _dead_pid():
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def test_status_reports_dead_supervisor(root, capsys):
    d = make_run(root, "true", name="ghost")
    runs.write_json(d / "status.json", {"state": "running", "supervisor_pid": _dead_pid()})
    assert main(["status", "ghost"]) == 0
    assert "state:   unknown (supervisor not running)" in capsys.readouterr().out
    assert main(["status"]) == 0
    assert "ghost" in (line := capsys.readouterr().out) and "unknown (supervisor not running)" in line


def test_status_and_tail(root, capsys):
    d = make_run(root, "for i in 1 2 3; do echo line $i; done")
    supervise(d)
    main(["status", d.name])
    out = capsys.readouterr().out
    assert "state:   succeeded" in out and "supervisor  exit code=0 check=none" in out and "  line 3" in out
    main(["tail", d.name, "-n", "2"])
    assert capsys.readouterr().out == "line 2\nline 3\n"


def test_status_list(root, capsys):
    assert main(["status"]) == 0
    assert capsys.readouterr().out == f"no runs in {root}\n"
    b = make_run(root, "true", name="b-first")
    a = make_run(root, "true", name="a-second")
    runs.write_json(a / "run.json", {**runs.read_json(a / "run.json"), "created": "2099-01-01T00:00:00.000Z"})
    supervise(b)
    assert main(["status"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[2].split() == ["RUN", "STATE", "AGE", "LAST", "EVENT"]
    assert lines[3].split()[:2] == ["b-first", "succeeded"] and lines[4].startswith("a-second")  # by creation
    assert lines[3].endswith("exit code=0 check=none")


def test_run_refuses_existing_name(root):
    make_run(root, "true", name="taken")
    with pytest.raises(SystemExit, match="already exists"):
        main(["run", "--name", "taken", "--", "true"])


def test_run_option_errors(root, tmp_path):
    brief = tmp_path / "b.md"
    brief.write_text("x")
    with pytest.raises(SystemExit, match="needs --harness or"):
        main(["run", "--brief", str(brief), "--", "true"])
    with pytest.raises(SystemExit, match="HARNESS:SESSION_ID"):
        main(["run", "--manager", "vim:1", "--", "true"])
    for bad in (["--checkin", "-1"], ["--checkin", "nan"], ["--stall", "0"], ["--stall", "inf"],
                ["--slot", "../x"], ["--slot", ".hidden"]):
        with pytest.raises(SystemExit, match=f"{bad[0]} "):
            main(["run", *bad, "--", "true"])
    assert not root.exists() or not any(root.iterdir())


def test_checkin_is_recorded_in_run_json(root):
    d = make_run(root, "true", checkin=[30, 0.5, 30])
    assert runs.read_json(d / "run.json")["checkin_minutes"] == [0.5, 30]


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
def test_end_to_end_with_tmux(root, tmp_path, fake_agents):
    name = "e2e-" + uuid.uuid4().hex[:8]
    env = {**os.environ, "LABCTL_E2E": "from-caller", "LABCTL_HARNESS": "claude"}
    brief = tmp_path / "brief.txt"
    brief.write_text("why this run exists\n")
    cmd = [*runs.LABCTL, "run", "--name", name, "--wake-on", "step 2",
           "--brief", str(brief), "--", "sh", "-c", 'echo step 1 $LABCTL_E2E; echo step 2']
    try:
        r = subprocess.run(cmd, env=env, cwd=tmp_path, capture_output=True, text=True, timeout=10)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == name
        d = root / name
        wait_until(lambda: status(d).get("state") == "succeeded")
        # the run's own events; the experimenter's report may already follow them
        assert [e["event"] for e in events(d) if e["source"] == "supervisor"] == ["match", "exit"]
        assert (d / "log").read_text() == "step 1 from-caller\nstep 2\n"
        assert (d / "brief.md").read_text() == "why this run exists\n"
        assert not (d / ".env.json").exists()
        meta = runs.read_json(d / "run.json")
        assert meta["command"] == ["sh", "-c", "echo step 1 $LABCTL_E2E; echo step 2"]
        assert meta["cwd"] == str(tmp_path) and meta["git"] is None
        # the match and the successful exit woke the experimenter (fake claude, found through
        # the caller's PATH); its verdict reached the inbox as the one report of the run
        wait_until(lambda: (root / "inbox.jsonl").exists())
        call = fake_agents()[0]
        assert call["argv"][:2] == ["claude", "-p"] and "--session-id" in call["argv"]
        assert call["cwd"] == str(tmp_path)
        [entry] = runs.read_jsonl(root / "inbox.jsonl")
        assert "event=report code=0 check=none" in entry["text"]
    finally:
        subprocess.run(["tmux", "kill-session", "-t", runs.tmux_session(name)], capture_output=True)
