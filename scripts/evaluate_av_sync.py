#!/usr/bin/env python3
"""Measure manually paired physical events in an existing evidence export.

Frame indices are in presentation order, starting at zero; sample indices refer
to the original mono WAV. No frame rate or container start time is assumed.
"""
import argparse
import hashlib
import json
import math
import subprocess
import tempfile
import wave
import zipfile
from decimal import Decimal
from pathlib import Path


IDENTITY = (
    "id",
    "session_id",
    "device_id",
    "boot_id",
    "kind",
    "start_us",
    "end_us",
    "size_bytes",
    "sha256",
    "discontinuity",
)


def frame_times(path, ffprobe="ffprobe"):
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_frames",
            "-show_entries",
            "frame=best_effort_timestamp_time",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    if result.stderr.strip():
        raise ValueError("video decoder reported errors; frame indices are unreliable")
    frames = json.loads(result.stdout).get("frames", [])
    times = [
        round(Decimal(frame["best_effort_timestamp_time"]) * 1_000_000)
        for frame in frames
    ]
    if len(times) < 2 or any(b <= a for a, b in zip(times, times[1:])):
        raise ValueError("video needs strictly increasing decoded presentation times")
    return [stamp - times[0] for stamp in times]


def index(value, limit, name):
    if type(value) is not int or not 0 <= value < limit:
        raise ValueError(f"{name} is outside the original media")
    return value


