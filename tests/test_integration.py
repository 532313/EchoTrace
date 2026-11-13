"""Real HTTP/WebSocket + compiled native simulator. No hardware metrics are inferred."""

import hashlib
import asyncio
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
import uvicorn
from websockets.sync.client import connect
from echotrace.app import create_app
from echotrace.config import Settings
from echotrace.protocol import SessionCreate, EventIn
from echotrace.store import Conflict

ROOT = Path(__file__).resolve().parents[1]
EDGE = Path(os.environ.get("ECHOTRACE_EDGE", str(ROOT / "build/host/echotrace-edge")))


class RunningServer:
    def __init__(
        self, root, port=None, ws="websockets-sansio", handshake_delay=0, ack_delay=0
    ):
        if port is None:
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self.connections = 0
        self.app = create_app(
            Settings(
                root,
                token="integration-test-token",
                min_free_bytes=0,
                model_config=root / "absent-models.json",
            )
        )

        async def endpoint(scope, receive, send):
            if scope["type"] == "websocket":
                self.connections += 1
                await asyncio.sleep(handshake_delay)

            async def paced_send(message):
                if message.get("type") == "websocket.send" and message.get("text"):
                    if json.loads(message["text"]).get("type") == "media.chunk.ack":
                        await asyncio.sleep(ack_delay)
                await send(message)

            await self.app(scope, receive, paced_send)

        self.server = uvicorn.Server(
            uvicorn.Config(
                endpoint,
                host="127.0.0.1",
                port=port,
                log_level="error",
                ws=ws,
            )
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self):
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("HTTP server did not start")

    def stop(self):
        self.server.should_exit = True
        self.thread.join(8)
        if self.thread.is_alive():
            raise RuntimeError("server failed to stop")

    def request(self, method, path, data=None, headers=None, raw=False):
        body = (
            data
            if isinstance(data, bytes)
            else json.dumps(data).encode() if data is not None else None
        )
        h = {
            "Authorization": "Bearer integration-test-token",
            "Content-Type": "application/json",
        }
        h.update(headers or {})
        request = urllib.request.Request(
            self.url + path, data=body, method=method, headers=h
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            content = response.read()
            return (
                (response.status, response.headers, content)
                if raw
                else json.loads(content)
            )


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = RunningServer(Path(self.temp.name)).start()

    def tearDown(self):
        self.server.stop()
        self.temp.cleanup()

    def test_old_boot_reconcile_replay_keeps_connection(self):
        store = self.server.app.state.store
        project = store.create_project("replay across source boot")
        old = store.create_session(
            SessionCreate(
                project_id=project["id"], device_id="edge", boot_id="old",
                source_time_us=10,
            )
        )
        store.set_state(old["id"], "stopped", 20)
        interrupted = store.create_session(
            SessionCreate(
                project_id=project["id"], device_id="edge", boot_id="old",
                source_time_us=30,
            )
        )
        store.mark_interrupted("edge", "old")
        current = store.create_session(
            SessionCreate(
                project_id=project["id"], device_id="edge", boot_id="new",
                source_time_us=100,
            )
        )

        with connect(
            self.server.url.replace("http://", "ws://") + "/ws/device",
            additional_headers={"Authorization": "Bearer integration-test-token"},
            open_timeout=3,
        ) as ws:
            ws.send(json.dumps({
                "type": "hello", "version": 3, "device_id": "edge",
                "boot_id": "new", "source_time_us": 100,
            }))
            self.assertEqual(json.loads(ws.recv(timeout=3))["type"], "hello")

            def reconcile(session_id, state, stamp):
                ws.send(json.dumps({
                    "type": "session.reconcile", "session_id": session_id,
                    "state": state, "source_time_us": stamp,
                }))
                reply = json.loads(ws.recv(timeout=3))
                self.assertEqual(reply["type"], "session.ack")
                self.assertEqual(reply["session_id"], session_id)
                return reply["ignored_stale"]

            self.assertTrue(reconcile(old["id"], "paused", 15))
            self.assertEqual(store.session(old["id"])["state"], "stopped")
            self.assertTrue(reconcile(interrupted["id"], "paused", 35))
            self.assertEqual(store.session(interrupted["id"])["state"], "interrupted")
            self.assertFalse(reconcile(interrupted["id"], "stopped", 40))
            self.assertEqual(store.session(interrupted["id"])["state"], "stopped")
            self.assertFalse(reconcile(current["id"], "paused", 110))
            self.assertTrue(reconcile(current["id"], "recording", 105))
            self.assertEqual(store.session(current["id"])["state"], "paused")
            ws.send(json.dumps({
                "type": "heartbeat", "source_time_us": 120,
                "session_id": current["id"], "capture_state": "paused",
            }))
            self.assertEqual(json.loads(ws.recv(timeout=3))["type"], "heartbeat")

    def test_closed_socket_and_missing_ack_are_conflicts(self):
        class ClosedSocket:
            application_state = type("State", (), {"name": "DISCONNECTED"})()

            async def send_json(self, message):
                raise RuntimeError(
                    'Cannot call "send" once a close message has been sent.'
                )

        class SilentSocket:
            async def send_json(self, message):
                pass

        async def exercise():
            devices = self.server.app.state.devices
            for sock in (ClosedSocket(), SilentSocket()):
                peer = {"socket": sock, "pending": {}, "send_lock": asyncio.Lock()}
                devices.connected["closed-test"] = peer
                with self.assertRaises(Conflict):
                    await devices.command("closed-test", "mark", timeout=0.01)
                self.assertEqual(peer["pending"], {})
            devices.connected.pop("closed-test")

        asyncio.run(exercise())

    def test_auth_and_no_phantom_recording(self):
        with self.assertRaises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(self.server.url + "/api/status")
        self.assertEqual(denied.exception.code, 401)
        project = self.server.request("POST", "/api/projects", {"name": "真实连接测试"})
        with self.assertRaises(urllib.error.HTTPError) as unavailable:
            self.server.request(
                "POST",
                "/api/sessions",
                {
                    "project_id": project["id"],
                    "device_id": "missing",
                    "boot_id": "boot",
                    "source_time_us": 1,
                },
            )
        self.assertEqual(unavailable.exception.code, 409)
        self.assertEqual(self.server.request("GET", "/api/sessions"), [])

    def test_upload_hash_range_and_truncation(self):
        store = self.server.app.state.store
        p = store.create_project("upload")
        s = store.create_session(
            SessionCreate(
                project_id=p["id"], device_id="edge", boot_id="boot", source_time_us=1
            )
        )
        body = b"0123456789abcdef"
        item = dict(
            id="media-range",
            session_id=s["id"],
            device_id="edge",
            boot_id="boot",
            start_us=1,
            end_us=1000000,
            kind="video",
            size_bytes=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
            mime="video/mp4",
        )
        self.server.request("POST", "/api/media", item)
        with self.assertRaises(urllib.error.HTTPError) as bad:
            self.server.request("PUT", "/api/media/media-range/content", body[:-1])
        self.assertEqual(bad.exception.code, 422)
        self.assertEqual(list((Path(self.temp.name) / "staging").iterdir()), [])
        self.server.request("PUT", "/api/media/media-range/content", body)
        store.ingest(
            EventIn(
                id="range-marker",
                session_id=s["id"],
                device_id="edge",
                boot_id="boot",
                seq=0,
                source_time_us=500000,
                kind="pedal",
                note="HTTP playback plan test",
            )
        )
        store.set_state(s["id"], "stopped", 1000000)
        evidence = self.server.request("GET", "/api/anchors/range-marker")
        self.assertEqual(evidence["playback"]["end_us"], 1000000)
        self.assertEqual(evidence["playback"]["video"][0]["id"], "media-range")
        self.assertTrue(evidence["playback"]["video"][0]["available"])
        status, headers, content = self.server.request(
            "GET",
            "/api/media/media-range/content",
            headers={"Range": "bytes=3-7"},
            raw=True,
        )
        self.assertEqual((status, content), (206, b"34567"))
        self.assertEqual(headers["Content-Range"], "bytes 3-7/16")
        with self.assertRaises(urllib.error.HTTPError) as invalid:
            self.server.request(
                "GET",
                "/api/media/media-range/content",
                headers={"Range": "bytes=999-1000"},
            )
        self.assertEqual(invalid.exception.code, 416)

    def test_undecodable_container_is_marked_unavailable(self):
        store = self.server.app.state.store
        p = store.create_project("invalid container")
        s = store.create_session(
            SessionCreate(
                project_id=p["id"], device_id="edge", boot_id="boot", source_time_us=1
            )
        )
        store.ingest(
            EventIn(
                id="bad-frame",
                session_id=s["id"],
                device_id="edge",
                boot_id="boot",
                seq=0,
                source_time_us=1000,
                kind="pedal",
                note="保留接线原图",
            )
        )
        content = b"not a video container"
        item = dict(
            id="invalid-container",
            session_id=s["id"],
            device_id="edge",
            boot_id="boot",
            start_us=1,
            end_us=2000000,
            kind="video",
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            mime="video/mp4",
        )
        self.server.request("POST", "/api/media", item)
        self.server.request("PUT", "/api/media/invalid-container/content", content)
        with self.assertRaises(urllib.error.HTTPError) as failure:
            self.server.request("POST", "/api/anchors/bad-frame/frame", {})
        self.assertEqual(failure.exception.code, 404)
        self.assertEqual(store.media("invalid-container")["status"], "unreadable")
        self.assertEqual(
            self.server.request("POST", "/api/query", {"text": "接线原图是什么样"})[
                "status"
            ],
            "unavailable",
        )


@unittest.skipUnless(EDGE.is_file(), "native host binary is not built")
class NativeIntegration(unittest.TestCase):
    def test_capture_fault_is_visible_and_cannot_resume(self):
        with tempfile.TemporaryDirectory(prefix="et-fault-", dir="/tmp") as directory:
            tmp = Path(directory)
            configuration = tmp / "edge.json"
            # An impossible reservation triggers the synthetic capture's space
            # guard; this test never fills a filesystem or uses a camera.
            configuration.write_text(json.dumps({"reserve_mb": 1 << 30}))
            server = RunningServer(tmp / "hub").start()
            with (tmp / "edge.log").open("w") as log:
                child = subprocess.Popen(
                    [
                        str(EDGE),
                        "--simulated",
                        "--config",
                        str(configuration),
                        "--hub",
                        f"ws://127.0.0.1:{server.port}/ws/device",
                        "--data",
                        str(tmp / "edge"),
                        "--socket",
                        str(tmp / "control.sock"),
                    ],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env={**os.environ, "ECHOTRACE_TOKEN": "integration-test-token"},
                )
                try:
                    devices = []
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline and not devices:
                        devices = server.request("GET", "/api/status")["devices"]
                        time.sleep(0.1)
                    self.assertTrue(devices)
                    device = devices[0]
                    project = server.request(
                        "POST",
                        "/api/projects",
                        {"name": "synthetic storage fault regression"},
                    )
                    session = server.request(
                        "POST",
                        "/api/sessions",
                        {
                            "project_id": project["id"],
                            "device_id": device["device_id"],
                            "boot_id": device["boot_id"],
                            "source_time_us": device["source_time_us"],
                        },
                    )
                    deadline = time.monotonic() + 15
                    state = {}
                    while time.monotonic() < deadline:
                        state = json.loads((tmp / "edge/state.json").read_text())
                        if state["state"] == "paused":
                            break
                        time.sleep(0.1)
                    self.assertEqual(state["state"], "paused")
                    self.assertIn("存储", state["error"])
                    # A successful background command must not erase the
                    # recording fault shown by the terminal's status API.
                    server.request("POST", "/api/playback/stop", {})
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as control:
                        control.settimeout(3)
                        control.connect(str(tmp / "control.sock"))
                        control.sendall(b'{"op":"status"}\n')
                        with control.makefile("rb") as response:
                            status = json.loads(response.readline(65536))
                    self.assertEqual(status["error"], state["error"])
                    timeline = server.request(
                        "GET", f"/api/sessions/{session['id']}/timeline"
                    )
                    self.assertTrue(
                        any(
                            "storage reserve reached"
                            in json.loads(event["body"])["note"]
                            for event in timeline["events"]
                        )
                    )
                    with self.assertRaises(urllib.error.HTTPError) as fault:
                        server.request(
                            "PATCH",
                            f"/api/sessions/{session['id']}/state",
                            {
                                "state": "recording",
                                "source_time_us": device["source_time_us"],
                            },
                        )
                    self.assertEqual(fault.exception.code, 409)
                    stopped = server.request(
                        "PATCH",
                        f"/api/sessions/{session['id']}/state",
                        {
                            "state": "stopped",
                            "source_time_us": device["source_time_us"],
                        },
                    )
                    self.assertEqual(stopped["state"], "stopped")
                    self.assertEqual(
                        json.loads((tmp / "edge/state.json").read_text())["error"], ""
                    )
                finally:
                    child.terminate()
                    try:
                        child.wait(10)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
                    server.stop()

    def test_slow_handshake_does_not_create_parallel_connections(self):
        with tempfile.TemporaryDirectory(prefix="echotrace-handshake-") as directory:
            tmp = Path(directory)
            server = RunningServer(tmp / "hub", handshake_delay=4).start()
            with (tmp / "edge.log").open("w") as log:
                child = subprocess.Popen(
                    [
                        str(EDGE),
                        "--simulated",
                        "--hub",
                        f"ws://127.0.0.1:{server.port}/ws/device",
                        "--data",
                        str(tmp / "edge"),
                        "--socket",
                        str(tmp / "control.sock"),
                    ],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env={**os.environ, "ECHOTRACE_TOKEN": "integration-test-token"},
                )
                try:
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        time.sleep(0.2)
                        if time.monotonic() >= deadline - 1:
                            self.assertTrue(
                                server.request("GET", "/api/status")["devices"]
                            )
                            break
                    self.assertEqual(server.connections, 1)
                    self.assertIsNone(child.poll())
                finally:
                    child.terminate()
                    try:
                        child.wait(10)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
                    server.stop()

    def test_continuous_segments_with_control_and_fetch(self):
        with tempfile.TemporaryDirectory(prefix="echotrace-continuous-") as directory:
            tmp = Path(directory)
            server = RunningServer(tmp / "hub", ws="websockets", ack_delay=0.04).start()
            port = server.port
            with (tmp / "edge.log").open("w") as log:
                child = subprocess.Popen(
                    [
                        str(EDGE),
                        "--simulated",
                        "--hub",
                        f"ws://127.0.0.1:{server.port}/ws/device",
                        "--data",
                        str(tmp / "edge"),
                        "--socket",
                        str(tmp / "control.sock"),
                    ],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env={**os.environ, "ECHOTRACE_TOKEN": "integration-test-token"},
                )
                try:
                    deadline = time.monotonic() + 10
                    devices = []
                    while time.monotonic() < deadline and not devices:
                        devices = server.request("GET", "/api/status")["devices"]
                        time.sleep(0.1)
                    self.assertTrue(devices)
                    project = server.request(
                        "POST",
                        "/api/projects",
                        {"name": "continuous synthetic regression"},
                    )
                    session = server.request(
                        "POST",
                        "/api/sessions",
                        {
                            "project_id": project["id"],
                            "device_id": devices[0]["device_id"],
                            "boot_id": devices[0]["boot_id"],
                            "source_time_us": devices[0]["source_time_us"],
                        },
                    )
                    sid = session["id"]
                    deadline = time.monotonic() + 34
                    requested = set()
                    interrupted = False
                    resuming = set()
                    while time.monotonic() < deadline:
                        server.request(
                            "POST",
                            f"/api/sessions/{sid}/mark",
                            {"note": "concurrent upload control"},
                        )
                        timeline = server.request(
                            "GET", f"/api/sessions/{sid}/timeline"
                        )
                        for item in timeline["media"]:
                            if item["id"] not in requested:
                                server.request(
                                    "POST", f"/api/media/{item['id']}/fetch", {}
                                )
                                requested.add(item["id"])
                        if not interrupted and list(
                            (tmp / "hub/staging").glob("*.part")
                        ):
                            resuming = {
                                item["id"]
                                for item in timeline["media"]
                                if item["status"] != "ready"
                            }
                            server.stop()
                            time.sleep(0.5)
                            server = RunningServer(
                                tmp / "hub", port, ws="websockets", ack_delay=0.04
                            ).start()
                            interrupted = True
                            reconnect_deadline = time.monotonic() + 10
                            while time.monotonic() < reconnect_deadline:
                                if (
                                    server.request("GET", f"/api/sessions/{sid}")[
                                        "state"
                                    ]
                                    == "recording"
                                ):
                                    break
                                time.sleep(0.1)
                        self.assertIsNone(
                            child.poll(), (tmp / "edge.log").read_text()[-2000:]
                        )
                        time.sleep(0.3)
                    server.request(
                        "PATCH",
                        f"/api/sessions/{sid}/state",
                        {"state": "stopped", "source_time_us": 1},
                    )
                    original_directory = tmp / "edge/sessions" / sid
                    video_count = len(list(original_directory.glob("*.mp4")))
                    audio_count = len(list(original_directory.glob("*.wav")))
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        timeline = server.request(
                            "GET", f"/api/sessions/{sid}/timeline"
                        )
                        if (
                            sum(m["kind"] == "video" for m in timeline["media"])
                            == video_count
                            and sum(m["kind"] == "audio" for m in timeline["media"])
                            == audio_count
                        ):
                            break
                        time.sleep(0.1)
                    videos = sum(m["kind"] == "video" for m in timeline["media"])
                    self.assertGreaterEqual(videos, 3)
                    self.assertEqual(videos, video_count)
                    self.assertEqual(
                        sum(m["kind"] == "audio" for m in timeline["media"]),
                        audio_count,
                    )
                    self.assertTrue(
                        interrupted, "test must interrupt an in-flight upload"
                    )
                    self.assertTrue(resuming)
                    self.assertTrue(
                        all(
                            m["status"] == "ready"
                            for m in timeline["media"]
                            if m["id"] in resuming
                        ),
                        "interrupted uploads must resume without a new fetch request",
                    )
                    for item in timeline["media"]:
                        server.request("POST", f"/api/media/{item['id']}/fetch", {})
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        timeline = server.request(
                            "GET", f"/api/sessions/{sid}/timeline"
                        )
                        if all(m["status"] == "ready" for m in timeline["media"]):
                            break
                        time.sleep(0.1)
                    self.assertTrue(
                        all(m["status"] == "ready" for m in timeline["media"])
                    )
                    self.assertEqual(timeline["session"]["state"], "stopped")
                    self.assertNotIn("hub: ", (tmp / "edge.log").read_text())
                finally:
                    child.terminate()
                    try:
                        child.wait(10)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
                    server.stop()

    def test_record_reconnect_fetch_and_delete(self):
        with tempfile.TemporaryDirectory(prefix="echotrace-integration-") as tmp:
            tmp = Path(tmp)
            server = RunningServer(tmp / "hub").start()
            port = server.port
            log = (tmp / "edge.log").open("w")
            child = subprocess.Popen(
                [
                    str(EDGE),
                    "--simulated",
                    "--hub",
                    f"ws://127.0.0.1:{port}/ws/device",
                    "--data",
                    str(tmp / "edge"),
                    "--socket",
                    str(tmp / "control.sock"),
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
                env={**os.environ, "ECHOTRACE_TOKEN": "integration-test-token"},
            )

            def wait(predicate, seconds=15):
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    if child.poll() is not None:
                        raise AssertionError((tmp / "edge.log").read_text())
                    value = predicate()
                    if value:
                        return value
                    time.sleep(0.1)
                raise AssertionError(
                    "timed out; " + (tmp / "edge.log").read_text()[-2000:]
                )

            try:
                device = wait(
                    lambda: next(
                        iter(server.request("GET", "/api/status")["devices"]), None
                    )
                )
                project = server.request(
                    "POST", "/api/projects", {"name": "native integration synthetic"}
                )
                session = server.request(
                    "POST",
                    "/api/sessions",
                    {
                        "project_id": project["id"],
                        "device_id": device["device_id"],
                        "boot_id": device["boot_id"],
                        "source_time_us": device["source_time_us"],
                        "title": "explicitly synthetic",
                    },
                )
                sid = session["id"]
                time.sleep(0.35)
                server.request(
                    "POST",
                    f"/api/sessions/{sid}/mark",
                    {"note": "因为旧排线接触不良，所以更换排线"},
                )
                wait(
                    lambda: server.request("GET", f"/api/sessions/{sid}/timeline")[
                        "anchors"
                    ]
                )
                server.stop()
                # The recorder must continue and accept an offline local marker.
                time.sleep(0.5)
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                    sock.connect(str(tmp / "control.sock"))
                    sock.sendall(b'{"op":"mark","note":"offline source marker"}\n')
                    sock.recv(8192)
                time.sleep(0.4)
                server = RunningServer(tmp / "hub", port).start()
                wait(lambda: server.request("GET", "/api/status")["devices"])
                wait(
                    lambda: server.request("GET", f"/api/sessions/{sid}")["state"]
                    == "recording"
                )
                time.sleep(0.4)
                server.request(
                    "PATCH",
                    f"/api/sessions/{sid}/state",
                    {"state": "stopped", "source_time_us": device["source_time_us"]},
                )
                timeline = wait(
                    lambda: (lambda t: t if len(t["media"]) >= 2 else None)(
                        server.request("GET", f"/api/sessions/{sid}/timeline")
                    )
                )
                self.assertTrue(
                    any(
                        a["note"] == "offline source marker"
                        for a in timeline["anchors"]
                    )
                )
                query = server.request(
                    "POST",
                    "/api/query",
                    {"text": "为什么更换排线", "project_id": project["id"]},
                )
                self.assertEqual(query["status"], "found")
                self.assertIn("接触不良", query["answer"])
                for media in timeline["media"]:
                    server.request("POST", f"/api/media/{media['id']}/fetch", {})
                wait(
                    lambda: all(
                        m["status"] == "ready"
                        for m in server.request("GET", f"/api/sessions/{sid}/timeline")[
                            "media"
                        ]
                    )
                )
                anchor = next(a for a in timeline["anchors"] if "接触不良" in a["note"])
                server.request("POST", f"/api/anchors/{anchor['id']}/frame", {})
                frame = server.request(
                    "GET", f"/api/anchors/{anchor['id']}/frame", raw=True
                )[2]
                self.assertTrue(frame.startswith(b"\xff\xd8"))
                self.assertEqual(
                    server.request("DELETE", f"/api/sessions/{sid}")["source_deletion"],
                    "done",
                )
                self.assertFalse((tmp / "edge/sessions" / sid).exists())
            finally:
                child.terminate()
                try:
                    child.wait(10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                log.close()
                server.stop()


if __name__ == "__main__":
    unittest.main()
