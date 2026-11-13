#!/usr/bin/env python3
"""Read closed originals without decoding; inspect this recorder's single-track fMP4."""
import argparse
import hashlib
import json
import struct
import time
import wave
from collections import Counter
from pathlib import Path


def boxes(stream, begin, end):
    offset = begin
    while offset < end:
        stream.seek(offset)
        header = stream.read(8)
        if len(header) != 8:
            raise ValueError("truncated MP4 box header")
        size, kind = struct.unpack(">I4s", header)
        head = 8
        if size == 1:
            raw = stream.read(8)
            if len(raw) != 8:
                raise ValueError("truncated extended MP4 size")
            size, head = struct.unpack(">Q", raw)[0], 16
        elif size == 0:
            size = end - offset
        if size < head or offset + size > end:
            raise ValueError("MP4 box exceeds its parent")
        yield kind, offset + head, offset + size
        offset += size


def fragmented_video(path):
    result = {"frames": 0, "fragment_count": 0}
    durations = Counter()
    sample_bytes = 0
    mdat_bytes = 0
    tracks = 0

    def body(stream, begin, end):
        if end - begin > 2 * 1024 * 1024:
            raise ValueError("MP4 metadata box exceeds audit bound")
        stream.seek(begin)
        value = stream.read(end - begin)
        if len(value) != end - begin:
            raise ValueError("truncated MP4 metadata")
        return value

    def walk(stream, begin, end, defaults=None):
        nonlocal tracks, sample_bytes, mdat_bytes
        for kind, first, last in boxes(stream, begin, end):
            if kind == b"mdat":
                mdat_bytes += last - first
            elif kind in (
                b"moov",
                b"trak",
                b"mdia",
                b"minf",
                b"stbl",
                b"moof",
                b"traf",
            ):
                if kind == b"trak":
                    tracks += 1
                if kind == b"moof":
                    result["fragment_count"] += 1
                walk(stream, first, last, {} if kind == b"traf" else defaults)
            elif kind == b"mdhd":
                value = body(stream, first, last)
                at = 20 if value[0] == 1 else 12
                result["timescale"] = struct.unpack_from(">I", value, at)[0]
            elif kind == b"tkhd":
                value = body(stream, first, last)
                width, height = struct.unpack_from(">II", value, len(value) - 8)
                result["width"], result["height"] = width >> 16, height >> 16
            elif kind == b"hdlr":
                if body(stream, first, last)[8:12] != b"vide":
                    raise ValueError("audit supports a single video track only")
            elif kind == b"tfhd":
                if defaults is None:
                    raise ValueError("tfhd outside traf")
                value = body(stream, first, last)
                flags = int.from_bytes(value[:4], "big") & 0xFFFFFF
                at = 8 + (8 if flags & 1 else 0) + (4 if flags & 2 else 0)
                for mask, name in ((8, "duration"), (16, "size"), (32, "flags")):
                    if flags & mask:
                        defaults[name] = struct.unpack_from(">I", value, at)[0]
                        at += 4
            elif kind == b"trun":
                if defaults is None:
                    raise ValueError("trun outside traf")
                value = body(stream, first, last)
                flags, count = struct.unpack_from(">II", value)
                flags &= 0xFFFFFF
                if count > 100000:
                    raise ValueError("MP4 sample count exceeds audit bound")
                at = 8 + (4 if flags & 1 else 0) + (4 if flags & 4 else 0)
                for _ in range(count):
                    sample = dict(defaults)
                    for mask, name in (
                        (256, "duration"),
                        (512, "size"),
                        (1024, "flags"),
                        (2048, "cts"),
                    ):
                        if flags & mask:
                            sample[name] = struct.unpack_from(">I", value, at)[0]
                            at += 4
                    if not sample.get("duration") or not sample.get("size"):
                        raise ValueError("missing MP4 sample duration or size")
                    durations[sample["duration"]] += 1
                    sample_bytes += sample["size"]
                if at != len(value):
                    raise ValueError("MP4 sample table length differs")
                result["frames"] += count

    with path.open("rb") as stream:
        walk(stream, 0, path.stat().st_size)
    if tracks != 1 or not durations or not result.get("timescale"):
        raise ValueError("expected this recorder's single-track fragmented MP4")
    if sample_bytes != mdat_bytes:
        raise ValueError("MP4 sample bytes differ from mdat payload")
    ticks = sum(value * count for value, count in durations.items())
    result["duration_us"] = round(ticks * 1e6 / result["timescale"])
    result["average_fps"] = result["frames"] * result["timescale"] / ticks
    result["maximum_sample_duration_us"] = max(durations) * 1e6 / result["timescale"]
    result["sample_intervals_over_60ms"] = sum(
        count
        for duration, count in durations.items()
        if duration * 1000 > result["timescale"] * 60
    )
    return result


