import os
import subprocess
import threading
import time

import pytest
from conftest import inbox, make_run, supervise, wait_until

from labctl import agents, harness, runs, wake
from labctl.cli import main
from labctl.install import load_role, model_for


def run_with(root, tmp_path, script="true", experimenter=None, manager=None, brief="BRIEF: keep it alive\n", **kw):
    path = None
    if experimenter:
        path = tmp_path / "brief.md"
        path.write_text(brief)
    return make_run(root, script, brief=path, sessions=agents.new_sessions(experimenter, manager), **kw)


def ev(d, event, **fields):
    return runs.append_event(d, event, **fields)


# --- routing and the inbox -------------------------------------------------

def test_routing_table(root, tmp_path):
    with_exp = run_with(root, tmp_path, experimenter="claude", name="with")
    without = run_with(root, tmp_path, name="without")
    rows = [  # (event, fields, route with experimenter, route without)
        ("exit", {"code": 0, "check": None, "ok": True}, "experimenter", "manager"),
        ("exit", {"code": 1, "check": None, "ok": False}, "experimenter", "manager"),
        ("exit", {"code": 0, "check": "failed", "ok": False}, "experimenter", "manager"),
        ("match", {"pattern": "x", "line": "x"}, "experimenter", "manager"),
        ("stall", {"minutes": 1.0}, "experimenter", "manager"),
        ("checkin", {"minutes": 1.0, "elapsed": 60.0}, "experimenter", "manager"),
        ("exit", {"code": 143, "check": None, "ok": False, "cancelled": True}, "manager", "manager"),
    ]
    for kind, fields, a, b in rows:
        assert wake.route(with_exp, ev(with_exp, kind, **fields)) == a, (kind, fields)
        assert wake.route(without, ev(without, kind, **fields)) == b, (kind, fields)
    for kind, source in [("escalation", "experimenter"), ("fix_request", "experimenter"), ("report", "experimenter"),
                         ("delivery_failed", "labctl")]:
        assert wake.route(with_exp, ev(with_exp, kind, source=source, message="m")) == "manager"


def test_inbox_gets_manager_events_only(root, tmp_path, fake_agents, capsys):
    d = run_with(root, tmp_path, name="r1")
    wake.deliver(d, ev(d, "exit", code=0, check=None, ok=True))
    wake.deliver(d, ev(d, "escalation", source="experimenter", message="need a decision\nsecond line"))
    first, second = inbox(root)
    assert (first["n"], first["run"], first["seq"], second["n"]) == (1, "r1", 1, 2)
    assert first["text"].startswith("[labctl wake] source=supervisor run=r1 event=exit code=0 check=none seq=1 inbox=1 ")
    assert second["text"].splitlines()[0].startswith(
        "[labctl wake] source=experimenter run=r1 event=escalation seq=2 inbox=2 ")
    assert second["text"].splitlines()[1:] == ["  need a decision", "  second line"]
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "  #2  " in out and "r1  escalation: need a decision ..." in out and "second line" not in out
    assert fake_agents() == []


def test_wait_without_id_follows_the_inbox(root, tmp_path, capsys):
    a = run_with(root, tmp_path, name="a")
    b = run_with(root, tmp_path, name="b")
    wake.deliver(a, ev(a, "exit", code=0, check=None, ok=True))
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("rc", main(["wait", "--poll", "0.05"])))
    t.start()
    time.sleep(0.3)
    assert t.is_alive()  # the existing entry does not count
    wake.deliver(b, ev(b, "escalation", source="experimenter", message="help"))
    t.join(timeout=5)
    assert out["rc"] == 0
    printed = capsys.readouterr().out
    assert printed.startswith("[labctl wake] source=experimenter run=b event=escalation seq=1 inbox=2 ")
    assert main(["wait", "--after", "0"]) == 0  # chaining with --after
    assert "inbox=1" in capsys.readouterr().out.splitlines()[0]


