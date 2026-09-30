import os
import signal
import subprocess

from conftest import events, make_run, start, status, supervise, wait_until

from labctl import runs, supervisor, wake


def test_success(root):
    d = make_run(root, "echo hello; echo world >&2")
    supervise(d)
    assert (d / "log").read_text() == "hello\nworld\n"
    assert (d / "exit-code").read_text().strip() == "0"
    [ev] = events(d)
    assert ev["seq"] == 1 and ev["source"] == "supervisor" and ev["run"] == d.name
    assert (ev["event"], ev["code"], ev["check"], ev["ok"]) == ("exit", 0, None, True)
    st = status(d)
    assert st["state"] == "succeeded" and st["started"] and st["finished"]


def test_failure_exit_code(root):
    d = make_run(root, "echo oops; exit 3")
    supervise(d)
    [ev] = events(d, "exit")
    assert ev["code"] == 3 and ev["ok"] is False
    assert (d / "exit-code").read_text().strip() == "3"
    assert status(d)["state"] == "failed"


def test_check_passes_with_run_env(root):
    d = make_run(root, "echo done", check='test -f "$LABCTL_RUN_DIR/log" && test "$LABCTL_RUN_ID" = chk',
                 name="chk")
    supervise(d)
    [ev] = events(d, "exit")
    assert ev["check"] == "passed" and ev["ok"] is True
    assert status(d)["state"] == "succeeded" and status(d)["check_code"] == 0


def test_check_fails(root):
    d = make_run(root, "echo done", check="echo bad result; exit 2")
    supervise(d)
    [ev] = events(d, "exit")
    assert (ev["code"], ev["check"], ev["ok"]) == (0, "failed", False)
    assert status(d)["state"] == "failed" and status(d)["check_code"] == 2
    assert "bad result" in (d / "log").read_text()


def test_check_skipped_on_failure(root):
    d = make_run(root, "exit 1", check="touch ran")
    supervise(d)
    assert events(d, "exit")[0]["check"] is None
    assert not (root.parent / "ran").exists()


def test_wake_on_match(root):
    d = make_run(root, "echo step 1; echo step 3 done; echo step 4", wake_on=[r"step 3", r"nomatch"])
    supervise(d)
    [m] = events(d, "match")
    assert m["pattern"] == "step 3" and m["line"] == "step 3 done" and m["seq"] == 1
    assert events(d)[-1]["event"] == "exit"


def test_carriage_return_progress(root):
    script = r"printf 'load 10%%\rload 50%%\r'; sleep 0.2; printf 'load 100%%\r\nnext\n'"
    d = make_run(root, script, wake_on=["50%", "100%"])
    supervise(d)
    assert [(m["pattern"], m["line"]) for m in events(d, "match")] == [("50%", "load 50%"),
                                                                      ("100%", "load 100%")]
    assert (d / "log").read_text() == "load 100%\nnext\n"


def test_unterminated_output_is_capped(root):
    d = make_run(root, "head -c 300000 /dev/zero | tr '\\0' x; echo; echo end")
    supervise(d)
    first, second = (d / "log").read_text().splitlines()
    assert first == "x" * supervisor.MAX_PENDING and second == "end"


def test_stall_fires_once_per_silence(root):
    # 0.003 min = 0.18 s; two silences of 0.8 s -> exactly two stall events
    d = make_run(root, "echo a; sleep 0.8; echo b; sleep 0.8; echo c", stall=0.003)
    supervise(d)
    assert [e["event"] for e in events(d)] == ["stall", "stall", "exit"]
    assert events(d, "stall")[0]["minutes"] == 0.003


def test_checkin_fires_once_at_its_time(root):
    d = make_run(root, "echo a; sleep 1", checkin=[0.005])  # 0.3 s
    supervise(d)
    [c, _] = events(d)
    assert (c["event"], c["source"], c["minutes"]) == ("checkin", "supervisor", 0.005)
    assert 0.3 <= c["elapsed"] < 0.6


def test_checkins_fire_in_order_even_with_continuous_output(root):
    d = make_run(root, "i=0; while [ $i -lt 40 ]; do i=$((i+1)); echo $i; sleep 0.03; done",
                 checkin=[0.01, 0.004])  # 0.6 s and 0.24 s; the output never pauses long enough to time out
    supervise(d)
    assert [(e["event"], e.get("minutes")) for e in events(d)] == [("checkin", 0.004), ("checkin", 0.01),
                                                                   ("exit", None)]
    assert [round(e["elapsed"], 1) >= e["minutes"] * 60 - 0.05 for e in events(d, "checkin")] == [True, True]


def test_no_checkin_after_the_command_exits(root):
    d = make_run(root, "echo quick", checkin=[0.005])
    supervise(d)
    assert [e["event"] for e in events(d)] == ["exit"]
    # a daemonized grandchild keeps the pipe open for the drain second: still no check-in
    d = make_run(root, "sleep 3 & echo main done", checkin=[0.005])
    supervise(d)
    assert [e["event"] for e in events(d)] == ["exit"]


