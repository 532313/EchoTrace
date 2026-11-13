import hashlib
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from audit_edge_session import audit, fragmented_video
from check_storage import write_probe
from migrate_edge_data import migrate
from summarize_sd_validation import summarize


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self):
        self.temporary.cleanup()

    def original(self):
        source = self.root / "source"
        session = source / "sessions" / "session-a"
        session.mkdir(parents=True)
        path = session / "audio.wav"
        with wave.open(str(path), "wb") as stream:
            stream.setparams((1, 2, 16000, 0, "NONE", "PCM"))
            stream.writeframes(b"\0\0" * 16000)
        (source / "state.json").write_text(
            json.dumps({"state": "stopped", "session": {"id": "session-a"}})
        )
        (source / "journal").mkdir()
        (source / "journal/observations.jsonl").write_bytes(b'{"original":"event"}\n')
        (source / "manifests").mkdir()
        item = {
            "id": "test-audio",
            "session_id": "session-a",
            "kind": "audio",
            "start_us": 1000000,
            "end_us": 2000000,
            "discontinuity": False,
            "size_bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        manifest = source / "manifests/audio.json"
        manifest.write_text(json.dumps({"path": str(path), "media": item}))
        return source, path, manifest

    def test_bounded_probe_cleans_only_its_files(self):
        sentinel = self.root / "existing.bin"
        sentinel.write_bytes(b"keep original")
        result = write_probe(self.root, 1024 * 1024)
        self.assertTrue(result["passed"])
        self.assertEqual(list(self.root.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_bytes(), b"keep original")

    def test_migration_keeps_originals_and_rejects_damage(self):
        source, path, manifest = self.original()
        before = {
            str(p.relative_to(source)): p.read_bytes()
            for p in source.rglob("*")
            if p.is_file()
        }
        mount = self.root / "sd"
        mount.mkdir()
        destination = mount / "echotrace"
        with patch(
            "migrate_edge_data.mounted_storage",
            return_value={"available_bytes": 1024**3},
        ):
            result = migrate(source, destination, mount, self.root / "fake-device")
            self.assertEqual(result["verified_original_media"], 1)
            self.assertEqual(audit(destination, "session-a", True)["passed"], True)
            self.assertEqual(
                before,
                {
                    str(p.relative_to(source)): p.read_bytes()
                    for p in source.rglob("*")
                    if p.is_file()
                },
            )
            self.assertEqual(
                json.loads((destination / "manifests/audio.json").read_text())["path"],
                str(destination / path.relative_to(source)),
            )
            with self.assertRaises(ValueError):
                migrate(source, destination, mount, self.root / "fake-device")
            path.write_bytes(path.read_bytes()[:-1] + b"\1")
            with self.assertRaises(ValueError):
                migrate(source, mount / "damaged", mount, self.root / "fake-device")
            self.assertFalse((mount / "damaged").exists())
            self.assertEqual(list(mount.iterdir()), [destination])
        broken = audit(source, "session-a", True)
        self.assertFalse(broken["passed"])
        self.assertIn("SHA-256", broken["errors"][0]["error"])

    def test_summary_rejects_missing_changed_and_duplicate_index_identity(self):
        item = {
            "id": "video-a",
            "kind": "video",
            "start_us": 0,
            "end_us": 3600000000,
            "size_bytes": 64,
            "sha256": "a" * 64,
            "discontinuity": False,
            "width": 1920,
            "height": 1080,
            "duration_us": 3600000000,
            "frames": 90000,
        }
        run = {
            "timeline": {"media": [dict(item)]},
            "session": {"id": "unit-test"},
            "markers": [],
            "completed_duration": True,
            "requested_seconds": 3600,
            "elapsed_seconds": 3600,
            "minimum_echo_MemAvailable_kB": 24576,
            "minimum_hub_MemAvailable": 1024**3,
        }
        audited = {"media": [dict(item)], "passed": True, "sha256_checked": True}
        storage = {"storage": {}, "write_probe": {}}
        result = summarize(run, audited, storage, {})
        self.assertTrue(result["recording_storage_gate_passed"])
        self.assertFalse(result["synchronization_measured"])
        run["timeline"]["media"][0]["sha256"] = "b" * 64
        self.assertFalse(
            summarize(run, audited, storage, {})["recording_storage_gate_passed"]
        )
        run["timeline"]["media"] = []
        self.assertFalse(
            summarize(run, audited, storage, {})["source_and_index_metadata_match"]
        )
        run["timeline"]["media"] = [dict(item), dict(item)]
        self.assertFalse(
            summarize(run, audited, storage, {})["source_and_index_metadata_match"]
        )

    @unittest.skipUnless(
        shutil.which("ffmpeg"), "ffmpeg is required for a real MP4 fixture"
    )
    def test_fragment_tables_match_generated_frames_and_reject_truncation(self):
        path = self.root / "fragmented.mp4"
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
                "testsrc2=size=160x96:rate=25",
                "-t",
                "2",
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
                str(path),
            ],
            check=True,
            timeout=30,
        )
        result = fragmented_video(path)
        self.assertEqual(
            (result["frames"], result["width"], result["height"]), (50, 160, 96)
        )
        self.assertEqual(result["duration_us"], 2000000)
        self.assertAlmostEqual(result["average_fps"], 25)
        damaged = self.root / "truncated.mp4"
        damaged.write_bytes(path.read_bytes()[:-32])
        with self.assertRaises(ValueError):
            fragmented_video(damaged)
        damaged.write_bytes(struct.pack(">I4s", 4, b"moov"))
        with self.assertRaises(ValueError):
            fragmented_video(damaged)


if __name__ == "__main__":
    unittest.main()