def test_success_without_brief_costs_no_model_calls(root, tmp_path, fake_agents):
    d = run_with(root, tmp_path, "echo fine")
    supervise(d)
    time.sleep(0.3)
    assert fake_agents() == []
    [entry] = inbox(root)
    assert "event=exit code=0" in entry["text"]


def test_success_with_brief_is_verified_then_reported_once(root, tmp_path, fake_agents, monkeypatch):
    monkeypatch.setenv("FAKE_REPLY", "checked out/metrics.json: present, 3 rows")
    d = run_with(root, tmp_path, "echo fine", experimenter="claude", check="true")
    supervise(d)
    wait_until(lambda: inbox(root))
    [call] = fake_agents()
    assert "event=exit code=0 check=passed" in call["prompt"]
    [entry] = inbox(root)
    assert entry["text"].splitlines() == [entry["text"].splitlines()[0], "  checked out/metrics.json: present, 3 rows"]
    assert entry["text"].startswith(f"[labctl wake] source=experimenter run={d.name} event=report code=0 check=passed ")


def test_success_with_failed_delivery_still_reaches_the_manager(root, tmp_path, fake_agents, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "auth")
    d = run_with(root, tmp_path, "echo fine", experimenter="claude")
    supervise(d)
    wait_until(lambda: inbox(root))
    [entry] = inbox(root)
    assert "event=delivery_failed" in entry["text"]
    assert "  [labctl wake] source=supervisor run=" in entry["text"] and "event=exit code=0" in entry["text"]


# --- experimenter sessions -------------------------------------------------

@pytest.mark.parametrize("h,expected_id", [("claude", None), ("codex", "thread-fake"), ("opencode", "ses_fake")])
def test_lazy_start_then_resume(root, tmp_path, fake_agents, monkeypatch, h, expected_id):
    monkeypatch.setenv("FAKE_REPLY", "diagnosis: bad data path")
    d = run_with(root, tmp_path, experimenter=h)
    e1 = ev(d, "match", pattern="boom", line="boom!")
    agents.deliver_turn(d, e1["seq"])
    rec = agents.load(d)["experimenter"]
    assert rec["started"] is True and rec["delivered"] == e1["seq"]
    sid = rec["session_id"]
    assert sid == expected_id or (h == "claude" and len(sid) == 36)
    e2 = ev(d, "exit", code=1, check=None, ok=False)
    agents.deliver_turn(d, e2["seq"])
    agents.deliver_turn(d, e2["seq"])  # a duplicate _deliver finds nothing pending
    first, second = fake_agents()
    model, role = model_for(h, load_role("experimenter").tier), load_role("experimenter").prompt
    assert first["argv"] == harness.start(h, model=model, role=role, message="", session_id=sid, in_git=False)[0]
    brief_at = first["prompt"].index(f"Brief for labctl run {d.name}:")
    assert (role in first["prompt"]) == (h == "opencode")
    assert "BRIEF: keep it alive" in first["prompt"] and "matched: boom!" in first["prompt"]
    assert brief_at < first["prompt"].index("[labctl wake] source=supervisor run=")
    assert second["argv"] == harness.resume(h, sid, "", model=model, in_git=False)[0]
    assert second["prompt"].startswith("[labctl wake] source=supervisor") and "event=exit code=1" in second["prompt"]
    assert first["cwd"] == str(tmp_path) and first["runs_dir"] == str(root)
    assert agents.load(d)["experimenter"]["session_id"] == sid
    assert "exit 0" in (d / "wake.log").read_text()
    # the failed exit ended without `labctl escalate`, so labctl reports the reply to the manager
    [report] = [e for e in runs.read_events(d) if e["event"] == "report"]
    assert report["source"] == "experimenter" and report["message"] == "diagnosis: bad data path"
    [entry] = inbox(root)
    assert "event=report" in entry["text"] and "  diagnosis: bad data path" in entry["text"]


