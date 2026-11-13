#!/usr/bin/env python3
import argparse
import json
import os
import subprocess
import time
from pathlib import Path

from check_storage import mounted_storage


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mount", type=Path, required=True)
    parser.add_argument("--device", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--hub", required=True)
    parser.add_argument("--limit-seconds", type=int, default=3900)
    parser.add_argument(
        "--edge", type=Path, default=Path("/root/echotrace/bin/echotrace-edge")
    )
    parser.add_argument(
        "--terminal", type=Path, default=Path("/root/echotrace/bin/echotrace-terminal")
    )
    parser.add_argument("--socket", default="/run/echotrace-sd.sock")
    args = parser.parse_args()
    if not 30 <= args.limit_seconds <= 86400:
        parser.error("validation process limit must be between 30 seconds and one day")
    storage = mounted_storage(args.mount, args.device)
    data = args.data.resolve()
    if (
        not data.is_relative_to(args.mount.resolve())
        or data.stat().st_dev != args.mount.stat().st_dev
    ):
        raise ValueError("capture data is not on the checked SD filesystem")
    configuration = json.loads(args.config.read_text())
    if configuration.get("reserve_mb", 0) < 256:
        raise ValueError("SD validation requires at least 256 MiB recording reserve")
    pidfile = Path("/run/echotrace-sd-validation.json")
    if pidfile.exists():
        previous = json.loads(pidfile.read_text())
        process = Path("/proc") / str(previous["capture_pid"])
        if process.exists() and (process / "exe").resolve() == args.edge.resolve():
            raise ValueError("an SD validation capture daemon is already running")
    directory = data / "validation"
    directory.mkdir(exist_ok=True)
    stamp = str(time.monotonic_ns() // 1000)
    environment = {
        **os.environ,
        "LD_LIBRARY_PATH": "/root/echotrace/lib:/oem/usr/lib:/usr/lib:/lib",
    }
    child = None
    terminal = None
    try:
        with (directory / ("capture-" + stamp + ".log")).open("xb") as log:
            child = subprocess.Popen(
                [
                    str(args.edge),
                    "--data",
                    str(data),
                    "--config",
                    str(args.config),
                    "--socket",
                    args.socket,
                    "--hub",
                    args.hub,
                ],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=environment,
            )
        time.sleep(0.5)
        if child.poll() is not None:
            raise RuntimeError("capture daemon exited before readiness")
        ticks = Path(f"/proc/{child.pid}/stat").read_text().split()[21]
        # The shell watchdog reads /proc/uptime (CLOCK_BOOTTIME), including
        # suspend. Build its deadline from that same clock.
        deadline = (
            int(float(Path("/proc/uptime").read_text().split()[0])) + args.limit_seconds
        )
        watchdog = subprocess.Popen(
            [
                "/bin/sh",
                str(Path(__file__).with_name("bound_capture.sh")),
                str(child.pid),
                ticks,
                str(deadline),
                str(args.edge.resolve()),
                str(directory / ("deadline-" + stamp + ".log")),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        with (directory / ("terminal-" + stamp + ".log")).open("xb") as log:
            terminal = subprocess.Popen(
                [str(args.terminal), "--socket", args.socket],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=environment,
            )
        report = {
            "capture_pid": child.pid,
            "terminal_pid": terminal.pid,
            "watchdog_pid": watchdog.pid,
            "capture_start_ticks": ticks,
            "deadline_uptime_seconds": deadline,
            "data": str(data),
            "storage": storage,
        }
        pidfile.write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2))
    except BaseException:
        if terminal and terminal.poll() is None:
            terminal.terminate()
        if child and child.poll() is None:
            child.terminate()
        raise


if __name__ == "__main__":
    main()
