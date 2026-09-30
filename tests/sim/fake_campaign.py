"""A fake experiment campaign for exercising labctl without a GPU.

    fake_campaign.py run --out DIR [--steps N] [--duration SECONDS] [--tqdm]
                         [--fail-at STAGE] [--stall-at STAGE SECONDS] [--nan-at STAGE]
                         [--bad-output-at STAGE]
    fake_campaign.py check --out DIR

`run` goes through the stages clean, dependence, swap, replay. Each prints
"<stage> i/N key=<stage>-<i>" progress lines (with --tqdm: '\\r' progress
updates, one line per stage) spread over --duration seconds, and writes
DIR/<stage>/completion.json with "complete": true. Faults are injected halfway
through a stage: --fail-at prints a traceback ending in
"ValueError: frozen overlay changed" and exits 1; --stall-at goes silent for
SECONDS; --nan-at prints a progress line with "loss=nan" and carries on;
--bad-output-at writes that stage's completion.json with "complete": false and
carries on, like a silent bug.
`check` (for labctl run --check) exits 0 only if every stage is complete.
"""

import argparse
import json
import sys
import time
from pathlib import Path

STAGES = ("clean", "dependence", "swap", "replay")
TRACEBACK = """Traceback (most recent call last):
  File "campaign/run.py", line 212, in run_stage
    verify_overlay(overlay, frozen_digest)
  File "campaign/overlay.py", line 48, in verify_overlay
    raise ValueError("frozen overlay changed")
ValueError: frozen overlay changed"""


def run(args) -> int:
    pause = args.duration / (len(STAGES) * args.steps)
    for stage in STAGES:
        for i in range(1, args.steps + 1):
            if i == args.steps // 2 + 1:
                if stage == args.fail_at:
                    print(TRACEBACK, file=sys.stderr, flush=True)
                    return 1
                if args.stall_at and stage == args.stall_at[0]:
                    time.sleep(float(args.stall_at[1]))
                if stage == args.nan_at:
                    print(f"{stage} {i}/{args.steps} key={stage}-{i:04d} loss=nan", flush=True)
                    continue
            line = f"{stage} {i}/{args.steps} key={stage}-{i:04d} loss={1 / i:.4f}"
            print(line, end="\r" if args.tqdm and i < args.steps else "\n", flush=True)
            time.sleep(pause)
        done = Path(args.out) / stage
        done.mkdir(parents=True, exist_ok=True)
        complete = stage != args.bad_output_at
        (done / "completion.json").write_text(json.dumps({"stage": stage, "complete": complete, "steps": args.steps}))
    print("campaign done", flush=True)
    return 0


def check(args) -> int:
    missing = []
    for stage in STAGES:
        try:
            ok = json.loads((Path(args.out) / stage / "completion.json").read_text()).get("complete") is True
        except (OSError, ValueError):
            ok = False
        if not ok:
            missing.append(stage)
    if missing:
        print(f"incomplete stages: {', '.join(missing)}")
        return 1
    print("all stages complete")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="mode", required=True)
    r = sub.add_parser("run")
    r.add_argument("--out", required=True)
    r.add_argument("--steps", type=int, default=20)
    r.add_argument("--duration", type=float, default=8.0, help="total seconds of progress output")
    r.add_argument("--tqdm", action="store_true")
    r.add_argument("--fail-at", choices=STAGES)
    r.add_argument("--stall-at", nargs=2, metavar=("STAGE", "SECONDS"))
    r.add_argument("--nan-at", choices=STAGES)
    r.add_argument("--bad-output-at", choices=STAGES)
    c = sub.add_parser("check")
    c.add_argument("--out", required=True)
    args = p.parse_args(argv)
    return run(args) if args.mode == "run" else check(args)


if __name__ == "__main__":
    sys.exit(main())