@pytest.mark.parametrize("code", [0, 1])
@pytest.mark.parametrize("fix", [False, True])
def test_no_report_when_the_experimenter_escalated(root, tmp_path, fake_agents, monkeypatch, code, fix):
    d = run_with(root, tmp_path, experimenter="claude", name="esc2")
    monkeypatch.setenv("FAKE_ESCALATE_RUN", "esc2")
    monkeypatch.setenv("FAKE_ESCALATE", "OOM; need a smaller batch")
    if fix:
        monkeypatch.setenv("FAKE_FIX", "1")
    agents.deliver_turn(d, ev(d, "exit", code=code, check=None, ok=code == 0)["seq"])
    kind = "fix_request" if fix else "escalation"
    assert [e["event"] for e in runs.read_events(d)] == ["exit", kind]
    assert [e["text"].split()[4] for e in inbox(root)] == [f"event={kind}"]


def test_dash_leading_brief_starts_fine(root, tmp_path, fake_agents):
    for h in ("claude", "codex", "opencode"):
        d = run_with(root, tmp_path, experimenter=h, brief="---\ntitle: x\n---\n- goal: y\n", name=f"dash-{h}")
        agents.deliver_turn(d, ev(d, "stall", minutes=1.0)["seq"])
        assert agents.load(d)["experimenter"]["started"] is True, (d / "wake.log").read_text()


def test_manager_delivery_uses_resume(root, tmp_path, fake_agents):
    fake_agents.add_session("mgr-7")
    d = run_with(root, tmp_path, manager="codex:mgr-7")
    e = ev(d, "exit", code=0, check=None, ok=True)
    agents.deliver_turn(d, e["seq"])
    [call] = fake_agents()
    assert call["argv"] == harness.resume("codex", "mgr-7", "", model=model_for("codex", "frontier"), in_git=False)[0]
    assert call["prompt"].startswith("[labctl wake] source=supervisor")


def test_codex_active_writer_falls_back_to_queue(root, tmp_path, fake_agents, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "busy")
    d = run_with(root, tmp_path, manager="codex:thr-1")
    e = ev(d, "exit", code=0, check=None, ok=True)
    agents.deliver_turn(d, e["seq"])
    resume, queue = fake_agents()
    assert resume["argv"][:3] == ["codex", "exec", "resume"]
    assert queue["argv"][:4] == ["codex", "queue", "--thread", "thr-1"]
    assert queue["prompt"].startswith("[labctl wake] source=supervisor")
    assert not [e for e in runs.read_events(d) if e["event"] == "delivery_failed"]


# --- delivery is detached and serialized -----------------------------------

def test_slow_checkin_delivery_does_not_block_the_run(root, tmp_path, fake_agents, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "1.5")
    d = run_with(root, tmp_path, "sleep 0.4; echo done", experimenter="claude", checkin=[0.002])  # 0.12 s
    t0 = time.monotonic()
    supervise(d)
    assert time.monotonic() - t0 < 1.2
    assert [e["event"] for e in runs.read_events(d)] == ["checkin", "exit"]
    wait_until(lambda: fake_agents())
    assert "event=checkin minutes=0.002 " in fake_agents()[0]["prompt"]


def test_slow_agent_does_not_block_the_run(root, tmp_path, fake_agents, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "1.5")
    d = run_with(root, tmp_path, "echo boom; echo done", experimenter="claude", wake_on=["boom"])
    t0 = time.monotonic()
    supervise(d)
    assert time.monotonic() - t0 < 1.2
    assert runs.read_json(d / "status.json")["state"] == "succeeded"
    wait_until(lambda: len(fake_agents()) == 1)


def test_held_lock_delays_delivery(root, tmp_path, fake_agents, capsys):
    d = run_with(root, tmp_path, experimenter="claude", name="locked")
    rec = agents.load(d)["experimenter"]
    with agents.session_lock(agents.lock_path(d, "experimenter", rec)):
        wake.deliver(d, ev(d, "stall", minutes=1.0))
        time.sleep(0.6)
        assert fake_agents() == []
        main(["sessions", "locked"])
        assert "busy=yes" in capsys.readouterr().out
    wait_until(lambda: len(fake_agents()) == 1)


