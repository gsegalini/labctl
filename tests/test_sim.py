"""The fake campaign under the real supervisor (no agents)."""

import sys
from pathlib import Path

from conftest import events, make_run, status, supervise

from labctl import runs

CAMPAIGN = Path(__file__).parent / "sim" / "fake_campaign.py"


def campaign_run(root, *args, **kw):
    out = root.parent / "out"
    cmd = [sys.executable, str(CAMPAIGN), "run", "--out", str(out), "--steps", "6", "--duration", "0.3", *args]
    check = f"{sys.executable} {CAMPAIGN} check --out {out}"
    return runs.create_run(root, cmd, cwd=root.parent, check=check, **kw)


def test_clean_campaign_succeeds(root):
    d = campaign_run(root)
    supervise(d)
    assert status(d)["state"] == "succeeded" and events(d, "exit")[0]["check"] == "passed"
    assert "replay 6/6 key=replay-0006" in (d / "log").read_text()


def test_failure_nan_and_tqdm(root):
    d = campaign_run(root, "--tqdm", "--nan-at", "dependence", "--fail-at", "swap", wake_on=["loss=nan", "^ValueError"])
    supervise(d)
    log = (d / "log").read_text()
    assert log.splitlines()[0] == "clean 6/6 key=clean-0006 loss=0.1667"  # only the last \r update is logged
    assert log.rstrip().endswith("ValueError: frozen overlay changed")
    assert [m["pattern"] for m in events(d, "match")] == ["loss=nan", "^ValueError"]
    [ev] = events(d, "exit")
    assert (ev["code"], ev["check"]) == (1, None) and status(d)["state"] == "failed"


def test_stall_and_check_failure(root):
    d = campaign_run(root, "--stall-at", "clean", "0.6", stall=0.005)
    supervise(d)
    assert [e["event"] for e in events(d)] == ["stall", "exit"]
    (root.parent / "out" / "swap" / "completion.json").unlink()
    d2 = make_run(root, f"{sys.executable} {CAMPAIGN} check --out {root.parent / 'out'}")
    supervise(d2)
    assert "incomplete stages: swap" in (d2 / "log").read_text() and status(d2)["state"] == "failed"


def test_bad_output_is_caught_by_the_check(root):
    d = campaign_run(root, "--bad-output-at", "clean")
    supervise(d)
    assert "incomplete stages: clean" in (d / "log").read_text() and status(d)["state"] == "failed"
