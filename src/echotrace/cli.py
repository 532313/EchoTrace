from __future__ import annotations
import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .config import Settings
from .protocol import EventIn, MediaIn, SessionCreate
from .store import Store, identifier


def seed_demo(root):
    """Explicit synthetic fixtures. Never count these as a hardware evaluation."""
    settings = Settings(root.resolve(), min_free_bytes=0)
    settings.prepare()
    sentinel = settings.root / "synthetic-demo.json"
    if sentinel.exists():
        return json.loads(sentinel.read_text())
    if not shutil.which("ffmpeg"):
        raise RuntimeError(
            "ffmpeg is required to generate the labelled synthetic fixtures"
        )
    store = Store(settings.root)
    project = store.create_project("示例 · 串口接线与排线更换（模拟素材）")
    result = {
        "synthetic": True,
        "project_id": project["id"],
        "sessions": [],
        "anchors": [],
    }
    notes = [
        [
            (2, "pedal", "先拍下原始接线，黑色地线接在左侧。"),
            (5, "voice", "这个接法会导致启动失败，所以先不要接红色电源线。"),
            (8, "voice", "把 USB 转串口放到左边的零件托盘。"),
        ],
        [
            (2, "pedal", "更换排线之前保留这一张接线图。"),
            (5, "voice", "更换排线是因为旧排线接触不良，移动后会断连。"),
            (8, "voice", "今天已经更换排线，下一步检查启动日志。"),
        ],
    ]
    for index in range(2):
        boot_id = "synthetic-boot-" + str(index)
        start = (index + 1) * 100_000_000
        session = store.create_session(
            SessionCreate(
                project_id=project["id"],
                device_id="synthetic-demo",
                boot_id=boot_id,
                source_time_us=start,
                title=f"第 {index + 1} 次接线调试 · 模拟视频",
                clock_quality="unknown",
            )
        )
        result["sessions"].append(session["id"])
        for kind, extension, source, codec in [
            (
                "video",
                ".mp4",
                f"testsrc2=size=640x360:rate=25",
                [
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-g",
                    "25",
                    "-movflags",
                    "+frag_keyframe+empty_moov",
                ],
            ),
            (
                "audio",
                ".wav",
                f"sine=frequency={440 + index * 220}:sample_rate=16000",
                ["-c:a", "pcm_s16le", "-ac", "1"],
            ),
        ]:
            for second in range(0, 10, 4):
                duration = min(4, 10 - second)
                media_id = identifier()
                relative = (
                    "media/" + hashlib.sha256(media_id.encode()).hexdigest() + extension
                )
                path = settings.root / relative
                command = [
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
                    str(duration),
                    *codec,
                    str(path),
                ]
                subprocess.run(command, check=True, timeout=40)
                media = MediaIn(
                    id=media_id,
                    session_id=session["id"],
                    device_id=session["device_id"],
                    boot_id=boot_id,
                    start_us=start + second * 1_000_000,
                    end_us=start + (second + duration) * 1_000_000,
                    kind=kind,
                    size_bytes=path.stat().st_size,
                    sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    mime="video/mp4" if kind == "video" else "audio/wav",
                )
                store.register_media(media)
                store.attach_media(media_id, relative)
                with store.transaction():
                    store.db.execute(
                        "UPDATE media SET authoritative=1 WHERE id=?", (media_id,)
                    )
        for sequence, (second, kind, note) in enumerate(notes[index]):
            event = EventIn(
                id=identifier(),
                session_id=session["id"],
                device_id=session["device_id"],
                boot_id=boot_id,
                seq=sequence,
                source_time_us=start + second * 1_000_000,
                kind=kind,
                note=note,
                payload={"synthetic": True, "speech_is_not_real_audio": True},
            )
            store.ingest(event)
            result["anchors"].append(event.id)
        store.set_state(session["id"], "stopped", start + 10_000_000)
        # Make cross-session ordering deterministic, independent of filesystem timestamps.
        with store.transaction():
            store.db.execute(
                "UPDATE sessions SET created_ms=created_ms+? WHERE id=?",
                (index * 1000, session["id"]),
            )
    sentinel.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    store.close()
    return result


def doctor(settings):
    from .ai import LocalAI

    capabilities = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "executables": {
            key: shutil.which(key)
            for key in ("ffmpeg", "ffprobe", "cmake", "qmake", "g++")
        },
        "models": LocalAI(settings.models()).status(),
        "data": str(settings.root),
        "hardware_checked": False,
    }
    if Path("/proc/device-tree/model").exists():
        capabilities["board"] = (
            Path("/proc/device-tree/model").read_text().strip("\x00\n")
        )
    for path in ("/sys/kernel/debug/rknpu/version", "/sys/module/rknpu/version"):
        try:
            capabilities["npu_driver"] = Path(path).read_text().strip()
            break
        except OSError:
            pass
    return capabilities


def main():
    parser = argparse.ArgumentParser(prog="echotrace")
    parser.add_argument("command", choices=("serve", "demo", "doctor", "simulate-edge"))
    parser.add_argument(
        "--data", type=Path, default=Path(os.environ.get("ECHOTRACE_DATA", "runtime"))
    )
    parser.add_argument("--host", default=os.environ.get("ECHOTRACE_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("ECHOTRACE_PORT", "18081"))
    )
    parser.add_argument(
        "--models",
        type=Path,
        default=Path(os.environ.get("ECHOTRACE_MODELS", "models/config.json")),
    )
    parser.add_argument("--hub", default="ws://127.0.0.1:18081/ws/device")
    args = parser.parse_args()
    settings = Settings(
        args.data.resolve(),
        os.environ.get("ECHOTRACE_TOKEN", ""),
        args.host,
        args.port,
        model_config=args.models.resolve(),
    )
    if args.command == "demo":
        print(json.dumps(seed_demo(args.data), ensure_ascii=False, indent=2))
    elif args.command == "doctor":
        print(json.dumps(doctor(settings), ensure_ascii=False, indent=2))
    elif args.command == "simulate-edge":
        executable = Path(__file__).resolve().parents[2] / "build/host/echotrace-edge"
        if not executable.is_file():
            parser.error("build the native echotrace-edge executable first")
        subprocess.run(
            [
                str(executable),
                "--simulated",
                "--data",
                str(settings.root),
                "--hub",
                args.hub,
            ],
            check=True,
        )
    else:
        import uvicorn
        from .app import create_app

        uvicorn.run(
            create_app(settings),
            host=settings.host,
            port=settings.port,
            log_level="info",
        )


if __name__ == "__main__":
    main()