def test_manager_lock_is_shared_across_runs(root, tmp_path):
    a = run_with(root, tmp_path, manager="claude:m1", name="a")
    b = run_with(root, tmp_path, manager="claude:m1", name="b")
    rec = agents.load(a)["manager"]
    assert agents.lock_path(a, "manager", rec) == agents.lock_path(b, "manager", rec)
    assert agents.lock_path(a, "manager", rec).parent == root / ".sessions"


# --- escalate, sessions, attach --------------------------------------------

def test_escalate_wakes_wait_and_manager(root, tmp_path, fake_agents, capsys):
    fake_agents.add_session("mgr-1")
    d = run_with(root, tmp_path, experimenter="claude", manager="claude:mgr-1", name="esc")
    runs.write_json(d / "status.json", {"state": "running", "supervisor_pid": os.getpid()})
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("rc", main(["wait", "esc", "--poll", "0.05"])))
    t.start()
    time.sleep(0.3)
    assert t.is_alive()
    assert main(["escalate", "esc", "-m", "OOM at step 40; lower batch size?"]) == 0
    t.join(timeout=5)
    assert out["rc"] == 0
    printed = capsys.readouterr().out
    assert "[labctl wake] source=experimenter run=esc event=escalation seq=1 " in printed
    [e] = runs.read_events(d)
    assert (e["source"], e["event"], e["message"]) == ("experimenter", "escalation",
                                                        "OOM at step 40; lower batch size?")
    assert wake.format_wake(d, e).splitlines()[1:] == ["  OOM at step 40; lower batch size?"]
    wait_until(lambda: len(fake_agents()) == 1)
    [call] = fake_agents()
    assert call["argv"][:4] == ["claude", "-p", "--resume", "mgr-1"]
    assert "OOM at step 40" in call["prompt"]
    [entry] = inbox(root)
    assert "event=escalation" in entry["text"]


def test_fix_request_goes_to_the_manager(root, tmp_path, fake_agents, capsys):
    fake_agents.add_session("mgr-2")
    d = run_with(root, tmp_path, experimenter="claude", manager="claude:mgr-2", name="fx")
    assert main(["escalate", "fx", "--fix", "-m", "clean/completion.json has complete=false\nfix the writer"]) == 0
    assert capsys.readouterr().out == "fix_request for fx seq=1\n"
    [e] = runs.read_events(d)
    assert (e["source"], e["event"]) == ("experimenter", "fix_request")
    [entry] = inbox(root)
    assert entry["text"].splitlines() == [
        f"[labctl wake] source=experimenter run=fx event=fix_request seq=1 inbox=1 time={e['time']}",
        "  clean/completion.json has complete=false", "  fix the writer"]
    wait_until(lambda: len(fake_agents()) == 1)
    assert fake_agents()[0]["argv"][:4] == ["claude", "-p", "--resume", "mgr-2"]
    assert main(["wait", "fx", "--after", "0"]) == 0
    assert "event=fix_request" in capsys.readouterr().out


def test_report_is_the_verdict_on_a_finished_run(root, tmp_path, fake_agents, capsys):
    d = run_with(root, tmp_path, experimenter="codex", name="rep")
    with pytest.raises(SystemExit, match="has not exited"):
        main(["report", "rep", "-m", "too early"])
    ev(d, "exit", code=0, check="passed", ok=True)
    assert main(["report", "rep", "-m", "all four stages complete\nchecked out/*/completion.json"]) == 0
    assert capsys.readouterr().out == "report for rep seq=2\n"
    [entry] = inbox(root)
    assert entry["text"].splitlines()[0].startswith(
        "[labctl wake] source=experimenter run=rep event=report code=0 check=passed seq=2 inbox=1 ")
    assert entry["text"].splitlines()[1:] == ["  all four stages complete", "  checked out/*/completion.json"]


