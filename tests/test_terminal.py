import asyncio
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
                asyncio.run(check())
            finally:
                store.close()