def evaluate(bundle_path, annotations, ffprobe="ffprobe"):
    if annotations.get("format") != "echotrace-av-sync-v1":
        raise ValueError("unsupported annotation format")
    if type(annotations.get("synthetic")) is not bool:
        raise ValueError("annotations must explicitly label synthetic material")
    if not str(annotations.get("annotator", "")).strip():
        raise ValueError("an annotator is required")
    points = annotations.get("points", [])
    if not points or len({p.get("id") for p in points}) != len(points):
        raise ValueError("nonempty, uniquely identified event pairs are required")
    if any(not isinstance(p.get("id"), str) or not p["id"] for p in points):
        raise ValueError("each paired event needs an id")
    pairs = [
        tuple(
            point[k]
            for k in (
                "video_media_id",
                "video_frame_index",
                "audio_media_id",
                "audio_sample_index",
            )
        )
        for point in points
    ]
    if len(set(pairs)) != len(pairs):
        raise ValueError("the same paired event cannot be counted twice")
    identity = tuple(annotations.get(k) for k in ("session_id", "device_id", "boot_id"))
    if not all(isinstance(value, str) and value for value in identity):
        raise ValueError("session, device and boot identity are required")
    with zipfile.ZipFile(
        bundle_path
    ) as bundle, tempfile.TemporaryDirectory() as temporary:
        if len(bundle.namelist()) != len(set(bundle.namelist())):
            raise ValueError("duplicate names in evidence bundle")
        manifest = json.loads(bundle.read("evidence.json"))
        if manifest.get("format") != "echotrace-evidence-v1":
            raise ValueError("expected an EchoTrace evidence export")
        media, bounds = {}, set()
        for evidence in manifest["evidence"]:
            anchor = evidence["anchor"]
            if (
                tuple(anchor[k] for k in ("session_id", "device_id", "boot_id"))
                != identity
            ):
                raise ValueError("bundle contains another source clock or session")
            bounds.add((anchor["started_us"], anchor["ended_us"]))
            for item in evidence["media"]:
                old = media.setdefault(item["id"], item)
                if any(old[k] != item[k] for k in IDENTITY):
                    raise ValueError("conflicting media identity in evidence bundle")
        if len(bounds) != 1:
            raise ValueError("export contains inconsistent session bounds")
        started, ended = bounds.pop()
        files = {item["media_id"]: item for item in manifest["files"]}
        if len(files) != len(manifest["files"]):
            raise ValueError("duplicate media files in evidence bundle")
        selected = {p[k] for p in points for k in ("video_media_id", "audio_media_id")}
        local, decoded = {}, {}
        total = 0
        for number, media_id in enumerate(sorted(selected)):
            item, entry = media[media_id], files[media_id]
            if (
                tuple(item[k] for k in ("session_id", "device_id", "boot_id"))
                != identity
            ):
                raise ValueError("media uses another source clock or session")
            if item["discontinuity"]:
                raise ValueError(
                    "do not use discontinuous segments for synchronization acceptance"
                )
            if item["kind"] not in ("video", "audio"):
                raise ValueError("expected original audio or video")
            info = bundle.getinfo(entry["path"])
            total += info.file_size
            if info.file_size != item["size_bytes"] or total > 1024**3:
                raise ValueError(
                    "selected media size differs or exceeds the 1 GiB bound"
                )
            if entry["sha256"] != item["sha256"]:
                raise ValueError("conflicting checksum in evidence bundle")
            # Never extract archive paths: write only our own generated filenames.
            path = Path(temporary) / str(number)
            digest = hashlib.sha256()
            with bundle.open(info) as source, path.open("xb") as destination:
                for chunk in iter(lambda: source.read(262144), b""):
                    digest.update(chunk)
                    destination.write(chunk)
            if digest.hexdigest() != item["sha256"]:
                raise ValueError("original checksum mismatch")
            local[media_id] = path
        measured = []
        for point in points:
            video = media[point["video_media_id"]]
            audio = media[point["audio_media_id"]]
            if (video["kind"], audio["kind"]) != ("video", "audio"):
                raise ValueError("paired media kinds are reversed")
            if video["id"] not in decoded:
                decoded[video["id"]] = frame_times(local[video["id"]], ffprobe)
            times = decoded[video["id"]]
            frame = index(point["video_frame_index"], len(times), "video_frame_index")
            video_time = video["start_us"] + times[frame]
            with wave.open(str(local[audio["id"]]), "rb") as stream:
                if (
                    stream.getnchannels(),
                    stream.getsampwidth(),
                    stream.getframerate(),
                ) != (1, 2, 16000):
                    raise ValueError("expected the original 16 kHz mono PCM")
                sample = index(
                    point["audio_sample_index"],
                    stream.getnframes(),
                    "audio_sample_index",
                )
                audio_time = (
                    audio["start_us"] + sample * 1_000_000 / stream.getframerate()
                )
            if not (
                video["start_us"] <= video_time < video["end_us"]
                and audio["start_us"] <= audio_time < audio["end_us"]
            ):
                raise ValueError("annotated event lies outside source metadata")
            interval = (
                times[frame + 1] - times[frame]
                if frame + 1 < len(times)
                else times[-1] - times[-2]
            )
            measured.append(
                {
                    **point,
                    "video_source_us": video_time,
                    "audio_source_us": audio_time,
                    "audio_minus_video_ms": (audio_time - video_time) / 1000,
                    "frame_quantization_ms": interval / 1000,
                }
            )
        absolute = sorted(abs(point["audio_minus_video_ms"]) for point in measured)
        p95 = absolute[math.ceil(len(absolute) * 0.95) - 1]
        duration = (ended - started) / 1e6 if ended is not None else None
        groups = [0, 0, 0]
        if duration and duration > 0:
            for point in measured:
                relative = (point["video_source_us"] - started) / (ended - started)
                if 0 <= relative <= 0.1:
                    groups[0] += 1
                if 0.4 <= relative <= 0.6:
                    groups[1] += 1
                if 0.9 <= relative <= 1:
                    groups[2] += 1
        coverage = duration is not None and duration >= 3600 and min(groups) >= 3
        return {
            "format": "echotrace-av-sync-report-v1",
            "synthetic": annotations["synthetic"],
            "annotator": annotations["annotator"],
            **dict(zip(("session_id", "device_id", "boot_id"), identity)),
            "scope": "source recording alignment at manually paired events; playback latency is not measured",
            "original_checksums_verified": True,
            "pairs": len(measured),
            "recording_seconds": duration,
            "begin_middle_end_counts": groups,
            "one_hour_coverage": coverage,
            "absolute_error_p95_ms": p95,
            "absolute_error_max_ms": max(absolute),
            "continuous_recording_verified": False,
            "one_hour_sampled_200ms_gate_passed": not annotations["synthetic"]
            and coverage
            and p95 <= 200,
            "limitations": "Manual pairing and frame quantization affect uncertainty; points do not measure unobserved intervals.",
            "points": measured,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--annotations", type=Path, required=True)
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = evaluate(
        args.bundle, json.loads(args.annotations.read_text()), args.ffprobe
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(
        json.dumps(
            {k: v for k, v in report.items() if k != "points"},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