def test_sessions_output(root, tmp_path, capsys):
    run_with(root, tmp_path, experimenter="opencode", manager="claude:mgr-1", name="s1")
    run_with(root, tmp_path, name="plain")
    main(["sessions"])
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    assert lines[0].split() == ["s1", "experimenter", "opencode", "-", "started=no", "busy=no"]
    assert lines[1].split() == ["s1", "manager", "claude", "mgr-1", "started=yes", "busy=no"]


def test_attach(root, tmp_path, fake_agents):
    d = run_with(root, tmp_path, experimenter="codex", manager="opencode:ses_m", name="att")
    with pytest.raises(SystemExit, match="no session yet: no event has woken"):
        main(["attach", "att"])
    assert main(["attach", "att", "manager"]) == 0
    agents.update(d, "experimenter", session_id="thread-9", started=True)
    assert main(["attach", "att"]) == 0
    manager, experimenter = fake_agents()
    assert manager["argv"] == ["opencode", "-s", "ses_m"]
    assert experimenter["argv"] == ["codex", "resume", "-m", model_for("codex", "cheap"), "thread-9"]
    assert experimenter["cwd"] == str(tmp_path)
    plain = run_with(root, tmp_path, name="plain")
    with pytest.raises(SystemExit, match="has no experimenter"):
        agents.attach(plain, "experimenter")


# --- command builders --------------------------------------------------------

def test_builders_pass_the_message_on_stdin():
    tools = ["--permission-mode", "dontAsk", "--allowedTools", "Bash(labctl:*)", "Bash(nvidia-smi:*)",
             "Read", "Grep", "Glob"]
    assert harness.start("claude", model="M", role="R", message="MSG", session_id="S") == (
        ["claude", "-p", "--session-id", "S", "--model", "M", "--output-format", "json",
         "--append-system-prompt", "R", *tools], "MSG")
    assert harness.resume("claude", "S", "MSG") == (
        ["claude", "-p", "--resume", "S", "--output-format", "json", *tools], "MSG")
    assert harness.attach("claude", "S") == ["claude", "--resume", "S"]

    argv, stdin = harness.start("codex", model="M", role="line1\nline2\n", message="MSG", in_git=True)
    assert (argv, stdin) == (["codex", "exec", "--json", "-m", "M", "-c",
                              "developer_instructions='''\nline1\nline2\n'''", "-s", "workspace-write",
                              "-c", "sandbox_workspace_write.network_access=true", "-"], "MSG")
    assert "--skip-git-repo-check" in harness.start("codex", model="M", role="R", message="MSG", in_git=False)[0]
    assert harness.resume("codex", "T", "MSG", model="M", in_git=False) == (
        ["codex", "exec", "resume", "--json", "-m", "M", "-c", 'sandbox_mode="workspace-write"',
         "-c", "sandbox_workspace_write.network_access=true", "--skip-git-repo-check", "T", "-"], "MSG")
    assert "--skip-git-repo-check" not in harness.resume("codex", "T", "MSG", model="M", in_git=True)[0]
    assert harness.attach("codex", "T", "M") == ["codex", "resume", "-m", "M", "T"]
    assert harness.codex_queue("T", "-MSG") == (["codex", "queue", "--thread", "T", "--message=-MSG"], "")
    assert len(harness.codex_queue("T", "x" * 500_000)[0][-1]) < harness.MAX_ARG

    assert harness.start("opencode", model="M", role="R", message="MSG") == (
        ["opencode", "run", "--format", "json", "-m", "M"], "R\n\nMSG")
    assert harness.resume("opencode", "S", "MSG") == (["opencode", "run", "--format", "json", "-s", "S"], "MSG")
    assert harness.attach("opencode", "S") == ["opencode", "-s", "S"]
    with pytest.raises(ValueError, match="role prompt"):
        harness.start("claude", model="M", role="r" * 200_000, message="MSG", session_id="S")