def audit(root, session_id, verify_hash=False):
    root = root.resolve()
    directory = root / "sessions" / session_id
    if directory.parent != root / "sessions" or not directory.is_dir():
        raise ValueError("invalid session directory")
    state = json.loads((root / "state.json").read_text())
    if (state.get("session") or {}).get("id") == session_id and state[
        "state"
    ] != "stopped":
        raise ValueError("stop this session before auditing its originals")
    result = {
        "session_id": session_id,
        "sha256_checked": verify_hash,
        "scope": "original byte integrity and container sample tables; not full video decode or physical AV synchronization",
        "media": [],
        "errors": [],
        "unfinished_files": [
            str(p.relative_to(root)) for p in directory.rglob("*.part")
        ],
    }
    referenced = set()
    started = time.monotonic()
    for manifest in sorted((root / "manifests").glob("*.json")):
        metadata = json.loads(manifest.read_text())
        item = metadata["media"]
        if item["session_id"] != session_id:
            continue
        report = {
            key: item[key]
            for key in (
                "id",
                "kind",
                "start_us",
                "end_us",
                "size_bytes",
                "sha256",
                "discontinuity",
            )
        }
        try:
            path = Path(metadata["path"]).resolve()
            referenced.add(path)
            if (
                directory not in path.parents
                or path.stat().st_size != item["size_bytes"]
            ):
                raise ValueError("original path or size differs from manifest")
            if verify_hash:
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(262144), b""):
                        digest.update(chunk)
                if digest.hexdigest() != item["sha256"]:
                    raise ValueError("original SHA-256 differs from manifest")
            if item["kind"] == "video":
                report.update(fragmented_video(path))
            elif item["kind"] == "audio":
                with wave.open(str(path), "rb") as stream:
                    if (
                        stream.getnchannels(),
                        stream.getsampwidth(),
                        stream.getframerate(),
                    ) != (1, 2, 16000):
                        raise ValueError("unexpected WAV format")
                    report["samples"] = stream.getnframes()
                    report["duration_us"] = stream.getnframes() * 1000000 // 16000
            else:
                raise ValueError("audit supports original video and audio only")
            report["duration_difference_us"] = report["duration_us"] - (
                item["end_us"] - item["start_us"]
            )
        except (OSError, ValueError, struct.error, wave.Error) as error:
            result["errors"].append({"media_id": item["id"], "error": str(error)})
        result["media"].append(report)
    result["orphaned_originals"] = [
        str(path.relative_to(root))
        for path in directory.rglob("*")
        if path.is_file()
        and path.suffix in (".mp4", ".wav")
        and path.resolve() not in referenced
    ]
    result["passed"] = (
        bool(result["media"])
        and not result["errors"]
        and not result["unfinished_files"]
        and not result["orphaned_originals"]
    )
    result["elapsed_seconds"] = time.monotonic() - started
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--verify-sha256", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = audit(args.data, args.session, args.verify_sha256)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "media"},
            ensure_ascii=False,
        )
    )
    raise SystemExit(0 if report["passed"] else 1)
