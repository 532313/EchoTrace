from __future__ import annotations
import asyncio
import base64
import hashlib
import json
import os
import platform
import shutil
import subprocess
import time
import zipfile
import wave
from pathlib import Path

import numpy as np
from .ai import LocalAI, ModelUnavailable
from .store import Conflict, NotFound, canonical, identifier
from .protocol import QueryIn


class MemoryService:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings
        self.ai = LocalAI(settings.models())
        self.worker_tasks = []
        self.stopping = False
        self.request_media = None
        self.errors = []
        self.on_voice_result = None

    async def start(self):
        self.worker_tasks = [
            asyncio.create_task(self._worker(("asr", "embedding"))),
            asyncio.create_task(self._worker(("caption",))),
        ]

    async def stop(self):
        self.stopping = True
        for task in self.worker_tasks:
            task.cancel()
        await asyncio.gather(*self.worker_tasks, return_exceptions=True)

    def schedule(self, event):
        if event.kind not in ("mark", "pedal", "voice", "scene"):
            return
        priority = 1 if event.kind in ("mark", "pedal", "voice") else 10
        if event.kind == "voice":
            self.store.enqueue_job("asr", event.id, priority, self.settings.max_jobs)
            if event.payload.get("media_id"):
                return
        self.store.enqueue_job("caption", event.id, priority, self.settings.max_jobs)

    def embed_anchor(self, anchor_id):
        anchor = self.store.anchor(anchor_id)
        text = " ".join(anchor[k] for k in ("note", "asr_text", "caption")).strip()
        if not text:
            return
        vector = self.ai.embed([text])[0].astype("<f4")
        with self.store.transaction():
            self.store.db.execute(
                "INSERT OR REPLACE INTO embeddings VALUES(?,?,?,?,?)",
                (
                    anchor_id,
                    self.ai.options["embedding"].get("revision", "local-unsealed"),
                    hashlib.sha256(text.encode()).hexdigest(),
                    vector.tobytes(),
                    len(vector),
                ),
            )

    def query(self, data):
        semantic = None
        mode = "lexical"
        warnings = []
        if data.semantic:
            try:
                query = self.ai.embed(
                    ["为这个句子生成表示以用于检索相关文章：" + data.text]
                )[0]
                records = self.store.rows(
                    """SELECT e.*,s.project_id,a.session_id FROM embeddings e JOIN anchors a
                      ON a.id=e.anchor_id JOIN sessions s ON s.id=a.session_id"""
                )
                scored = []
                revision = self.ai.options["embedding"].get(
                    "revision", "local-unsealed"
                )
                for record in records:
                    if record["model"] != revision or record["dimensions"] != len(
                        query
                    ):
                        continue
                    if (
                        data.project_id
                        and record["project_id"] != data.project_id
                        or data.session_id
                        and record["session_id"] != data.session_id
                    ):
                        continue
                    vector = np.frombuffer(record["vector"], dtype="<f4")
                    scored.append((record["anchor_id"], float(np.dot(query, vector))))
                semantic = sorted(scored, key=lambda r: r[1], reverse=True)[:40]
                mode = "hybrid"
            except ModelUnavailable:
                warnings.append("语义模型未就绪，当前使用中文全文检索。")
        hits = self.store.search(
            data.text, data.project_id, data.session_id, data.top_k, semantic
        )
        if not hits:
            return {
                "status": "insufficient",
                "answer": "没有找到可以核实这件事的记录。可以补充项目、物品名称或当时的口述。",
                "hits": [],
                "mode": mode,
                "warnings": warnings,
            }
        visual = any(
            s in data.text
            for s in ("画面", "视频", "接线图", "什么样", "长什么", "照片")
        )
        available = [h for h in hits if h["evidence"]["status"] == "found"]
        if visual and not available:
            return {
                "status": "unavailable",
                "answer": "找到相关记录，但原始画面尚未缓存或已经不可用。请连接记录终端并获取证据。",
                "hits": hits,
                "mode": mode,
                "warnings": warnings,
            }
        chosen = available[0] if visual else hits[0]
        anchor = chosen["anchor"]
        seconds = (anchor["source_time_us"] - anchor["started_us"]) / 1_000_000
        prefix = f"在「{anchor['title'] or '未命名会话'}」的 {int(seconds // 60):02d}:{int(seconds % 60):02d}，"
        if anchor["note"] or anchor["asr_text"]:
            answer = (
                prefix
                + "你记录了：“"
                + (anchor["note"] or anchor["asr_text"])[:280]
                + "”。"
            )
        elif anchor["caption"]:
            answer = (
                prefix
                + "自动画面描述为：“"
                + anchor["caption"][:220]
                + "”。请以原图为准。"
            )
        else:
            answer = prefix + "存在一个操作标记，请查看对应原始片段。"
        if any(x in data.text for x in ("最后", "放哪", "哪里")):
            answer += "这是最后检索到的相关记录，不能证明物品现在仍在那里。"
        if not chosen["evidence"]["media"]:
            warnings.append("该条只有文字/事件记录，尚无关联的原始音视频。")
        return {
            "status": "found",
            "answer": answer,
            "hits": hits,
            "mode": mode,
            "warnings": warnings,
        }

    def extract_frame(self, anchor_id):
        evidence = self.store.evidence(anchor_id)
        stamp = evidence["anchor"]["source_time_us"]
        media = next(
            (
                m
                for m in evidence["media"]
                if m["kind"] == "video"
                and m["available"]
                and m["start_us"] <= stamp < m["end_us"]
            ),
            None,
        )
        if media is None:
            raise NotFound("no cached video covering this source timestamp")
        output = (
            self.settings.root
            / "frames"
            / (hashlib.sha256(anchor_id.encode()).hexdigest() + ".jpg")
        )
        tmp = output.with_suffix(".part.jpg")
        offset = (stamp - media["start_us"]) / 1_000_000
        command = [
            self.settings.ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(self.store.managed_path(media["path"])),
            "-ss",
            f"{offset:.6f}",
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(tmp),
        ]
        decode = {"backend": "ffmpeg", "source_time_us": stamp, "media_id": media["id"]}
        try:
            hardware = Path(self.settings.frame_worker)
            extracted = False
            if platform.machine() in ("aarch64", "arm64") and hardware.is_file():
                try:
                    completed = subprocess.run(
                        [
                            str(hardware.resolve()),
                            "--input",
                            str(self.store.managed_path(media["path"])),
                            "--seconds",
                            str(offset),
                            "--output",
                            str(tmp),
                        ],
                        check=True,
                        capture_output=True,
                        text=True,
                        timeout=15,
                    )
                    reports = [
                        line
                        for line in completed.stdout.splitlines()
                        if line.startswith('{"backend":')
                    ]
                    if not reports:
                        raise ValueError(
                            "hardware decoder did not report its source timestamp"
                        )
                    report = json.loads(reports[-1])
                    if abs(report["relative_time_us"] - int(offset * 1e6)) > 200000:
                        raise ValueError(
                            "hardware frame is outside the requested source interval"
                        )
                    decode.update(report)
                    extracted = True
                except (subprocess.SubprocessError, ValueError, OSError) as error:
                    self.errors.append("硬件取帧回退：" + str(error)[:200])
                    self.errors[:] = self.errors[-20:]
            if not extracted:
                subprocess.run(command, check=True, timeout=30, capture_output=True)
            if not tmp.exists() or tmp.stat().st_size == 0:
                raise RuntimeError("decoder produced no frame")
            os.replace(tmp, output)
        except subprocess.SubprocessError as error:
            with self.store.transaction():
                self.store.db.execute(
                    "UPDATE media SET status='unreadable' WHERE id=?", (media["id"],)
                )
            raise NotFound("原始片段无法解码，已标记为不可用") from error
        finally:
            if tmp.exists():
                tmp.unlink()
        with self.store.transaction():
            derivation = json.loads(self.store.anchor(anchor_id)["derivation"])
            derivation["frame"] = decode
            self.store.db.execute(
                "UPDATE anchors SET frame_path=?,derivation=? WHERE id=?",
                (
                    str(output.relative_to(self.settings.root)),
                    canonical(derivation),
                    anchor_id,
                ),
            )
        return output

    def terminal_thumbnail(self, result):
        if not result.get("hits"):
            return None
        anchor_id = result["hits"][0]["anchor"]["id"]
        frame = self.extract_frame(anchor_id)
        completed = subprocess.run(
            [
                self.settings.ffmpeg,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(frame),
                "-vf",
                "scale=160:90:force_original_aspect_ratio=decrease,pad=160:90:(ow-iw)/2:(oh-ih)/2",
                "-frames:v",
                "1",
                "-pix_fmt",
                "rgb565le",
                "-f",
                "rawvideo",
                "pipe:1",
            ],
            capture_output=True,
            check=True,
            timeout=10,
        )
        if len(completed.stdout) != 160 * 90 * 2:
            raise ValueError("unexpected terminal thumbnail layout")
        return {
            "format": "rgb565le",
            "width": 160,
            "height": 90,
            "data": base64.b64encode(completed.stdout).decode("ascii"),
        }

    def speech_window(self, media):
        """Use the preceding segment so an utterance is not cut at a file boundary."""
        current = self.store.managed_path(media["path"])
        with wave.open(str(current), "rb") as stream:
            if stream.getparams()[:3] != (1, 2, 16000) or stream.getnframes() > 320000:
                raise ValueError("unsupported source audio window")
            samples = stream.getnframes()
            content = stream.readframes(samples)
        previous = self.store.rows(
            """SELECT * FROM media WHERE session_id=? AND boot_id=? AND kind='audio'
            AND end_us<=? AND end_us>=? ORDER BY end_us DESC LIMIT 1""",
            (
                media["session_id"],
                media["boot_id"],
                media["start_us"],
                media["start_us"] - 200000,
            ),
        )
        if not previous or previous[0]["discontinuity"]:
            return current, media["start_us"], 0, samples, [media["id"]], False
        prior = previous[0]
        if not self.store.verify_media(prior):
            raise NotFound("waiting for preceding audio context")
        gap = round((media["start_us"] - prior["end_us"]) * 16000 / 1e6)
        with wave.open(str(self.store.managed_path(prior["path"])), "rb") as stream:
            if stream.getparams()[:3] != (1, 2, 16000) or stream.getnframes() > 320000:
                raise ValueError("unsupported preceding audio")
            keep = min(stream.getnframes(), max(0, 320000 - samples - gap))
            stream.setpos(stream.getnframes() - keep)
            prefix = stream.readframes(keep)
        boundary = keep + gap
        path = self.settings.root / "staging" / (identifier() + ".wav")
        with wave.open(str(path), "wb") as stream:
            stream.setparams((1, 2, 16000, 0, "NONE", "PCM"))
            stream.writeframes(prefix + bytes(gap * 2) + content)
        begin = media["start_us"] - boundary * 1_000_000 // 16000
        return (
            path,
            begin,
            boundary,
            boundary + samples,
            [prior["id"], media["id"]],
            True,
        )

    def summary(self, session_id):
        session = self.store.session(session_id)
        anchors = self.store.rows(
            "SELECT * FROM anchors WHERE session_id=? AND hidden=0 ORDER BY source_time_us",
            (session_id,),
        )
        lines = []
        for anchor in anchors[-30:]:
            text = (
                anchor["note"]
                or anchor["asr_text"]
                or (
                    "自动描述：" + anchor["caption"]
                    if anchor["caption"]
                    else "操作标记"
                )
            )
            elapsed = (anchor["source_time_us"] - session["started_us"]) // 1_000_000
            lines.append(f"{elapsed // 60:02d}:{elapsed % 60:02d} {text}")
        summary = "\n".join(lines) or "本次会话没有可汇总的步骤记录。"
        with self.store.transaction():
            self.store.db.execute(
                "UPDATE sessions SET summary=? WHERE id=?", (summary, session_id)
            )
        return {
            "summary": summary,
            "source": "chronological_observations",
            "anchor_ids": [a["id"] for a in anchors[-30:]],
        }

    def export(self, anchor_ids):
        evidences = [
            self.store.evidence(anchor_id) for anchor_id in dict.fromkeys(anchor_ids)
        ]
        missing = [m["id"] for e in evidences for m in e["media"] if not m["available"]]
        if missing:
            raise Conflict(
                "fetch all referenced media before export: "
                + ",".join(sorted(set(missing)))
            )
        export_id = identifier()
        path = self.settings.root / "exports" / (export_id + ".zip")
        temporary = path.with_suffix(".part")
        total_bytes = sum(
            media["size_bytes"]
            for media in {m["id"]: m for e in evidences for m in e["media"]}.values()
        )
        if (
            shutil.disk_usage(path.parent).free
            < total_bytes + self.settings.min_free_bytes
        ):
            raise Conflict("insufficient free storage for evidence export")
        manifest = {
            "format": "echotrace-evidence-v1",
            "created_ms": int(time.time() * 1000),
            "evidence": evidences,
            "files": [],
            "limitations": "Descriptions are derived; source media and observations are preserved.",
        }
        try:
            with zipfile.ZipFile(
                temporary, "w", compression=zipfile.ZIP_STORED
            ) as bundle:
                seen = set()
                for evidence in evidences:
                    for media in evidence["media"]:
                        if media["id"] in seen:
                            continue
                        seen.add(media["id"])
                        source = self.store.managed_path(media["path"])
                        name = (
                            "media/"
                            + hashlib.sha256(media["id"].encode()).hexdigest()
                            + source.suffix
                        )
                        digest_state = hashlib.sha256()
                        with source.open("rb") as stream, bundle.open(
                            name, "w", force_zip64=True
                        ) as output:
                            for chunk in iter(lambda: stream.read(65536), b""):
                                digest_state.update(chunk)
                                output.write(chunk)
                        digest = digest_state.hexdigest()
                        if digest != media["sha256"]:
                            raise Conflict(
                                "source media checksum changed; export refused"
                            )
                        manifest["files"].append(
                            {"media_id": media["id"], "path": name, "sha256": digest}
                        )
                    event_id = (
                        evidence["anchor"].get("parent_event_id")
                        or evidence["anchor"]["id"]
                    )
                    event = self.store.one(
                        "SELECT body FROM events WHERE id=?", (event_id,)
                    )
                    evidence["original_observation"] = json.loads(event["body"])
                bundle.writestr(
                    "evidence.json", json.dumps(manifest, ensure_ascii=False, indent=2)
                )
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return export_id, path

    async def _worker(self, kinds):
        next_replenish = 0.0
        while not self.stopping:
            job = self.store.claim_job(kinds)
            if not job:
                if kinds == ("caption",) and time.monotonic() >= next_replenish:
                    self.store.replenish_caption_jobs(self.settings.max_jobs)
                    next_replenish = time.monotonic() + 10
                await asyncio.sleep(0.3)
                continue
            try:
                anchor = self.store.anchor(job["anchor_id"])
                evidence = self.store.evidence(anchor["id"])
                required_kind = "video" if job["kind"] == "caption" else "audio"
                missing = [
                    m
                    for m in evidence["media"]
                    if not m["available"]
                    and m["kind"] == required_kind
                    and m["start_us"] <= anchor["source_time_us"] < m["end_us"]
                ]
                if missing and self.request_media:
                    await self.request_media(missing[0]["id"])
                if job["kind"] == "caption":
                    if self.ai.status()["vlm"]["status"] == "unavailable":
                        raise ModelUnavailable("VLM local assets are not installed")
                    image = await asyncio.to_thread(self.extract_frame, anchor["id"])
                    text, model = await asyncio.to_thread(self.ai.caption, image)
                    self.store.derive(
                        anchor["id"],
                        "caption",
                        text,
                        model,
                        str(image.relative_to(self.settings.root)),
                    )
                elif job["kind"] == "asr":
                    if self.ai.status()["asr"]["status"] == "unavailable":
                        raise ModelUnavailable("ASR local assets are not installed")
                    media = next(
                        (
                            m
                            for m in evidence["media"]
                            if m["kind"] == "audio"
                            and m["available"]
                            and m["start_us"] <= anchor["source_time_us"] < m["end_us"]
                        ),
                        None,
                    )
                    if not media:
                        raise NotFound("waiting for audio segment")
                    if media["discontinuity"]:
                        self.store.finish_job(
                            job["id"],
                            "done",
                            "audio discontinuity or assistant playback; excluded from user notes",
                        )
                        continue
                    path = self.store.managed_path(media["path"])
                    (
                        window,
                        source_begin,
                        boundary,
                        sample_count,
                        context_ids,
                        temporary,
                    ) = await asyncio.to_thread(self.speech_window, media)
                    try:
                        regions = await asyncio.to_thread(
                            self.ai.speech_regions, window
                        )
                        session = self.store.session(anchor["session_id"])
                        next_segment = self.store.rows(
                            "SELECT id FROM media WHERE session_id=? AND kind='audio' AND start_us>=? AND start_us<=? LIMIT 1",
                            (
                                media["session_id"],
                                media["end_us"],
                                media["end_us"] + 200000,
                            ),
                        )
                        deferred = False
                        processed = (
                            set(json.loads(job["detail"][17:]))
                            if job["detail"].startswith("speech_processed:")
                            else set()
                        )
                        for index, (start, end) in enumerate(regions):
                            if index in processed or end <= boundary:
                                continue
                            if end >= sample_count - 512 and (
                                next_segment or session["state"] == "recording"
                            ):
                                # The next job includes this file as context. If recording ends
                                # without another file, retry and finalize the remaining utterance.
                                deferred = not bool(next_segment)
                                continue
                            await self._transcribe_region(
                                window,
                                anchor,
                                media,
                                start,
                                end,
                                index,
                                source_begin,
                                context_ids,
                            )
                            processed.add(index)
                        if deferred:
                            self.store.finish_job(
                                job["id"],
                                "pending",
                                "speech_processed:" + json.dumps(sorted(processed)),
                                retry_seconds=2,
                            )
                            continue
                    finally:
                        if temporary:
                            window.unlink(missing_ok=True)
                try:
                    await asyncio.to_thread(self.embed_anchor, anchor["id"])
                except ModelUnavailable:
                    pass
                self.store.finish_job(job["id"], "done")
            except ModelUnavailable as e:
                self.store.finish_job(job["id"], "blocked", str(e))
            except (NotFound, Conflict) as e:
                # A manual marker may precede the close/upload of its source segment.
                self.store.finish_job(
                    job["id"],
                    "pending",
                    str(e),
                    retry_seconds=min(120, 2 ** min(job["attempts"], 6)),
                )
            except asyncio.CancelledError:
                self.store.finish_job(job["id"], "pending", "worker shutdown")
                raise
            except Exception as e:
                self.errors.append(str(e)[:300])
                self.errors[:] = self.errors[-20:]
                self.store.finish_job(job["id"], "failed", str(e))

    async def _transcribe_region(
        self, path, anchor, media, start, end, index, source_begin, context_ids
    ):
        text, model = await asyncio.to_thread(self.ai.transcribe, path, start, end)
        if text:
            prefixes = ("查一下", "查一查", "回看一下", "查询一下")
            spoken = text.strip(" ，。,.！!")
            prefix = next((p for p in prefixes if spoken.startswith(p)), None)
            if prefix and self.on_voice_result:
                question = spoken[len(prefix) :].strip(" ，。,.！!")
                if question:
                    result = await asyncio.to_thread(
                        self.query,
                        QueryIn(text=question, project_id=anchor["project_id"]),
                    )
                    await self.on_voice_result(anchor["device_id"], result)
                return
            child_id = self.store.derive_speech(
                anchor["id"], start, end, text, model, index, source_begin, context_ids
            )
            try:
                await asyncio.to_thread(self.embed_anchor, child_id)
            except ModelUnavailable:
                pass