def test_checkin_clock_starts_with_the_command_not_the_queue(root):
    a = make_run(root, "sleep 1", slot="gpu")
    b = make_run(root, "sleep 0.6", slot="gpu", checkin=[0.005])  # 0.3 s after b starts
    pa = start(a)
    wait_until(lambda: status(a).get("state") == "running")
    pb = start(b)
    assert pa.wait(timeout=10) == 0 and pb.wait(timeout=10) == 0
    [c] = events(b, "checkin")
    assert c["time"] >= status(b)["started"] >= status(a)["finished"]
    assert 0.3 <= c["elapsed"] < 0.6


def test_checkin_and_stall_due_together(root):
    d = make_run(root, "echo a; sleep 1", stall=0.005, checkin=[0.005])
    supervise(d)
    assert sorted(e["event"] for e in events(d)) == ["checkin", "exit", "stall"]


def test_slot_runs_serially(root):
    a = make_run(root, "echo a; sleep 0.6", slot="gpu")
    b = make_run(root, "echo b; sleep 0.1", slot="gpu")
    pa = start(a)
    wait_until(lambda: status(a).get("state") == "running")
    pb = start(b)
    wait_until(lambda: status(b).get("state") == "queued")
    assert status(a)["state"] == "running"
    assert pa.wait(timeout=10) == 0 and pb.wait(timeout=10) == 0
    assert status(b)["started"] >= status(a)["finished"]
    assert status(a)["state"] == status(b)["state"] == "succeeded"


def test_cancel_kills_process_group(root):
    d = make_run(root, "sleep 100 & echo $! > bg.pid; echo started; wait")
    p = start(d)
    pidfile = root.parent / "bg.pid"
    wait_until(lambda: pidfile.exists() and pidfile.read_text().strip())
    bg = int(pidfile.read_text())
    assert runs.pid_alive(bg)
    supervisor.cancel(d, grace=2)
    assert p.wait(timeout=10) == 0
    wait_until(lambda: not runs.pid_alive(bg))
    assert status(d)["state"] == "cancelled"
    [ev] = events(d, "exit")
    assert ev["cancelled"] is True and ev["ok"] is False


def test_cancel_while_queued(root):
    a = make_run(root, "sleep 30", slot="s")
    b = make_run(root, "touch ran", slot="s")
    pa = start(a)
    wait_until(lambda: status(a).get("state") == "running")
    pb = start(b)
    wait_until(lambda: status(b).get("state") == "queued")
    supervisor.cancel(b)
    assert pb.wait(timeout=10) == 0
    assert status(b)["state"] == "cancelled"
    assert events(b, "exit")[0]["code"] is None
    supervisor.cancel(a)
    assert pa.wait(timeout=10) == 0
    assert not (root.parent / "ran").exists()


def test_cancel_with_dead_supervisor(root):
    d = make_run(root, "true")
    child = subprocess.Popen(["sleep", "100"], start_new_session=True)
    dead = subprocess.Popen(["true"])
    dead.wait()
    runs.write_json(d / "status.json", {"state": "running", "supervisor_pid": dead.pid, "child_pid": child.pid})
    supervisor.cancel(d, grace=0.5)  # our unreaped child stays in its group, so SIGKILL follows
    assert child.wait(timeout=5) == -signal.SIGTERM
    assert status(d)["state"] == "cancelled"
    assert events(d, "exit")[0]["cancelled"] is True


def test_deliver_failure_does_not_crash(root, monkeypatch):
    def boom(run_dir, event):
        raise RuntimeError("boom")

    monkeypatch.setattr(wake, "deliver", boom)
    d = make_run(root, "true")
    ev = supervisor.emit(d, "match", pattern="x", line="x")
    assert ev["seq"] == 1


def test_format_wake(root):
    d = make_run(root, "true")
    (d / "log").write_text("".join(f"line {i}\n" for i in range(50)))
    ev = runs.append_event(d, "exit", code=1, check=None, ok=False)
    text = wake.format_wake(d, ev).splitlines()
    assert text[0].startswith(f"[labctl wake] source=supervisor run={d.name} event=exit code=1 check=none ")
    assert f"time={ev['time']}" in text[0]
    assert text[1:] == [f"  line {i}" for i in range(10, 50)]
    stall = runs.append_event(d, "stall", minutes=0.5)
    assert "event=stall minutes=0.5 " in wake.header(stall)
    checkin = runs.append_event(d, "checkin", minutes=5.0, elapsed=300.2)
    assert wake.format_wake(d, checkin).splitlines()[0] == (
        f"[labctl wake] source=supervisor run={d.name} event=checkin minutes=5 seq={checkin['seq']} "
        f"time={checkin['time']}")
    assert wake.format_wake(d, checkin).splitlines()[-1] == "  line 49"  # followed by the log tail
    fix = runs.append_event(d, "fix_request", source="experimenter", message="wrong output")
    assert wake.header(fix).startswith(f"[labctl wake] source=experimenter run={d.name} event=fix_request seq=")
    other = runs.append_event(d, "note", source="experimenter", text="look")
    assert wake.header(other).startswith(f"[labctl wake] source=experimenter run={d.name} event=note text=look ")


def test_tail_reads_last_lines(tmp_path):
    f = tmp_path / "log"
    f.write_text("".join(f"{i}\n" for i in range(100000)))
    assert runs.tail(f, 3) == ["99997", "99998", "99999"]
    assert runs.tail(tmp_path / "missing", 3) == []


def test_pid_alive():
    assert runs.pid_alive(os.getpid())
    assert not runs.pid_alive(None)
