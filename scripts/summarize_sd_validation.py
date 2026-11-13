#!/usr/bin/env python3
"""Summarize measured storage/recording evidence without claiming AV synchronization."""
import argparse
import json
from pathlib import Path
from soak_capture import summarize_media


def summarize(run, audit, storage, outage):
    original = {item["id"]: item for item in audit["media"]}
    indexed = {item["id"]: item for item in run["timeline"]["media"]}
    fields = ("kind", "start_us", "end_us", "size_bytes", "sha256", "discontinuity")
    matching = (
        len(original) == len(audit["media"])
        and len(indexed) == len(run["timeline"]["media"])
        and set(original) == set(indexed)
        and all(
            all(original[key].get(field) == indexed[key].get(field) for field in fields)
            for key in original
            if key in indexed
        )
    )
    video = [item for item in audit["media"] if item["kind"] == "video"]
    audio = [item for item in audit["media"] if item["kind"] == "audio"]
    video_time = sum(item.get("duration_us", 0) for item in video) / 1e6
    frames = sum(item.get("frames", 0) for item in video)
    marker_ms = sorted(item["roundtrip_ms"] for item in run["markers"])
    echo_min = run.get("minimum_echo_MemAvailable_kB")
    hub_min = run.get("minimum_hub_MemAvailable")
    summary = {
        "scope": "SD recording, original integrity, indexing and Hub service reconnection; not full project acceptance",
        "session_id": run["session"]["id"],
        "storage": storage["storage"],
        "storage_probe": storage["write_probe"],
        "completed_duration": run["completed_duration"],
        "requested_seconds": run["requested_seconds"],
        "elapsed_seconds": run["elapsed_seconds"],
        "original_integrity_passed": audit["passed"] and audit["sha256_checked"],
        "source_and_index_metadata_match": matching,
        "media": summarize_media(audit["media"]),
        "video_frames_in_sample_tables": frames,
        "video_average_fps": frames / video_time if video_time else None,
        "all_video_1080p": bool(video)
        and all(
            (item.get("width"), item.get("height")) == (1920, 1080) for item in video
        ),
        "sample_intervals_over_60ms": sum(
            item.get("sample_intervals_over_60ms", 0) for item in video
        ),
        "video_duration_difference_maximum_us": max(
            (abs(item.get("duration_difference_us", 0)) for item in video), default=None
        ),
        "audio_samples": sum(item.get("samples", 0) for item in audio),
        "audio_duration_difference_maximum_us": max(
            (abs(item.get("duration_difference_us", 0)) for item in audio), default=None
        ),
        "minimum_echo_MemAvailable_MiB": (
            echo_min / 1024 if echo_min is not None else None
        ),
        "minimum_hub_MemAvailable_MiB": (
            hub_min / 1024**2 if hub_min is not None else None
        ),
        "echo_memory_samples": run.get("echo_samples"),
        "http_marker_roundtrip_samples": len(marker_ms),
        "http_marker_roundtrip_p95_ms": (
            marker_ms[max(0, (len(marker_ms) * 95 + 99) // 100 - 1)]
            if marker_ms
            else None
        ),
        "http_marker_scope": "HTTP command roundtrip through SSH tunnels; not physical pedal feedback",
        "service_outage": outage,
        "synchronization_measured": False,
        "physical_usb_disconnect_tested": False,
        "full_video_decode": False,
        "quality_evaluation": False,
    }
    summary["recording_storage_gate_passed"] = (
        summary["completed_duration"]
        and summary["elapsed_seconds"] >= 3600
        and summary["original_integrity_passed"]
        and matching
        and summary["all_video_1080p"]
        and summary["video_average_fps"] is not None
        and 24.9 <= summary["video_average_fps"] <= 25.1
        and echo_min is not None
        and echo_min >= 20 * 1024
        and hub_min is not None
        and hub_min >= 512 * 1024**2
    )
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ("run", "audit", "storage", "outage", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    report = summarize(
        *(
            json.loads(path.read_text())
            for path in (args.run, args.audit, args.storage, args.outage)
        )
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(args.output)
