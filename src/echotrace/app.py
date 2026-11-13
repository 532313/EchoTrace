from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import platform
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from . import __version__
from .config import Settings
from .ai import ModelUnavailable
from .memory import MemoryService
from .terminal import TerminalApp
from .playback import make_playback_plan
from .protocol import (
    VERSION,
    AUDIO_OPUS,
    MEDIA_CHUNK,
    EventIn,
    ExportIn,
    MediaIn,
    NoteUpdate,
    Packet,
    ProjectCreate,
    QueryIn,
    SessionCreate,
    StateChange,
)
from .store import Conflict, NotFound, Store, identifier

logger = logging.getLogger(__name__)


class Upload:
    def __init__(self, settings, store, media_id):
        self.settings, self.store = settings, store
        self.item = store.media(media_id)
        if (
            shutil.disk_usage(settings.root).free
            < self.item["size_bytes"] + settings.min_free_bytes
        ):
            raise OSError("insufficient storage reserve")
        self.path = settings.root / "staging" / (identifier() + ".part")
        self.stream = self.path.open("xb")
        self.size, self.sequence = 0, 0
        self.digest = hashlib.sha256()

    def write(self, chunk):
        self.size += len(chunk)
        if self.size > self.item["size_bytes"]:
            raise ValueError("upload exceeds its declared length")
        self.digest.update(chunk)
        self.stream.write(chunk)

    def finish(self):
        if (
            self.size != self.item["size_bytes"]
            or self.digest.hexdigest() != self.item["sha256"]
        ):
            raise ValueError("upload checksum or length mismatch")
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.stream.close()
        suffix = {"video": ".mp4", "audio": ".wav", "image": ".jpg"}[self.item["kind"]]
        relative = (
            "media/" + hashlib.sha256(self.item["id"].encode()).hexdigest() + suffix
        )
        os.replace(self.path, self.settings.root / relative)
        item = self.store.attach_media(self.item["id"], relative)
        with self.store.transaction():
            self.store.db.execute(
                """UPDATE jobs SET available_ms=0 WHERE status='pending' AND
                anchor_id IN (SELECT id FROM anchors WHERE session_id=?)""",
                (item["session_id"],),
            )
        self.store.trim_cache(self.settings.cache_limit_bytes, protected_id=item["id"])
        return item

    def abort(self):
        if not self.stream.closed:
            self.stream.close()
        self.path.unlink(missing_ok=True)


class Devices:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings
        self.connected = {}
        self.fetching = {}

    async def command(self, device_id, operation, arguments=None, timeout=8):
        peer = self.connected.get(device_id)
        if not peer:
            raise Conflict("记录终端未连接；命令未执行")
        request_id = identifier()
        future = asyncio.get_running_loop().create_future()
        peer["pending"][request_id] = future
        try:
            async with peer["send_lock"]:
                await peer["socket"].send_json(
                    {
                        "type": "command",
                        "id": request_id,
                        "op": operation,
                        "arguments": arguments or {},
                    }
                )
            reply = await asyncio.wait_for(future, timeout)
            if not reply.get("ok"):
                raise Conflict(reply.get("error", "device rejected command"))
            return reply
        except (asyncio.TimeoutError, WebSocketDisconnect, OSError) as error:
            raise Conflict("记录终端未确认命令；请检查连接和终端状态") from error
        except RuntimeError as error:
            if peer["socket"].application_state.name != "CONNECTED":
                raise Conflict("记录终端连接已关闭；命令未确认") from error
            raise
        finally:
            peer["pending"].pop(request_id, None)

    async def fetch(self, media_id):
        item = self.store.media(media_id)
        if self.store.verify_media(item):
            return {"status": "ready"}
        if time.monotonic() - self.fetching.get(media_id, -100) < 10:
            return {"status": "requested"}
        result = await self.command(
            item["device_id"], "media.request", {"media_id": media_id}
        )
        self.fetching[media_id] = time.monotonic()
        return result

    async def send_audio(self, device_id, path):
        size = path.stat().st_size
        if size > 2 * 1024 * 1024:
            raise ValueError("spoken feedback exceeds the bounded audio cache")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        audio_id = "speech-" + identifier()
        await self.command(
            device_id,
            "playback.cache.begin",
            {"id": audio_id, "size_bytes": size, "sha256": digest},
        )
        peer = self.connected.get(device_id)
        if not peer:
            raise Conflict("device disconnected during audio feedback")
        async with peer["send_lock"]:
            with path.open("rb") as source:
                sequence = 0
                for chunk in iter(lambda: source.read(65536), b""):
                    await peer["socket"].send_bytes(
                        Packet(MEDIA_CHUNK, sequence, 0, 0, chunk).pack()
                    )
                    sequence += 1
        return await self.command(device_id, "playback.cache.end", {"id": audio_id})

    def list(self):
        now = time.monotonic()
        return [
            {
                "device_id": key,
                "boot_id": peer["boot_id"],
                "source_time_us": peer["source_time_us"],
                "last_seen_seconds": now - peer["seen"],
                "capabilities": peer["capabilities"],
                "session_id": peer.get("session_id"),
                "capture_state": peer.get("capture_state", "unknown"),
            }
            for key, peer in self.connected.items()
        ]


