import asyncio
import base64
import io
import wave
import tempfile
import unittest
from pathlib import Path
from echotrace.config import Settings
from echotrace.memory import MemoryService
from echotrace.protocol import EventIn, SessionCreate
from echotrace.store import Store
from echotrace.terminal import TerminalApp


class TerminalHistoryTests(unittest.TestCase):
    def test_history_pagination_and_text_survive_missing_original_media(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(Path(directory), min_free_bytes=0,
                                model_config=Path(directory) / "absent.json")
            settings.prepare()
            store = Store(settings.root)
            try:
                project = store.create_project("小屏项目")
                anchor_id = ""
                for index in range(9):
                    begin = 1_000_000 + index * 10_000_000
                    session = store.create_session(SessionCreate(
                        project_id=project["id"], device_id="echo", boot_id="boot",
                        source_time_us=begin, title=f"第 {index} 次操作"))
                    anchor_id = f"step-{index}"
                    store.ingest(EventIn(id=anchor_id, session_id=session["id"],
                        device_id="echo", boot_id="boot", source_time_us=begin + 1_000,
                        seq=index, kind="mark", note="更换排线是因为接触不良"))
                    store.set_state(session["id"], "stopped", begin + 2_000_000)
                app = TerminalApp(settings, store, MemoryService(store, settings), None, None)

                async def check():
                    first = await app.handle("echo", {}, "history.sessions", {})
                    second = await app.handle("echo", {}, "history.sessions", {"offset": 8})
                    self.assertEqual(len(first["items"]), 8)
                    self.assertTrue(first["more"])
                    self.assertEqual(len(second["items"]), 1)
                    self.assertFalse(second["more"])
                    self.assertNotIn(second["items"][0]["id"],
                                     [s["id"] for s in first["items"]])
                    timeline = await app.handle("echo", {}, "history.anchors",
                                                {"session_id": session["id"]})
                    self.assertEqual(timeline["items"][0]["id"], anchor_id)
                    detail = await app.handle("echo", {}, "anchor.open", {"anchor_id": anchor_id})
                    self.assertEqual(detail["note"], "更换排线是因为接触不良")
                    self.assertFalse(detail["pending"])
                    self.assertNotIn("thumbnail", detail)
                    query = await app.handle("echo", {}, "query",
                                             {"text": "为什么更换排线", "project_id": project["id"]})
                    self.assertTrue(query["hits"])
                    self.assertEqual(query["transcript"], "为什么更换排线")
                    before = store.stats()["anchors"]
                    buffer = io.BytesIO()
                    with wave.open(buffer, "wb") as audio:
                        audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(16000)
                        audio.writeframes(bytes(3200))
                    app.memory.ai.transcribe = lambda path: ("项目叫排线维修", "mock-asr")
                    named = await app.handle("echo", {}, "voice", {
                        "mode": "project.name", "audio": base64.b64encode(buffer.getvalue()).decode()})
                    self.assertEqual(named["transcript"], "排线维修")
                    self.assertEqual(store.stats()["anchors"], before)
                    self.assertFalse(list((settings.root / "staging").glob("question-*.wav")))
                asyncio.run(check())
            finally:
                store.close()


class ScreenPlaybackTests(unittest.TestCase):
    def test_play_pause_and_seek_across_video_segments(self):
        import shutil
        if not shutil.which("ffmpeg"):
            self.skipTest("ffmpeg is not installed")
        from echotrace.cli import seed_demo
        from echotrace.terminal_playback import ScreenPlayer
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            demo = seed_demo(root)
            settings = Settings(root, min_free_bytes=0, model_config=root / "absent.json")
            store = Store(root)

            async def check():
                queue, commands = asyncio.Queue(), []

                class Socket:
                    async def send_json(self, message):
                        await queue.put(message)

                class Devices:
                    connected = {"echo": {"capture_state": "stopped", "socket": Socket(),
                                          "send_lock": asyncio.Lock()}}

                    async def command(self, device_id, operation, arguments=None):
                        commands.append((operation, arguments))
                        return {"ok": True}

                player = ScreenPlayer(settings, store, Devices())

                async def frame(player_id):
                    while True:
                        message = await asyncio.wait_for(queue.get(), 4)
                        if message.get("id") != player_id:
                            continue
                        if message["type"] == "screen.frame":
                            self.assertEqual(len(base64.b64decode(message["data"])), 28800)
                            return message
                        if message.get("state") == "paused":
                            self.fail(message.get("message"))

                try:
                    first = await player.start("echo", demo["anchors"][0])
                    await frame(first["id"])
                    paused = await player.pause("echo")
                    self.assertEqual(paused["state"], "paused")
                    second = await player.resume("echo", 4500)
                    picture = await frame(second["id"])
                    self.assertGreaterEqual(picture["position_ms"], 4500)
                    self.assertTrue(any(op == "playback.start" for op, _ in commands))
                    await player.stop("echo")
                    self.assertEqual(player.states["echo"]["state"], "stopped")
                finally:
                    await player.close()

            try:
                asyncio.run(check())
            finally:
                store.close()
