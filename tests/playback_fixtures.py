import copy
import hashlib
import json
import subprocess
from pathlib import Path


def generate(root, endpoint):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    clips = {"video": [], "audio": []}
    for index, color in enumerate(("red", "green", "blue"), 1):
        for kind, suffix, source, codec in (
            (
                "video",
                ".mp4",
                f"color=c={color}:s=160x96:r=25",
                [
                    "-c:v",
                    "libx264",
                    "-bf",
                    "2",
                    "-g",
                    "25",
                    "-pix_fmt",
                    "yuv420p",
                    "-movflags",
                    "+frag_keyframe+empty_moov",
                ],
            ),
            (
                "audio",
                ".wav",
                f"sine=frequency={index*220}:sample_rate=16000",
                ["-ac", "1", "-c:a", "pcm_s16le"],
            ),
        ):
            path = root / f"{kind}-{index}{suffix}"
            subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    source,
                    "-t",
                    "2",
                    *codec,
                    str(path),
                ],
                check=True,
                timeout=30,
            )
            clips[kind].append(
                {
                    "id": f"{kind}-{index}",
                    "start_us": (1 + 2 * (index - 1)) * 1_000_000,
                    "end_us": (1 + 2 * index) * 1_000_000,
                    "available": True,
                    "url": endpoint + "/" + path.name,
                    "size_bytes": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "discontinuity": False,
                }
            )
    continuous = dict(
        format="echotrace-playback-v1",
        session_id="synthetic-playback",
        boot_id="synthetic-boot",
        begin_us=1_000_000,
        end_us=7_000_000,
        anchor_us=1_000_000,
        continuity_tolerance_us=50000,
        gaps=[],
        **clips,
    )
    gap = copy.deepcopy(continuous)
    gap["end_us"] += 1_000_000
    for kind in ("video", "audio"):
        gap[kind][2]["start_us"] += 1_000_000
        gap[kind][2]["end_us"] += 1_000_000
        gap["gaps"].append(
            {
                "kind": kind,
                "start_us": 5_000_000,
                "end_us": 6_000_000,
                "reason": "unrecorded",
            }
        )
    uncached = copy.deepcopy(continuous)
    uncached["video"][1]["available"] = False
    uncached["video"][1]["url"] = None
    uncached["gaps"] = [
        {
            "kind": "video",
            "start_us": 3_000_000,
            "end_us": 5_000_000,
            "reason": "uncached",
            "media_id": "video-2",
        }
    ]
    shifted_file = root / "video-shifted.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(root / "video-1.mp4"),
            "-c:v",
            "copy",
            "-output_ts_offset",
            "5",
            str(shifted_file),
        ],
        check=True,
        timeout=30,
    )
    shifted = copy.deepcopy(continuous)
    shifted["end_us"] = 3_000_000
    shifted["video"] = [
        dict(
            continuous["video"][0],
            url=endpoint + "/" + shifted_file.name,
            size_bytes=shifted_file.stat().st_size,
            sha256=hashlib.sha256(shifted_file.read_bytes()).hexdigest(),
        )
    ]
    shifted["audio"] = [continuous["audio"][0]]
    bad_clock = copy.deepcopy(shifted)
    bad_clock["video"][0]["end_us"] += 500000
    fixtures = {
        "synthetic": True,
        "quality_evaluation": False,
        "continuous": continuous,
        "gap": gap,
        "uncached": uncached,
        "shifted": shifted,
        "bad_clock": bad_clock,
        "audio_only": dict(continuous, video=[]),
    }
    (root / "plans.json").write_text(json.dumps(fixtures, indent=2))
    return fixtures