def test_output_parsers():
    assert harness.session_id_from("codex", 'noise\n{"type":"thread.started","thread_id":"T1"}\n') == "T1"
    assert harness.session_id_from("opencode", '{"type":"step_start","sessionID":"ses_1"}\n') == "ses_1"
    assert harness.session_id_from("codex", "") is None
    assert harness.session_id_from("claude", '{"type":"result","session_id":"S1","result":"x"}') == "S1"
    assert harness.reply_from("claude", '{"type":"result","is_error":false,"result":"hi"}') == ("hi", False)
    assert harness.reply_from("claude", '{"type":"result","is_error":true,"result":"limit"}') == ("limit", True)
    codex_out = ('{"type":"thread.started","thread_id":"T"}\n'
                 '{"type":"item.completed","item":{"id":"a","type":"agent_message","text":"one"}}\n'
                 '{"type":"item.completed","item":{"id":"b","type":"command_execution","command":"ls"}}\n'
                 '{"type":"item.completed","item":{"id":"c","type":"agent_message","text":"two"}}\n')
    assert harness.reply_from("codex", codex_out) == ("two", False)
    assert harness.reply_from("codex", '{"type":"turn.failed","error":{"message":"x"}}') == (None, True)
    oc_out = ('{"type":"text","sessionID":"s","part":{"type":"text","text":"first"}}\n'
              '{"type":"text","sessionID":"s","part":{"type":"text","text":"last"}}\n')
    assert harness.reply_from("opencode", oc_out) == ("last", False)
    assert harness.reply_from("opencode", "garbage") == (None, False)
    assert harness.session_not_found("No conversation found with session ID: x")


def test_fake_claude_parses_like_the_real_one(fake_agents):
    """A prompt after the variadic --allowedTools is swallowed; a '-' prompt is an option."""
    bad = ["claude", "-p", "--resume", "S", "--allowedTools", "Bash(labctl:*)", "Read", "MSG"]
    r = subprocess.run(bad, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert r.returncode == 1 and "Input must be provided" in r.stderr
    r = subprocess.run(["claude", "-p", "- goal: x", "--resume", "S"], capture_output=True, text=True)
    assert r.returncode == 1 and "unknown option" in r.stderr
    fake_agents.add_session("S")
    argv, stdin = harness.resume("claude", "S", "- goal: x")
    r = subprocess.run(argv, input=stdin, capture_output=True, text=True)
    assert r.returncode == 0 and fake_agents()[-1]["prompt"] == "- goal: x"


def test_in_git_repo(tmp_path):
    assert not agents.in_git_repo(tmp_path)
    assert agents.in_git_repo(os.path.dirname(__file__))


def test_failed_first_turn_is_attachable_and_status_keeps_the_outcome(root, tmp_path, fake_agents, capsys):
    d = run_with(root, tmp_path, experimenter="claude", name="half")
    agents.update(d, "experimenter", session_id="S-half")  # a first turn that created the session, then failed
    assert main(["attach", "half"]) == 0
    assert fake_agents()[-1]["argv"] == ["claude", "--resume", "S-half"]
    ev(d, "exit", code=1, check=None, ok=False)
    ev(d, "delivery_failed", source="labctl", role="experimenter", for_seq="1", message="exit 1")
    main(["status"])
    line = next(x for x in capsys.readouterr().out.splitlines() if x.startswith("half"))
    assert "exit code=1" in line and "[1 delivery failure]" in line


def test_first_message_names_the_run_and_its_commands(root, tmp_path, fake_agents):
    d = run_with(root, tmp_path, experimenter="opencode", name="cmds")
    agents.deliver_turn(d, ev(d, "stall", minutes=1.0)["seq"])
    prompt = fake_agents()[0]["prompt"]
    for cmd in ("labctl status cmds", "labctl tail cmds -n 100", "labctl cancel cmds", "labctl escalate cmds -m",
                "labctl escalate cmds --fix -m"):
        assert cmd in prompt
