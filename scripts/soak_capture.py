#!/usr/bin/env python3
import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path


def summarize_media(media):
    result = {}
    for kind in ("video", "audio"):
        items = sorted(
            (item for item in media if item["kind"] == kind),
            key=lambda item: item["start_us"],
        )
        gaps = [
            right["start_us"] - left["end_us"] for left, right in zip(items, items[1:])
        ]
        result[kind] = {
            "segments": len(items),
            "bytes": sum(item["size_bytes"] for item in items),
            "duration_seconds": sum(
                (item["end_us"] - item["start_us"]) / 1e6 for item in items
            ),
            "first_us": items[0]["start_us"] if items else None,
            "last_us": items[-1]["end_us"] if items else None,
            "maximum_gap_us": max([0] + gaps),
            "maximum_overlap_us": max([0] + [-gap for gap in gaps]),
            "discontinuous_segments": sum(
                bool(item["discontinuity"]) for item in items
            ),
        }
    return result


def echo_samples(path):
    if not path or not path.exists():
        return []
    result = []
    for line in path.read_text().splitlines():
        fields = line.split("\t", 2)
        if len(fields) != 3:
            continue
        try:
            metrics = {
                key.rstrip(":"): int(value)
                for key, value in (part.split("=", 1) for part in fields[1].split())
            }
            result.append(
                {
                    "source_seconds": float(fields[0]),
                    "metrics": metrics,
                    "state": json.loads(fields[2]),
                }
            )
        except (ValueError, json.JSONDecodeError):
            continue
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hub", default="http://127.0.0.1:18084")
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--device-id", default="echo-mate")
    parser.add_argument("--seconds", type=int, required=True)
    parser.add_argument("--poll-seconds", type=int, default=10)
    parser.add_argument("--mark-seconds", type=int, default=300)
    parser.add_argument("--title", default="SD 连续录制验证（自动化）")
    parser.add_argument("--echo-metrics", type=Path)
    parser.add_argument(
        "--stop-file",
        type=Path,
        help="optional operator stop signal; the path must not exist initially",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.stop_file and args.stop_file.exists():
        parser.error("stop signal already exists; choose a new path")
    if not 15 <= args.seconds <= 4 * 3600 or not 1 <= args.poll_seconds <= 30:
        parser.error(
            "duration must be 15 seconds to 4 hours; polling must be 1..30 seconds"
        )
    token = args.token_file.read_text().strip() if args.token_file else ""

    def api(method, path, data=None):
        request = urllib.request.Request(
            args.hub + path,
            method=method,
            data=json.dumps(data).encode() if data is not None else None,
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)

    status = api("GET", "/api/status")
    device = next(
        item for item in status["devices"] if item["device_id"] == args.device_id
    )
    project = api("POST", "/api/projects", {"name": args.title})
    report = {
        "synthetic": False,
        "automated_test": True,
        "quality_evaluation": False,
        "synchronization_measured": False,
        "requested_seconds": args.seconds,
        "samples": [],
        "markers": [],
        "connection_interruptions": [],
        "project": project,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    session = None
    failure = None
    started = time.monotonic()
    next_mark = 5
    outage = None

    def persist():
        temporary = args.output.with_suffix(".part")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        temporary.replace(args.output)

    try:
        session = api(
            "POST",
            "/api/sessions",
            {
                "project_id": project["id"],
                "device_id": device["device_id"],
                "boot_id": device["boot_id"],
                "source_time_us": device["source_time_us"],
                "title": args.title,
            },
        )
        report["session"] = session
        started = time.monotonic()
        print(
            json.dumps(
                {
                    "event": "recording_started",
                    "session_id": session["id"],
                    "duration": args.seconds,
                }
            ),
            flush=True,
        )
        while time.monotonic() - started < args.seconds and not (
            args.stop_file and args.stop_file.exists()
        ):
            elapsed = time.monotonic() - started
            source_samples = echo_samples(args.echo_metrics)
            try:
                status = api("GET", "/api/status")
                timeline = api("GET", "/api/sessions/" + session["id"] + "/timeline")
                if outage is not None:
                    report["connection_interruptions"].append(
                        {"start_elapsed": outage, "end_elapsed": elapsed}
                    )
                    outage = None
                connected = next(
                    (
                        item
                        for item in status["devices"]
                        if item["device_id"] == args.device_id
                    ),
                    None,
                )
                sample = {
                    "elapsed_seconds": round(elapsed, 3),
                    "hub_MemAvailable": status["memory"].get("MemAvailable"),
                    "state": timeline["session"]["state"],
                    "connected": bool(connected),
                    "media": summarize_media(timeline["media"]),
                    "hub_errors": status["errors"],
                }
                if source_samples:
                    sample["echo"] = source_samples[-1]
                report["samples"].append(sample)
                if timeline["session"]["state"] not in ("recording", "interrupted"):
                    raise RuntimeError(
                        "capture left recording state: " + timeline["session"]["state"]
                    )
                if source_samples:
                    latest = source_samples[-1]
                    if (latest["state"].get("session") or {}).get("id") == session[
                        "id"
                    ] and latest["state"].get("state") != "recording":
                        raise RuntimeError("source capture is no longer recording")
                    if latest["metrics"].get("MemAvailable", 999999) < 12 * 1024:
                        raise RuntimeError(
                            "source memory fell below the recording safety reserve"
                        )
                if elapsed >= next_mark and connected:
                    stamp = time.monotonic()
                    ack = api(
                        "POST",
                        "/api/sessions/" + session["id"] + "/mark",
                        {
                            "note": f"自动化稳定性标记，约第 {elapsed:.0f} 秒；不属于真实操作质量题集。"
                        },
                    )
                    report["markers"].append(
                        {
                            "elapsed_seconds": elapsed,
                            "roundtrip_ms": (time.monotonic() - stamp) * 1000,
                            "ack": ack,
                        }
                    )
                    next_mark = elapsed + args.mark_seconds
                print(
                    json.dumps(
                        {
                            "event": "sample",
                            "elapsed": round(elapsed),
                            "state": sample["state"],
                            "video_segments": sample["media"]["video"]["segments"],
                            "audio_segments": sample["media"]["audio"]["segments"],
                            "hub_MemAvailable_MiB": (
                                round(sample["hub_MemAvailable"] / 1024 / 1024, 1)
                                if sample["hub_MemAvailable"] is not None
                                else None
                            ),
                            "echo_MemAvailable_kB": (
                                source_samples[-1]["metrics"].get("MemAvailable")
                                if source_samples
                                else None
                            ),
                        }
                    ),
                    flush=True,
                )
            except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
                if outage is None:
                    outage = elapsed
                report["samples"].append(
                    {
                        "elapsed_seconds": elapsed,
                        "hub_unavailable": str(error),
                        "echo": source_samples[-1] if source_samples else None,
                    }
                )
                print(
                    json.dumps({"event": "hub_unavailable", "elapsed": round(elapsed)}),
                    flush=True,
                )
            persist()
            time.sleep(
                max(
                    0,
                    min(args.poll_seconds, args.seconds - (time.monotonic() - started)),
                )
            )
    except BaseException as error:
        failure = str(error)
        report["failure"] = failure
    finally:
        report["elapsed_seconds"] = time.monotonic() - started
        report["ended_by_stop_signal"] = bool(
            args.stop_file and args.stop_file.exists()
        )
        if session:
            for attempt in range(6):
                try:
                    report["stopped"] = api(
                        "PATCH",
                        "/api/sessions/" + session["id"] + "/state",
                        {
                            "state": "stopped",
                            "source_time_us": device["source_time_us"],
                        },
                    )
                    break
                except Exception as error:
                    report["stop_error"] = str(error)
                    time.sleep(2)
            try:
                time.sleep(1)
                report["timeline"] = api(
                    "GET", "/api/sessions/" + session["id"] + "/timeline"
                )
                report["media_summary"] = summarize_media(report["timeline"]["media"])
            except Exception as error:
                report["timeline_error"] = str(error)
        source_samples = echo_samples(args.echo_metrics)
        if source_samples:
            relevant = (
                [
                    sample
                    for sample in source_samples
                    if (sample["state"].get("session") or {}).get("id") == session["id"]
                    and sample["state"].get("state") == "recording"
                ]
                if session
                else []
            )
            report["echo_samples"] = len(relevant)
            if relevant:
                report["minimum_echo_MemAvailable_kB"] = min(
                    sample["metrics"]["MemAvailable"] for sample in relevant
                )
        available = [
            sample["hub_MemAvailable"]
            for sample in report["samples"]
            if sample.get("hub_MemAvailable") is not None
        ]
        report["minimum_hub_MemAvailable"] = min(available) if available else None
        report["completed_duration"] = (
            failure is None
            and report["elapsed_seconds"] >= args.seconds
            and "stopped" in report
        )
        report["completed_capture"] = (
            failure is None
            and report.get("stopped", {}).get("state") == "stopped"
            and (report["completed_duration"] or report["ended_by_stop_signal"])
        )
        persist()
    print(args.output, flush=True)
    if failure or not report["completed_capture"]:
        raise SystemExit(failure or "recording did not stop cleanly")


if __name__ == "__main__":
    main()
