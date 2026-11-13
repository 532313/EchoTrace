import copy
import hashlib
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from evaluate_av_sync import evaluate, frame_times


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools required"
)
class AVSyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.video = cls.root / "synthetic.mp4"
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
                "3",
                "-vf",
                "setpts=PTS+2/TB",
                "-fps_mode",
                "passthrough",
                "-c:v",
                "libx264",
                "-bf",
                "2",
                "-g",
                "25",
                "-movflags",
                "+frag_keyframe+empty_moov",
                str(cls.video),
            ],
            check=True,
            timeout=30,
        )
        cls.audio = cls.root / "synthetic.wav"
        samples = [0] * 16000
        for sample in (1280, 6400, 11520):
            samples[sample] = 30000
        with wave.open(str(cls.audio), "wb") as stream:
            stream.setparams((1, 2, 16000, 0, "NONE", "PCM"))
            stream.writeframes(struct.pack("<16000h", *samples))

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def fixture(self, *, damage=False, boot="boot-a"):
        identity = {
            "session_id": "synthetic-session",
            "device_id": "synthetic-device",
            "boot_id": boot,
        }
        manifest = {
            "format": "echotrace-evidence-v1",
            "evidence": [
                {
                    "anchor": {
                        **identity,
                        "started_us": 1000000,
                        "ended_us": 3601000000,
                    },
                    "media": [],
                }
            ],
            "files": [],
        }
        annotations = {
            "format": "echotrace-av-sync-v1",
            "synthetic": True,
            "annotator": "automated synthetic fixture, not a hardware result",
            **identity,
            "points": [],
        }
        contents = {}
        for group, start in enumerate((1000000, 1801000000, 3596000000)):
            for kind, path in (("video", self.video), ("audio", self.audio)):
                content = path.read_bytes()
                media_id = f"{kind}-{group}"
                first = start + (120000 if kind == "audio" else 0)
                item = {
                    **identity,
                    "id": media_id,
                    "kind": kind,
                    "start_us": first,
                    "end_us": first + 1000000,
                    "discontinuity": False,
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
                manifest["evidence"][0]["media"].append(item)
                entry = {
                    "media_id": media_id,
                    "path": f"media/{media_id}{path.suffix}",
                    "sha256": item["sha256"],
                }
                manifest["files"].append(entry)
                contents[entry["path"]] = content
            for number, frame in enumerate((2, 10, 18)):
                annotations["points"].append(
                    {
                        "id": f"group-{group}-clap-{number}",
                        "video_media_id": f"video-{group}",
                        "video_frame_index": frame,
                        "audio_media_id": f"audio-{group}",
                        "audio_sample_index": frame * 640,
                    }
                )
        bundle = self.root / ("damaged.zip" if damage else "evidence.zip")
        with zipfile.ZipFile(bundle, "w") as archive:
            for name, content in contents.items():
                if damage and name.endswith("audio-0.wav"):
                    content = content[:-1] + bytes([content[-1] ^ 1])
                archive.writestr(name, content)
            archive.writestr("evidence.json", json.dumps(manifest))
        return bundle, annotations

    def test_b_frames_nonzero_pts_and_known_offset_never_count_as_real_measurement(
        self,
    ):
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_frames",
                "-show_entries",
                "frame=best_effort_timestamp_time,pict_type",
                "-of",
                "json",
                str(self.video),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        frames = json.loads(probe.stdout)["frames"]
        self.assertGreater(float(frames[0]["best_effort_timestamp_time"]), 0)
        self.assertIn("B", {f["pict_type"] for f in frames})
        self.assertEqual(frame_times(self.video)[10], 400000)
        bundle, annotations = self.fixture()
        result = evaluate(bundle, annotations)
        self.assertAlmostEqual(result["absolute_error_p95_ms"], 120)
        self.assertTrue(result["one_hour_coverage"])
        self.assertEqual(result["begin_middle_end_counts"], [3, 3, 3])
        self.assertFalse(result["one_hour_sampled_200ms_gate_passed"])
        self.assertFalse(result["continuous_recording_verified"])
        self.assertTrue(result["synthetic"])

    def test_rejects_corruption_another_boot_and_out_of_range_annotations(self):
        bundle, annotations = self.fixture(damage=True)
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            evaluate(bundle, annotations)
        bundle, annotations = self.fixture()
        wrong_boot = {**annotations, "boot_id": "another-boot"}
        with self.assertRaisesRegex(ValueError, "another source clock"):
            evaluate(bundle, wrong_boot)
        invalid = copy.deepcopy(annotations)
        invalid["points"][0]["audio_sample_index"] = 16000
        with self.assertRaisesRegex(ValueError, "outside the original"):
            evaluate(bundle, invalid)
        invalid = copy.deepcopy(annotations)
        invalid["points"][1] = {**invalid["points"][0], "id": "different-id-same-event"}
        with self.assertRaisesRegex(ValueError, "counted twice"):
            evaluate(bundle, invalid)
        invalid = copy.deepcopy(annotations)
        invalid["points"][0]["video_frame_index"] = True
        with self.assertRaisesRegex(ValueError, "outside the original"):
            evaluate(bundle, invalid)


if __name__ == "__main__":
    unittest.main()
