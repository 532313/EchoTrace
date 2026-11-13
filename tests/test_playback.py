import unittest
import functools
import json
import os
import subprocess
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playback_fixtures import generate

from echotrace.playback import make_playback_plan

ROOT = Path(__file__).resolve().parents[1]
QT_TEST = Path(
    os.environ.get(
        "ECHOTRACE_QT_PLAYBACK", str(ROOT / "build/rk3576/echotrace-playback-test")
    )
)


class QtPlaybackTests(unittest.TestCase):
    @unittest.skipUnless(
        QT_TEST.is_file(), "build the native Qt playback test on RK3576"
    )
    def test_decode_transition_seek_gap_and_audio_command_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            requests = []
            active = 0
            maximum_active = 0
            request_lock = threading.Lock()

            class Handler(SimpleHTTPRequestHandler):
                def log_message(self, *_):
                    pass

                def do_GET(self):
                    if (
                        self.headers.get("Authorization")
                        != "Bearer playback-test-token"
                    ):
                        self.send_error(401)
                        return
                    if self.path.startswith("/video-1"):
                        time.sleep(0.15)
                    super().do_GET()

                def do_POST(self):
                    nonlocal active, maximum_active
                    if (
                        self.headers.get("Authorization")
                        != "Bearer playback-test-token"
                    ):
                        self.send_error(401)
                        return
                    with request_lock:
                        active += 1
                        maximum_active = max(maximum_active, active)
                    try:
                        body = json.loads(
                            self.rfile.read(int(self.headers["Content-Length"]))
                        )
                        requests.append({"path": self.path, "body": body})
                        time.sleep(0.1)
                    finally:
                        with request_lock:
                            active -= 1
                    data = b'{"ok":true}'
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)

            server = ThreadingHTTPServer(
                ("127.0.0.1", 0), functools.partial(Handler, directory=temporary)
            )
            endpoint = "http://127.0.0.1:" + str(server.server_port)
            generate(temporary, endpoint)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                result = subprocess.run(
                    [str(QT_TEST), str(Path(temporary) / "plans.json"), endpoint],
                    env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                    capture_output=True,
                    text=True,
                    timeout=45,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                report = json.loads(result.stdout.strip().splitlines()[-1])
                self.assertTrue(report["gap_stops"] and report["uncached_stops"])
                played = [
                    item["body"]["media_id"]
                    for item in requests
                    if item["path"] == "/api/playback"
                ]
                self.assertTrue(
                    {"audio-1", "audio-2", "audio-3"}.issubset(played), requests
                )
                self.assertEqual(
                    maximum_active, 1, "audio commands raced across pause or seek"
                )
                self.assertEqual(requests[-1]["path"], "/api/playback/stop")
                self.assertTrue(
                    any(
                        item["body"].get("media_id") == "audio-3"
                        and item["body"].get("offset_seconds", 0) >= 0.45
                        for item in requests
                    )
                )
                report.update(
                    audio_commands_serialized=True,
                    audio_requests=len(requests),
                    quality_evaluation=False,
                )
                output = os.environ.get("ECHOTRACE_PLAYBACK_REPORT")
                if output:
                    Path(output).write_text(json.dumps(report, indent=2))
            finally:
                server.shutdown()
                server.server_close()
                worker.join()


class PlaybackPlanTests(unittest.TestCase):
    def test_cached_overlap_does_not_create_a_false_gap(self):
        plan = make_playback_plan(
            self.evidence(
                [
                    {
                        "id": "uncached",
                        "kind": "video",
                        "start_us": 1_000_000,
                        "end_us": 6_000_000,
                        "available": False,
                    },
                    {
                        "id": "cached",
                        "kind": "video",
                        "start_us": 1_000_000,
                        "end_us": 10_000_000,
                    },
                ]
            )
        )
        self.assertEqual([gap for gap in plan["gaps"] if gap["kind"] == "video"], [])

    def evidence(self, media):
        return {
            "anchor": {
                "session_id": "session",
                "boot_id": "boot",
                "source_time_us": 5_000_000,
                "ended_us": 10_000_000,
            },
            "range_start_us": 1_000_000,
            "range_end_us": 15_000_000,
            "media": [
                dict(
                    item,
                    url="/media/" + item["id"] if item.get("available", True) else None,
                    size_bytes=100,
                    sha256="a" * 64,
                    discontinuity=False,
                    available=item.get("available", True),
                )
                for item in media
            ],
        }

    def test_source_clock_and_uncached_interval_are_preserved(self):
        plan = make_playback_plan(
            self.evidence(
                [
                    {
                        "id": "third",
                        "kind": "video",
                        "start_us": 7_000_000,
                        "end_us": 10_000_000,
                    },
                    {
                        "id": "first",
                        "kind": "video",
                        "start_us": 1_000_000,
                        "end_us": 4_000_000,
                    },
                    {
                        "id": "second",
                        "kind": "video",
                        "start_us": 4_000_000,
                        "end_us": 7_000_000,
                        "available": False,
                    },
                ]
            )
        )
        self.assertEqual(
            [clip["id"] for clip in plan["video"]], ["first", "second", "third"]
        )
        self.assertEqual(plan["end_us"], 10_000_000)
        video_gaps = [gap for gap in plan["gaps"] if gap["kind"] == "video"]
        self.assertEqual(
            video_gaps,
            [
                {
                    "kind": "video",
                    "start_us": 4_000_000,
                    "end_us": 7_000_000,
                    "reason": "uncached",
                    "media_id": "second",
                }
            ],
        )
        self.assertEqual(plan["video"][2]["start_us"], 7_000_000)

    def test_a_recording_gap_is_not_removed_from_the_timeline(self):
        plan = make_playback_plan(
            self.evidence(
                [
                    {
                        "id": "before",
                        "kind": "video",
                        "start_us": 1_000_000,
                        "end_us": 4_000_000,
                    },
                    {
                        "id": "after",
                        "kind": "video",
                        "start_us": 6_000_000,
                        "end_us": 10_000_000,
                    },
                    {
                        "id": "sound",
                        "kind": "audio",
                        "start_us": 1_000_000,
                        "end_us": 10_000_000,
                    },
                ]
            )
        )
        self.assertEqual(
            plan["gaps"],
            [
                {
                    "kind": "video",
                    "start_us": 4_000_000,
                    "end_us": 6_000_000,
                    "reason": "unrecorded",
                }
            ],
        )
        self.assertEqual(plan["anchor_us"], 5_000_000)
