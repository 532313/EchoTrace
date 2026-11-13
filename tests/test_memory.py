import hashlib
import asyncio
import io
import wave
import struct
import json
import sqlite3
import tempfile
import unittest
import zipfile
from unittest.mock import patch
from pathlib import Path

from echotrace.config import Settings
from echotrace.ai import ModelUnavailable
from echotrace.memory import MemoryService
from echotrace.protocol import EventIn, MediaIn, Packet, QueryIn, SessionCreate
from echotrace.store import Conflict, NotFound, Store


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(
            Path(self.temp.name),
            min_free_bytes=0,
            model_config=Path(self.temp.name) / "absent-models.json",
        )
        self.settings.prepare()
        self.store = Store(self.settings.root)
        self.service = MemoryService(self.store, self.settings)
        self.project = self.store.create_project("排线维修")
        self.session = self.store.create_session(
            SessionCreate(
                project_id=self.project["id"],
                device_id="edge",
                boot_id="boot-a",
                source_time_us=1_000_000,
                title="更换排线",
            )
        )

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def event(self, seq=0, note="因为旧排线接触不良，所以更换排线", **extra):
        data = dict(
            id=f"event-{seq}",
            session_id=self.session["id"],
            device_id="edge",
            boot_id="boot-a",
            seq=seq,
            source_time_us=2_000_000 + seq * 1000,
            kind="mark",
            note=note,
        )
        data.update(extra)
        return EventIn(**data)

    def media(self, content=b"original media", **extra):
        data = dict(
            id="media-a",
            session_id=self.session["id"],
            device_id="edge",
            boot_id="boot-a",
            start_us=1_000_000,
            end_us=4_000_000,
            kind="video",
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            mime="video/mp4",
        )
        data.update(extra)
        item = self.store.register_media(MediaIn(**data))
        path = self.settings.root / "media" / (item["id"] + ".mp4")
        path.write_bytes(content)
        return self.store.attach_media(
            item["id"], str(path.relative_to(self.settings.root))
        )

    def test_packet_wire_contract(self):
        packed = Packet(1, 0x01020304, 0x0102030405060708, 640, b"abc").pack()
        self.assertEqual(
            packed[:24],
            struct.pack("!HHIIQI", 3, 1, 3, 0x01020304, 0x0102030405060708, 640),
        )
        self.assertEqual(Packet.unpack(packed).payload, b"abc")
        for invalid in (packed + b"x", packed[:20], packed[:3] + b"\xff" + packed[4:]):
            with self.assertRaises(ValueError):
                Packet.unpack(invalid)

    def test_idempotent_replay_and_conflicting_content(self):
        item = self.event()
        self.assertFalse(self.store.ingest(item)["duplicate"])
        self.assertTrue(self.store.ingest(item)["duplicate"])
        with self.assertRaises(Conflict):
            self.store.ingest(item.model_copy(update={"note": "different"}))
        with self.assertRaises(Conflict):
            self.store.ingest(item.model_copy(update={"id": "another-id"}))
        self.assertEqual(self.store.stats()["anchors"], 1)

    def test_legacy_marks_replay_without_rewriting_original_events(self):
        legacy = self.event(kind="pedal")
        self.store.ingest(legacy)
        self.store.ingest(self.event(1, kind="mark"))
        self.assertEqual(self.store.stats()["anchors"], 2)
        raw = self.store.one("SELECT body FROM events WHERE id=?", (legacy.id,))
        self.assertEqual(json.loads(raw["body"])["kind"], "pedal")
        self.assertEqual(self.store.anchor("event-1")["kind"], "mark")

    def test_boot_identity_and_late_replay(self):
        with self.assertRaises(Conflict):
            self.store.ingest(self.event(boot_id="boot-b"))
        self.store.set_state(self.session["id"], "stopped", 5_000_000)
        self.store.ingest(self.event())
        with self.assertRaises(Conflict):
            self.store.ingest(self.event(1, source_time_us=6_000_000))
        with self.assertRaises(Conflict):
            self.store.set_state(self.session["id"], "recording", 6_000_000)

    def test_edit_preserves_raw_fact_and_checks_revision(self):
        self.store.ingest(self.event())
        self.store.derive("event-0", "caption", "自动识别为排线", "test-model")
        self.store.update_note("event-0", "用户修正：线缆接触不良", 0)
        with self.assertRaises(Conflict):
            self.store.update_note("event-0", "覆盖", 0)
        raw = json.loads(self.store.one("SELECT body FROM events")["body"])
        self.assertIn("因为旧排线", raw["note"])
        self.assertEqual(self.store.anchor("event-0")["caption"], "自动识别为排线")
        self.assertEqual(len(self.store.rows("SELECT * FROM note_history")), 1)

    def test_cause_search_unknown_and_visual_unavailable(self):
        self.store.ingest(self.event())
        self.store.ingest(self.event(1, note="更换排线之前保存接线图"))
        result = self.service.query(
            QueryIn(text="为什么更换排线", project_id=self.project["id"])
        )
        self.assertIn("接触不良", result["answer"])
        self.assertEqual(result["mode"], "lexical")
        self.assertEqual(
            self.service.query(QueryIn(text="火星飞船天线库存"))["status"],
            "insufficient",
        )
        self.assertEqual(
            self.service.query(QueryIn(text="更换排线之前是什么样"))["status"],
            "unavailable",
        )

    def test_media_identity_corruption_and_export(self):
        self.store.ingest(self.event())
        media = self.media()
        self.assertEqual(self.store.evidence("event-0")["status"], "found")
        export_id, path = self.service.export(["event-0"])
        with zipfile.ZipFile(path) as bundle:
            payload = json.loads(bundle.read("evidence.json"))
            self.assertEqual(
                payload["evidence"][0]["original_observation"]["id"], "event-0"
            )
            self.assertEqual(
                bundle.read(payload["files"][0]["path"]), b"original media"
            )
        self.store.managed_path(media["path"]).write_bytes(b"changed bytes!")
        with self.assertRaises(Conflict):
            self.service.export(["event-0"])
        self.assertFalse(self.store.verify_media(self.store.media("media-a")))

    def test_deleted_session_cannot_resurrect(self):
        self.store.ingest(self.event())
        self.media()
        with self.assertRaises(Conflict):
            self.store.delete_session(self.session["id"])
        self.store.set_state(self.session["id"], "stopped", 5_000_000)
        self.store.delete_session(self.session["id"])
        self.assertEqual(self.store.stats()["anchors"], 0)
        self.assertEqual(len(self.store.rows("SELECT * FROM tombstones")), 1)
        self.assertEqual(list((self.settings.root / "media").iterdir()), [])
        with self.assertRaises(NotFound):
            self.store.ingest(self.event())

    def test_vad_child_preserves_source_and_parent_export(self):
        event = self.event(kind="voice", note="", payload={"media_id": "media-a"})
        self.store.ingest(event)
        self.media()
        child = self.store.derive_speech(
            event.id, 16000, 32000, "因为接触不良而更换排线", "asr-test", 0, 1_000_000
        )
        self.assertEqual(self.store.anchor(child)["source_time_us"], 2_000_000)
        self.assertEqual(self.store.anchor(event.id)["hidden"], 1)
        progress = self.service.query(
            QueryIn(text="上次做到哪一步", project_id=self.project["id"])
        )
        self.assertEqual([hit["anchor"]["id"] for hit in progress["hits"]], [child])
        result = self.service.query(QueryIn(text="为什么更换排线"))
        self.assertEqual(result["hits"][0]["anchor"]["id"], child)
        _, path = self.service.export([child])
        with zipfile.ZipFile(path) as bundle:
            data = json.loads(bundle.read("evidence.json"))
            self.assertEqual(
                data["evidence"][0]["original_observation"]["id"], event.id
            )

    def test_legacy_schema_migration_preserves_history_and_cascade(self):
        event = self.event()
        self.store.ingest(event)
        self.store.update_note(event.id, "更换前先断电", 0)
        self.store.enqueue_job("caption", event.id)
        self.store.close()
        connection = sqlite3.connect(self.settings.root / "memory.sqlite3")
        schema = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='anchors'"
        ).fetchone()[0]
        schema = schema.replace(
            "CREATE TABLE anchors", "CREATE TABLE anchors_legacy", 1
        )
        schema = schema.replace(
            "id TEXT PRIMARY KEY,",
            "id TEXT PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,",
            1,
        )
        connection.execute(schema)
        connection.execute("INSERT INTO anchors_legacy SELECT * FROM anchors")
        connection.execute("DROP TABLE anchors")
        connection.execute("ALTER TABLE anchors_legacy RENAME TO anchors")
        connection.execute("PRAGMA user_version=1")
        connection.commit()
        connection.close()
        self.store = Store(self.settings.root)
        self.service = MemoryService(self.store, self.settings)
        self.assertEqual(self.store.anchor(event.id)["note"], "更换前先断电")
        self.assertEqual(len(self.store.rows("SELECT * FROM note_history")), 1)
        child = self.store.derive_speech(event.id, 0, 16000, "因为接触不良", "test", 0)
        self.assertEqual(self.store.anchor(child)["parent_event_id"], event.id)
        self.assertEqual(self.store.rows("PRAGMA foreign_key_check"), [])
        self.assertEqual(self.store.rows("PRAGMA user_version")[0]["user_version"], 2)
        self.store.set_state(self.session["id"], "stopped", 5_000_000)
        self.store.delete_session(self.session["id"])
        for table in ("anchors", "events", "jobs", "note_history"):
            self.assertEqual(self.store.rows("SELECT * FROM " + table), [])

    def test_asr_worker_indexes_a_derived_utterance(self):
        stream = io.BytesIO()
        with wave.open(stream, "wb") as output:
            output.setparams((1, 2, 16000, 0, "NONE", "PCM"))
            output.writeframes(b"\0\0" * 48000)
        self.media(stream.getvalue(), kind="audio", mime="audio/wav")
        # ASR must not queue a large, unrelated video while its WAV is ready.
        video = self.store.media("media-a")
        self.store.register_media(
            MediaIn(
                **{
                    **{key: video[key] for key in MediaIn.model_fields},
                    "id": "uncached-video",
                    "kind": "video",
                    "mime": "video/mp4",
                }
            )
        )
        requested = []

        async def request_media(media_id):
            requested.append(media_id)

        self.service.request_media = request_media
        event = self.event(kind="voice", note="", payload={"media_id": "media-a"})
        self.store.ingest(event)
        self.service.schedule(event)

        async def process():
            await self.service.start()
            try:
                for _ in range(60):
                    await asyncio.sleep(0.05)
                    job = self.store.one("SELECT * FROM jobs WHERE kind='asr'")
                    if job["status"] in ("done", "failed", "blocked"):
                        return job
                self.fail("ASR worker did not finish")
            finally:
                await self.service.stop()

        with patch.object(
            self.service.ai, "status", return_value={"asr": {"status": "files_present"}}
        ), patch.object(
            self.service.ai, "speech_regions", return_value=[(16000, 32000)]
        ), patch.object(
            self.service.ai,
            "transcribe",
            return_value=("因为接触不良而更换排线", "stub-asr"),
        ), patch.object(
            self.service.ai, "embed", side_effect=ModelUnavailable("not installed")
        ):
            job = asyncio.run(process())
        self.assertEqual(job["status"], "done", job["detail"])
        self.assertEqual(requested, [])
        child = self.store.anchor(event.id + "-speech-0")
        self.assertEqual(child["source_time_us"], 2_000_000)
        self.assertEqual(child["parent_event_id"], event.id)
        self.assertIn(
            child["id"],
            [row["anchor"]["id"] for row in self.store.search("为什么更换排线")],
        )

    def test_speech_window_joins_original_chunks_with_bounded_time(self):
        def pcm(value):
            stream = io.BytesIO()
            with wave.open(stream, "wb") as output:
                output.setparams((1, 2, 16000, 0, "NONE", "PCM"))
                output.writeframes(struct.pack("<h", value) * 16000)
            return stream.getvalue()

        self.media(
            pcm(111),
            id="audio-before",
            kind="audio",
            mime="audio/wav",
            start_us=1_000_000,
            end_us=2_000_000,
        )
        current = self.media(
            pcm(222),
            id="audio-after",
            kind="audio",
            mime="audio/wav",
            start_us=2_000_000,
            end_us=3_000_000,
        )
        path, begin, boundary, count, ids, temporary = self.service.speech_window(
            current
        )
        try:
            self.assertEqual(
                (begin, boundary, count, temporary), (1_000_000, 16000, 32000, True)
            )
            self.assertEqual(ids, ["audio-before", "audio-after"])
            with wave.open(str(path), "rb") as stream:
                self.assertEqual(struct.unpack("<h", stream.readframes(1))[0], 111)
                stream.setpos(16000)
                self.assertEqual(struct.unpack("<h", stream.readframes(1))[0], 222)
        finally:
            path.unlink(missing_ok=True)

    def test_single_writer_owns_the_data_directory(self):
        with self.assertRaises(Conflict):
            Store(self.settings.root)

    def test_legacy_nonfinite_measurement_rejected(self):
        with self.assertRaises(ValueError):
            self.store.ingest(
                self.event(kind="tray", payload={"delta_g": float("nan")})
            )

    def test_manual_job_preserved_auto_queue_bounded(self):
        for i in range(8):
            event = self.event(i)
            self.store.ingest(event)
            self.store.enqueue_job("caption", event.id, 10, max_pending=2)
        self.assertEqual(len(self.store.rows("SELECT * FROM jobs")), 2)
        self.store.enqueue_job("caption", "event-7", 1, max_pending=2)
        self.assertEqual(self.store.claim_job()["anchor_id"], "event-7")

    def test_skipped_scene_jobs_replenish_only_with_media_and_free_slots(self):
        for i in range(5):
            event = self.event(i, kind="scene", note="")
            self.store.ingest(event)
            self.store.enqueue_job("caption", event.id, 10, max_pending=2)
        self.assertEqual(self.store.replenish_caption_jobs(2), [])
        for job in self.store.rows("SELECT id FROM jobs"):
            self.store.finish_job(job["id"], "done")
        self.assertEqual(self.store.replenish_caption_jobs(2), [])
        self.media()
        self.assertEqual(
            self.store.replenish_caption_jobs(2), ["event-2", "event-3"]
        )
        self.assertEqual(self.store.replenish_caption_jobs(2), [])
        self.assertEqual(
            self.store.rows("SELECT count(*) AS n FROM jobs WHERE status='pending'")[0]["n"],
            2,
        )
        for job in self.store.rows("SELECT id FROM jobs WHERE status='pending'"):
            self.store.finish_job(job["id"], "done")
        self.assertEqual(self.store.replenish_caption_jobs(2), ["event-4"])

    def test_cache_eviction_preserves_local_original(self):
        self.media(content=b"1" * 100)
        self.media(
            content=b"2" * 100, id="media-b", start_us=4_000_000, end_us=6_000_000
        )
        with self.store.transaction():
            self.store.db.execute("UPDATE media SET authoritative=1 WHERE id='media-a'")
        self.store.trim_cache(100)
        self.assertTrue(self.store.verify_media(self.store.media("media-a")))
        self.assertIsNone(self.store.media("media-b")["path"])


if __name__ == "__main__":
    unittest.main()