def create_app(settings=None):
    settings = settings or Settings.from_env()
    settings.prepare()
    store = Store(settings.root)
    memory = MemoryService(store, settings)
    devices = Devices(store, settings)
    memory.request_media = devices.fetch

    async def voice_result(device_id, result):
        peer = devices.connected.get(device_id)
        if not peer:
            return
        try:
            result["thumbnail"] = await asyncio.to_thread(
                memory.terminal_thumbnail, result
            )
        except (NotFound, ValueError):
            pass
        async with peer["send_lock"]:
            await peer["socket"].send_json({"type": "query.result", "result": result})
        path = settings.root / "staging" / (identifier() + ".wav")
        try:
            await asyncio.to_thread(memory.ai.speak, result["answer"][:80], path)
            await devices.send_audio(device_id, path)
        except (ModelUnavailable, Conflict) as error:
            memory.errors.append("语音反馈：" + str(error)[:250])
            memory.errors[:] = memory.errors[-20:]
        finally:
            path.unlink(missing_ok=True)

    memory.on_voice_result = voice_result

    @asynccontextmanager
    async def lifespan(app):
        await memory.start()
        yield
        await memory.stop()
        await terminal.player.close()
        for peer in list(devices.connected.values()):
            with contextlib.suppress(Exception):
                await peer["socket"].close(code=1001)
        store.close()

    app = FastAPI(title="EchoTrace", version=__version__, lifespan=lifespan)
    app.state.store, app.state.memory, app.state.devices, app.state.settings = (
        store,
        memory,
        devices,
        settings,
    )

    def valid_token(authorization):
        return not settings.token or hmac.compare_digest(
            authorization or "", "Bearer " + settings.token
        )

    @app.middleware("http")
    async def authentication(request, call_next):
        if request.url.path.startswith("/api/") and not valid_token(
            request.headers.get("authorization")
        ):
            return JSONResponse({"detail": "authentication required"}, status_code=401)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(NotFound)
    async def not_found(request, error):
        return JSONResponse({"detail": str(error)}, status_code=404)

    @app.exception_handler(Conflict)
    async def conflict(request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.exception_handler(ValueError)
    async def invalid(request, error):
        return JSONResponse({"detail": str(error)}, status_code=422)

    @app.exception_handler(OSError)
    async def storage_error(request, error):
        return JSONResponse({"detail": str(error)}, status_code=507)

    @app.exception_handler(ModelUnavailable)
    async def model_unavailable(request, error):
        return JSONResponse({"detail": str(error)}, status_code=503)

    @app.get("/api/status")
    def status():
        meminfo = {}
        if Path("/proc/meminfo").exists():
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.split(":")[0] in (
                    "MemAvailable",
                    "CmaFree",
                    "CmaTotal",
                    "SwapFree",
                ):
                    k, value = line.split(":")
                    meminfo[k] = int(value.strip().split()[0]) * 1024
        return {
            "name": "EchoTrace",
            "version": __version__,
            "protocol": VERSION,
            "offline": True,
            "stats": store.stats(),
            "models": memory.ai.status(),
            "devices": devices.list(),
            "memory": meminfo,
            "platform": platform.machine(),
            "errors": memory.errors,
            "storage_free_bytes": shutil.disk_usage(settings.root).free,
            "source_time_us": time.monotonic_ns() // 1000,
            "capabilities": {
                "capture_requires_edge": True,
                "local_media_import": False,
                "lexical_search": True,
                "semantic_search": memory.ai.status()["embedding"]["status"]
                == "files_present",
            },
        }

    @app.get("/api/projects")
    def projects():
        return store.rows("SELECT * FROM projects ORDER BY created_ms DESC")

    @app.post("/api/projects", status_code=201)
    async def project_create(data: ProjectCreate):
        project = store.create_project(data.name)
        await publish_projects()
        return project

    async def publish_projects():
        message = {
            "type": "projects.changed",
            "projects": store.rows(
                "SELECT * FROM projects ORDER BY created_ms DESC LIMIT 100"
            ),
        }
        for peer in tuple(devices.connected.values()):
            with contextlib.suppress(Exception):
                async with peer["send_lock"]:
                    await peer["socket"].send_json(message)

    @app.patch("/api/projects/{project_id}")
    async def project_rename(project_id: str, data: ProjectCreate):
        project = store.rename_project(project_id, data.name)
        await publish_projects()
        return project

    @app.get("/api/sessions")
    def sessions(project_id: str = None):
        return store.rows(
            "SELECT * FROM sessions"
            + (" WHERE project_id=?" if project_id else "")
            + " ORDER BY created_ms DESC",
            (project_id,) if project_id else (),
        )

    @app.post("/api/sessions", status_code=201)
    async def session_create(data: SessionCreate):
        if data.device_id not in devices.connected:
            raise Conflict(
                "记录终端未连接。请连接 Echo-Mate，或启动明确标注的模拟采集端。"
            )
        peer = devices.connected[data.device_id]
        if peer["boot_id"] != data.boot_id:
            raise Conflict("device rebooted; refresh its source clock")
        session = store.create_session(data)
        try:
            reply = await devices.command(
                data.device_id, "session.start", {"session": session}, timeout=15
            )
            peer["session_id"] = session["id"]
            peer["capture_state"] = "recording"
            return {**session, "capture": reply}
        except Exception:
            store.mark_interrupted(data.device_id, data.boot_id)
            raise

    @app.get("/api/sessions/{session_id}")
    def session_get(session_id: str):
        return store.session(session_id)

    @app.patch("/api/sessions/{session_id}/state")
    async def session_state(session_id: str, data: StateChange):
        session = store.session(session_id)
        peer = devices.connected.get(session["device_id"])
        if peer is None or peer["boot_id"] != session["boot_id"]:
            raise Conflict(
                "终端未连接到本次启动，命令未执行。它可能仍在本地记录；可在终端停止，重连后同步状态。"
            )
        reply = await devices.command(
            session["device_id"],
            "session."
            + {"recording": "resume", "paused": "pause", "stopped": "stop"}[data.state],
            {"session_id": session_id},
            timeout=15,
        )
        peer["capture_state"] = data.state
        return store.set_state(
            session_id,
            data.state,
            max(
                int(reply.get("source_time_us", data.source_time_us)),
                session["last_us"],
            ),
        )

    @app.post("/api/sessions/{session_id}/mark")
    async def mark(session_id: str, data: dict):
        session = store.session(session_id)
        if session["state"] != "recording":
            raise Conflict("session is not recording")
        note = data.get("note", "")
        if not isinstance(note, str) or len(note) > 8000:
            raise ValueError("invalid note")
        return await devices.command(
            session["device_id"], "mark", {"session_id": session_id, "note": note}
        )

    @app.get("/api/sessions/{session_id}/timeline")
    def timeline(session_id: str):
        session = store.session(session_id)
        return {
            "session": session,
            "anchors": store.rows(
                "SELECT * FROM anchors WHERE session_id=? AND hidden=0 ORDER BY source_time_us",
                (session_id,),
            ),
            "events": store.rows(
                "SELECT id,source_time_us,kind,body FROM events WHERE session_id=? AND kind IN ('gap','sensor_fault','playback') ORDER BY source_time_us",
                (session_id,),
            ),
            "media": store.rows(
                "SELECT * FROM media WHERE session_id=? ORDER BY start_us",
                (session_id,),
            ),
        }

    @app.post("/api/sessions/{session_id}/summary")
    def summary(session_id: str):
        return memory.summary(session_id)

    @app.delete("/api/sessions/{session_id}")
    async def session_delete(session_id: str):
        session = store.session(session_id)
        store.delete_session(session_id)
        deleted = False
        if session["device_id"] in devices.connected:
            try:
                await devices.command(
                    session["device_id"], "session.delete", {"session_id": session_id}
                )
                deleted = True
                with store.transaction():
                    store.db.execute(
                        "UPDATE tombstones SET source_deleted=1 WHERE session_id=?",
                        (session_id,),
                    )
            except Conflict:
                pass
        return {
            "deleted": session_id,
            "source_deletion": "done" if deleted else "pending_reconnect",
        }

    @app.post("/api/events")
    def event_ingest(data: EventIn):
        result = store.ingest(data)
        if not result["duplicate"]:
            memory.schedule(data)
        return result

    @app.get("/api/anchors/{anchor_id}")
    def anchor_get(anchor_id: str):
        evidence = store.evidence(anchor_id)
        evidence["playback"] = make_playback_plan(evidence)
        return evidence

    @app.patch("/api/anchors/{anchor_id}/note")
    def note_update(anchor_id: str, data: NoteUpdate):
        result = store.update_note(anchor_id, data.note, data.revision)
        store.enqueue_job("embedding", anchor_id, 1)
        return result

    @app.post("/api/anchors/{anchor_id}/frame")
    async def extract_frame(anchor_id: str):
        path = await asyncio.to_thread(memory.extract_frame, anchor_id)
        return {
            "url": f"/api/anchors/{anchor_id}/frame",
            "size_bytes": path.stat().st_size,
        }

    @app.get("/api/anchors/{anchor_id}/frame")
    def frame_get(anchor_id: str):
        anchor = store.anchor(anchor_id)
        if (
            not anchor["frame_path"]
            or not store.managed_path(anchor["frame_path"]).is_file()
        ):
            raise NotFound("keyframe has not been extracted")
        return FileResponse(
            store.managed_path(anchor["frame_path"]), media_type="image/jpeg"
        )

    @app.post("/api/query")
    async def query(data: QueryIn):
        return await asyncio.to_thread(memory.query, data)

    @app.post("/api/playback")
    async def playback(data: dict):
        item = store.media(str(data.get("media_id", "")))
        if item["kind"] != "audio":
            raise ValueError("playback requires source audio")
        offset = float(data.get("offset_seconds", 0))
        if not 0 <= offset < (item["end_us"] - item["start_us"]) / 1e6:
            raise ValueError("playback offset is outside the audio segment")
        return await devices.command(
            item["device_id"],
            "playback.start",
            {"media_id": item["id"], "offset_seconds": offset},
        )

    @app.post("/api/playback/stop")
    async def playback_stop():
        for device_id in tuple(devices.connected):
            await devices.command(device_id, "playback.stop")
        return {"stopped": True}

    @app.post("/api/speech")
    async def speech(data: dict):
        text = data.get("text", "")
        if not isinstance(text, str) or not 0 < len(text) <= 400:
            raise ValueError("expected 1..400 characters")
        device_id = data.get("device_id") or next(iter(devices.connected), None)
        if not device_id:
            raise Conflict("recording terminal is not connected")
        path = settings.root / "staging" / (identifier() + ".wav")
        try:
            await asyncio.to_thread(memory.ai.speak, text[:80], path)
            return await devices.send_audio(device_id, path)
        finally:
            path.unlink(missing_ok=True)

    @app.post("/api/models/check")
    async def model_check(data: dict):
        """Operator smoke check through the same instances used by the service."""
        if getattr(app.state, "model_check_running", False):
            raise Conflict("a model check is already running")
        app.state.model_check_running = True
        samples = []
        running = True

        async def sample_memory():
            while running:
                if Path("/proc/meminfo").exists():
                    values = dict(
                        line.split(":", 1)
                        for line in Path("/proc/meminfo").read_text().splitlines()
                    )
                    samples.append(int(values["MemAvailable"].split()[0]) * 1024)
                await asyncio.sleep(0.1)

        monitor = asyncio.create_task(sample_memory())
        results = {}
        try:
            for role in ("vad", "asr", "embedding", "tts", "vlm"):
                started = time.monotonic()
                try:
                    if role in ("vad", "asr"):
                        source = memory.ai.options.get("asr", {}).get("test_audio", "")
                        if not source or not Path(source).is_file():
                            raise ModelUnavailable(
                                "configure the bundled ASR test_audio first"
                            )
                        if role == "vad":
                            output = await asyncio.to_thread(
                                memory.ai.speech_regions, Path(source)
                            )
                        else:
                            output = (
                                await asyncio.to_thread(
                                    memory.ai.transcribe, Path(source)
                                )
                            )[0]
                    elif role == "embedding":
                        vector = await asyncio.to_thread(
                            memory.ai.embed, ["排线接触不良", "需要更换旧线缆"]
                        )
                        output = {
                            "shape": list(vector.shape),
                            "norms": (vector * vector).sum(axis=1).tolist(),
                        }
                    elif role == "tts":
                        path = settings.root / "staging" / (identifier() + ".wav")
                        try:
                            await asyncio.to_thread(
                                memory.ai.speak, "过程记忆已经准备好。", path
                            )
                            output = {"pcm_wav_bytes": path.stat().st_size}
                        finally:
                            path.unlink(missing_ok=True)
                    else:
                        if not data.get("anchor_id"):
                            raise ModelUnavailable(
                                "provide an anchor_id with cached video for VLM checking"
                            )
                        frame = await asyncio.to_thread(
                            memory.extract_frame, data["anchor_id"]
                        )
                        output = (await asyncio.to_thread(memory.ai.caption, frame))[0]
                    results[role] = {
                        "ok": True,
                        "seconds": time.monotonic() - started,
                        "output": output,
                    }
                except Exception as error:
                    results[role] = {
                        "ok": False,
                        "seconds": time.monotonic() - started,
                        "error": str(error)[:1000],
                    }
        finally:
            running = False
            await monitor
            app.state.model_check_running = False
        return {
            "quality_evaluation": False,
            "results": results,
            "minimum_MemAvailable": min(samples) if samples else None,
            "memory_samples": len(samples),
            "models": memory.ai.status(),
        }

    @app.get("/api/compare")
    def compare(left: str, right: str):
        return {"left": store.evidence(left), "right": store.evidence(right)}

    @app.post("/api/media", status_code=201)
    def media_register(data: MediaIn):
        return store.register_media(data)

    @app.post("/api/media/{media_id}/fetch")
    async def fetch_media(media_id: str):
        return await devices.fetch(media_id)

    @app.put("/api/media/{media_id}/content")
    async def upload_media(media_id: str, request: Request):
        upload = Upload(settings, store, media_id)
        try:
            async for chunk in request.stream():
                upload.write(chunk)
            return upload.finish()
        finally:
            upload.abort()

    @app.get("/api/media/{media_id}/content")
    def media_content(media_id: str, request: Request):
        item = store.media(media_id)
        if not store.verify_media(item):
            raise NotFound("media is not cached; connect the source and fetch it")
        with store.lock:
            store.db.execute(
                "UPDATE media SET accessed_ms=? WHERE id=?",
                (int(time.time() * 1000), media_id),
            )
        # Starlette FileResponse implements single/multipart HTTP Range, HEAD and 416.
        return FileResponse(
            store.managed_path(item["path"]),
            media_type=item["mime"],
            headers={"Accept-Ranges": "bytes"},
        )

    @app.get("/api/jobs")
    def jobs():
        return store.rows("SELECT * FROM jobs ORDER BY created_ms DESC LIMIT 200")

    @app.post("/api/jobs/retry")
    def retry_jobs():
        with store.transaction():
            cursor = store.db.execute(
                "UPDATE jobs SET status='pending',available_ms=0 WHERE status IN ('failed','blocked')"
            )
        return {"requeued": cursor.rowcount}

    @app.post("/api/export")
    async def export(data: ExportIn):
        export_id, path = await asyncio.to_thread(memory.export, data.anchor_ids)
        return {
            "id": export_id,
            "url": f"/api/export/{export_id}",
            "size_bytes": path.stat().st_size,
        }

    @app.get("/api/export/{export_id}")
    def export_get(export_id: str):
        try:
            import uuid

            export_id = str(uuid.UUID(export_id))
        except ValueError:
            raise NotFound("invalid export id")
        path = settings.root / "exports" / (export_id + ".zip")
        if not path.is_file():
            raise NotFound("export does not exist")
        return FileResponse(
            path, media_type="application/zip", filename="EchoTrace-evidence.zip"
        )

    terminal = TerminalApp(settings, store, memory, devices, session_create)

    @app.websocket("/ws/device")
    async def device_socket(websocket: WebSocket):
        if not valid_token(websocket.headers.get("authorization")):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        peer, upload, device_id = None, None, None
        tasks = set()

        def background(coroutine):
            task = asyncio.create_task(coroutine)
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        try:
            hello = await asyncio.wait_for(websocket.receive_json(), 5)
            if hello.get("type") != "hello" or hello.get("version") != VERSION:
                raise ValueError("protocol v3 hello required")
            device_id, boot_id = hello.get("device_id"), hello.get("boot_id")
            if (
                not isinstance(device_id, str)
                or not 0 < len(device_id) <= 128
                or not isinstance(boot_id, str)
                or not 0 < len(boot_id) <= 128
            ):
                raise ValueError("invalid device identity")
            peer = {
                "socket": websocket,
                "boot_id": boot_id,
                "source_time_us": int(hello.get("source_time_us", 0)),
                "seen": time.monotonic(),
                "capabilities": hello.get("capabilities", []),
                "pending": {},
                "send_lock": asyncio.Lock(),
                "session_id": hello.get("session_id"),
                "capture_state": hello.get("capture_state", "stopped"),
            }
            previous = devices.connected.get(device_id)
            devices.connected[device_id] = peer
            if previous:
                with contextlib.suppress(WebSocketDisconnect, OSError, RuntimeError):
                    await previous["socket"].close(code=1012)
            if devices.connected.get(device_id) is not peer:
                await websocket.close(code=1012)
                return
            with store.transaction():
                store.db.execute(
                    """UPDATE jobs SET available_ms=0 WHERE status='pending' AND
                    anchor_id IN (SELECT a.id FROM anchors a JOIN sessions s ON s.id=a.session_id WHERE s.device_id=?)""",
                    (device_id,),
                )
            await websocket.send_json(
                {
                    "type": "hello",
                    "version": VERSION,
                    "accepted": True,
                    "media_chunk_ack": True,
                    "projects": store.rows(
                        "SELECT * FROM projects ORDER BY created_ms DESC"
                    ),
                }
            )

            async def delete_sources():
                for item in store.rows(
                    "SELECT session_id FROM tombstones WHERE device_id=? AND source_deleted=0",
                    (device_id,),
                ):
                    try:
                        await devices.command(
                            device_id,
                            "session.delete",
                            {"session_id": item["session_id"]},
                        )
                        with store.transaction():
                            store.db.execute(
                                "UPDATE tombstones SET source_deleted=1 WHERE session_id=?",
                                (item["session_id"],),
                            )
                    except Conflict:
                        break

            background(delete_sources())
            if peer["session_id"]:
                old = store.rows(
                    "SELECT * FROM sessions WHERE id=? AND device_id=? AND boot_id=?",
                    (peer["session_id"], device_id, boot_id),
                )
                if (
                    old
                    and old[0]["state"] == "interrupted"
                    and peer["capture_state"] in ("recording", "paused")
                ):
                    store.set_state(
                        old[0]["id"],
                        peer["capture_state"],
                        max(old[0]["last_us"], peer["source_time_us"]),
                    )

            async def intent(data):
                try:
                    args = data.get("arguments", {})
                    operation = data.get("op")
                    if operation == "session.start":
                        project_id = args.get("project_id")
                        if not project_id:
                            projects = store.rows(
                                "SELECT id FROM projects ORDER BY created_ms DESC LIMIT 1"
                            )
                            project_id = (
                                projects[0]["id"]
                                if projects
                                else store.create_project("工作台记录")["id"]
                            )
                        await session_create(
                            SessionCreate(
                                project_id=project_id,
                                device_id=device_id,
                                boot_id=boot_id,
                                source_time_us=peer["source_time_us"],
                                title=args.get("title", "工作台记录"),
                            )
                        )
                        await publish_projects()
                    else:
                        sid = args.get("session_id") or peer.get("session_id")
                        if not sid:
                            raise Conflict("no active session")
                        if operation == "mark":
                            await mark(sid, {"note": args.get("note", "")})
                        elif operation in (
                            "session.pause",
                            "session.resume",
                            "session.stop",
                        ):
                            await session_state(
                                sid,
                                StateChange(
                                    state={
                                        "session.pause": "paused",
                                        "session.resume": "recording",
                                        "session.stop": "stopped",
                                    }[operation],
                                    source_time_us=peer["source_time_us"],
                                ),
                            )
                        else:
                            raise ValueError("unknown terminal intent")
                except Exception as error:
                    with contextlib.suppress(Exception):
                        await websocket.send_json(
                            {"type": "error", "error": str(error)}
                        )

            async def terminal_call(data):
                reply = {
                    "type": "app.reply",
                    "id": str(data.get("id", ""))[:100],
                    "action": str(data.get("action", ""))[:60],
                    "ok": False,
                }
                try:
                    args = data.get("arguments", {})
                    if not isinstance(args, dict):
                        raise ValueError("invalid app arguments")
                    action = reply["action"]
                    if action == "projects":
                        result = projects()
                    elif action == "project.create":
                        result = await project_create(ProjectCreate.model_validate(args))
                    elif action == "project.rename":
                        result = await project_rename(
                            str(args.get("project_id", "")),
                            ProjectCreate(name=args.get("name", "")),
                        )
                    else:
                        result = await terminal.handle(device_id, peer, action, args)
                    reply.update(ok=True, data=result)
                except (ValueError, Conflict, NotFound, ValidationError, ModelUnavailable) as error:
                    reply["error"] = str(error)[:500]
                except Exception:
                    logger.exception("Terminal action failed: %s", reply["action"])
                    reply["error"] = "操作未完成，请稍后重试"
                async with peer["send_lock"]:
                    await websocket.send_json(reply)

            while True:
                message = await asyncio.wait_for(websocket.receive(), 35)
                if message["type"] == "websocket.disconnect":
                    break
                peer["seen"] = time.monotonic()
                if message.get("bytes") is not None:
                    packet = Packet.unpack(message["bytes"])
                    if packet.kind == MEDIA_CHUNK:
                        if upload is None or packet.seq != upload.sequence:
                            raise ValueError("media chunk is missing or out of order")
                        upload.write(packet.payload)
                        upload.sequence += 1
                        if "media_chunk_ack" in peer["capabilities"]:
                            await websocket.send_json(
                                {
                                    "type": "media.chunk.ack",
                                    "media_id": upload.item["id"],
                                    "sequence": packet.seq,
                                }
                            )
                    elif packet.kind == AUDIO_OPUS:
                        # Original WAV segments are authoritative and replayable after reconnect.
                        # Live Opus is validated here; ASR jobs consume the closed source WAV.
                        peer["source_time_us"] = packet.source_time_us
                    continue
                data = json.loads(message.get("text", ""))
                kind = data.get("type")
                if kind == "ack":
                    future = peer["pending"].get(data.get("id"))
                    if future and not future.done():
                        future.set_result(data)
                elif kind == "intent":
                    # Keep receiving the ACK while a terminal intent awaits command completion.
                    background(intent(data))
                elif kind == "screen.stop":
                    background(terminal.player.stop(device_id))
                elif kind == "app.request":
                    # Keep receiving command ACKs while the requested action awaits
                    # the recorder. Only one app task per device is admitted.
                    if peer.get("app_task") and not peer["app_task"].done():
                        async with peer["send_lock"]:
                            await websocket.send_json({
                                "type": "app.reply", "id": data.get("id"),
                                "action": data.get("action"), "ok": False,
                                "error": "上一项操作正在处理，请稍候",
                            })
                    else:
                        task = asyncio.create_task(terminal_call(data))
                        peer["app_task"] = task
                        tasks.add(task)
                        task.add_done_callback(tasks.discard)
                elif kind == "heartbeat":
                    peer["source_time_us"] = int(data["source_time_us"])
                    peer["capture_state"] = data.get(
                        "capture_state", peer["capture_state"]
                    )
                    peer["session_id"] = data.get("session_id", peer["session_id"])
                    active = store.rows(
                        "SELECT * FROM sessions WHERE id=? AND device_id=? AND boot_id=? AND state='interrupted'",
                        (peer["session_id"], device_id, boot_id),
                    )
                    if active and peer["capture_state"] in ("recording", "paused"):
                        store.set_state(
                            active[0]["id"],
                            peer["capture_state"],
                            max(active[0]["last_us"], peer["source_time_us"]),
                        )
                    await websocket.send_json({"type": "heartbeat", "accepted": True})
                elif kind == "event":
                    event = EventIn.model_validate(data["event"])
                    if event.device_id != device_id:
                        raise ValueError(
                            "connection cannot publish another source identity"
                        )
                    if store.rows(
                        "SELECT 1 FROM tombstones WHERE session_id=? AND device_id=?",
                        (event.session_id, device_id),
                    ):
                        await websocket.send_json(
                            {"type": "event.ack", "event_id": event.id, "deleted": True}
                        )
                        continue
                    result = store.ingest(event)
                    if not result["duplicate"]:
                        memory.schedule(event)
                    await websocket.send_json({"type": "event.ack", **result})
                elif kind == "session.reconcile":
                    if store.rows(
                        "SELECT 1 FROM tombstones WHERE session_id=? AND device_id=?",
                        (data["session_id"], device_id),
                    ):
                        continue
                    session = store.session(data["session_id"])
                    if session["device_id"] != device_id:
                        raise Conflict("cannot reconcile another source identity")
                    state = data["state"]
                    stamp = int(data["source_time_us"])
                    # The durable journal replays earlier boot cycles too.
                    # Their old paused/recording updates are history; only a
                    # stopped update may close an interrupted old session.
                    stale = (
                        session["boot_id"] != boot_id and state != "stopped"
                    ) or (
                        session["state"] == "stopped" and state != "stopped"
                    ) or (
                        stamp < session["last_us"] and state != "stopped"
                    )
                    if (
                        state in ("recording", "paused", "stopped")
                        and state != session["state"]
                        and not stale
                    ):
                        store.set_state(
                            session["id"],
                            state,
                            max(session["last_us"], stamp),
                            reason=(
                                "source_restart"
                                if data.get("reason") == "source_restart"
                                else ""
                            ),
                        )
                    await websocket.send_json(
                        {"type": "session.ack", "session_id": session["id"],
                         "ignored_stale": stale}
                    )
                elif kind == "media.register":
                    item = MediaIn.model_validate(data["media"])
                    if item.device_id != device_id:
                        raise ValueError("media source identity mismatch")
                    if store.rows(
                        "SELECT 1 FROM tombstones WHERE session_id=? AND device_id=?",
                        (item.session_id, device_id),
                    ):
                        continue
                    store.register_media(item)
                    await websocket.send_json(
                        {"type": "media.ack", "media_id": item.id}
                    )
                elif kind == "media.begin":
                    if upload is not None:
                        raise ValueError(
                            f"finish the previous media upload first: active={upload.item['id']} "
                            f"received={upload.size} incoming={data.get('media_id')}"
                        )
                    item = store.media(data["media_id"])
                    if item["device_id"] != device_id:
                        raise ValueError("upload source identity mismatch")
                    upload = Upload(settings, store, item["id"])
                elif kind == "media.end":
                    if upload is None or upload.item["id"] != data["media_id"]:
                        raise ValueError("upload identity mismatch")
                    item = upload.finish()
                    upload.abort()
                    upload = None
                    await websocket.send_json(
                        {"type": "media.complete", "media_id": item["id"]}
                    )
                elif kind == "query":
                    result = await asyncio.to_thread(
                        memory.query, QueryIn.model_validate(data["query"])
                    )
                    try:
                        result["thumbnail"] = await asyncio.to_thread(
                            memory.terminal_thumbnail, result
                        )
                    except (NotFound, ValueError):
                        pass
                    await websocket.send_json(
                        {"type": "query.result", "id": data.get("id"), "result": result}
                    )
                else:
                    raise ValueError("unknown device message")
        except (WebSocketDisconnect, asyncio.TimeoutError):
            pass
        except RuntimeError:
            if websocket.application_state.name != "DISCONNECTED":
                raise
        except (ValueError, Conflict, NotFound, ValidationError) as e:
            logger.warning("Device %s protocol rejected: %s", device_id, e)
            with contextlib.suppress(Exception):
                await websocket.send_json({"type": "error", "error": str(e)[:1000]})
                await websocket.close(code=1008)
        finally:
            for task in tuple(tasks):
                task.cancel()
            if tasks:
                await asyncio.gather(*tuple(tasks), return_exceptions=True)
            if upload:
                upload.abort()
            if peer:
                for future in peer["pending"].values():
                    if not future.done():
                        future.set_exception(
                            Conflict("device disconnected before acknowledgement")
                        )
                if devices.connected.get(device_id) is peer:
                    devices.connected.pop(device_id, None)
                    store.mark_interrupted(device_id, peer["boot_id"])

    app.mount(
        "/",
        StaticFiles(directory=str(Path(__file__).parent / "web"), html=True),
        name="workbench",
    )
    return app
