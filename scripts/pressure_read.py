#!/usr/bin/env python3
"""Bounded, read-only O_DIRECT bursts over an existing authenticated SSH master."""
import argparse
import json
import shlex
import subprocess
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--control", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--path", required=True)
    parser.add_argument("--seconds", type=int, default=300)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 600:
        parser.error("pressure duration must be 1..600 seconds")
    prefix = ["ssh", "-S", args.control, "-o", "BatchMode=yes", args.target]
    read = f"dd if={shlex.quote(args.path)} iflag=direct,fullblock bs=65536 count=32 status=none"
    probe = subprocess.run(
        prefix + [read], stdout=subprocess.DEVNULL, capture_output=False
    )
    if probe.returncode:
        raise SystemExit("O_DIRECT read probe failed; workload was not started")
    command = "set -e; i=0; while [ $i -lt 8 ]; do " + read + "; i=$((i+1)); done"
    report = {
        "scope": "read-only O_DIRECT file reads encrypted over SSH while recording",
        "path": args.path,
        "bytes_per_completed_burst": 8 * 32 * 65536,
        "requested_seconds": args.seconds,
        "bursts": [],
    }
    started = time.monotonic()
    while time.monotonic() - started < args.seconds:
        begin = time.monotonic()
        result = subprocess.run(
            prefix + [command],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        report["bursts"].append(
            {
                "elapsed_seconds": begin - started,
                "duration_seconds": time.monotonic() - begin,
                "returncode": result.returncode,
                "stderr": result.stderr.decode(errors="replace")[:1000],
            }
        )
        if result.returncode:
            break
        time.sleep(min(1, max(0, args.seconds - (time.monotonic() - started))))
    report["elapsed_seconds"] = time.monotonic() - started
    report["completed_bursts"] = sum(b["returncode"] == 0 for b in report["bursts"])
    report["read_bytes"] = (
        report["completed_bursts"] * report["bytes_per_completed_burst"]
    )
    report["passed"] = bool(report["bursts"]) and all(
        b["returncode"] == 0 for b in report["bursts"]
    )
    args.output.write_text(json.dumps(report, indent=2))
    print({k: v for k, v in report.items() if k != "bursts"}, flush=True)
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
