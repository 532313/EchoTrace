import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from test_integration import EDGE, ROOT, RunningServer


@unittest.skipUnless(EDGE.is_file(), "native simulator is required")
class CoordinatorTests(unittest.TestCase):
    def test_operator_stop_is_successful_without_claiming_full_duration(self):
        with tempfile.TemporaryDirectory(prefix="et-stop-", dir="/tmp") as directory:
            tmp = Path(directory)
            server = RunningServer(tmp / "hub").start()
            edge = subprocess.Popen(
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
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={**os.environ, "ECHOTRACE_TOKEN": "integration-test-token"},
            )
            controller = None
            try:
                deadline = time.monotonic() + 10
                while not server.request("GET", "/api/status")["devices"]:
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.1)
                token = tmp / "token"
                token.write_text("integration-test-token")
                report = tmp / "report.json"
                signal = tmp / "stop"
                command = [
                    sys.executable,
                    str(ROOT / "scripts/soak_capture.py"),
                    "--hub",
                    server.url,
                    "--device-id",
                    server.request("GET", "/api/status")["devices"][0]["device_id"],
                    "--token-file",
                    str(token),
                    "--seconds",
                    "15",
                    "--poll-seconds",
                    "1",
                    "--stop-file",
                    str(signal),
                    "--output",
                    str(report),
                ]
                with (tmp / "controller.log").open("w") as log:
                    controller = subprocess.Popen(
                        command, stdout=log, stderr=subprocess.STDOUT
                    )
                    deadline = time.monotonic() + 10
                    while not report.exists():
                        self.assertIsNone(
                            controller.poll(), (tmp / "controller.log").read_text()
                        )
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(0.1)
                    signal.write_text("operator completed")
                    self.assertEqual(
                        controller.wait(25),
                        0,
                        (tmp / "controller.log").read_text() + report.read_text(),
                    )
                result = json.loads(report.read_text())
                self.assertTrue(result["completed_capture"])
                self.assertTrue(result["ended_by_stop_signal"])
                self.assertFalse(result["completed_duration"])
                self.assertEqual(result["stopped"]["state"], "stopped")
                self.assertIsNone(edge.poll())
                before = report.read_bytes()
                stale = subprocess.run(command, capture_output=True, timeout=10)
                self.assertEqual(stale.returncode, 2)
                self.assertEqual(report.read_bytes(), before)
                self.assertEqual(len(server.request("GET", "/api/sessions")), 1)
            finally:
                for child in (controller, edge):
                    if child and child.poll() is None:
                        child.terminate()
                        try:
                            child.wait(10)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait()
                server.stop()


if __name__ == "__main__":
    unittest.main()
