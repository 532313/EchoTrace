"""Bounded small-screen views over the existing store and memory services."""
from __future__ import annotations
import asyncio
import base64
import io
import subprocess
import wave
from .protocol import SessionCreate, QueryIn
from .store import Conflict, NotFound, identifier


class TerminalApp:
    PAGE_SIZE = 8

    def __init__(self, settings, store, memory, devices, start_session):
        self.settings, self.store, self.memory = settings, store, memory
        self.devices, self.start_session = devices, start_session

    @staticmethod
    def page(args):
        offset = int(args.get("offset", 0))
        if not 0 <= offset <= 100000:
            raise ValueError("invalid page offset")
        return offset

    async def query(self, text, project_id=None):
        query = QueryIn(text=text, project_id=project_id or None, top_k=5)
        result = await asyncio.to_thread(self.memory.query, query)
        return {
            "answer": result["answer"][:700], "status": result["status"],
            "transcript": text[:300], "warnings": result["warnings"][:3],
            "hits": [{"id": h["anchor"]["id"],
                      "label": (h["anchor"]["note"] or h["anchor"]["asr_text"] or
                                h["anchor"]["caption"] or "操作标记")[:150],
                      "seconds": max(0, (h["anchor"]["source_time_us"] -
                                         h["anchor"]["started_us"]) // 1000000)}
                     for h in result["hits"]],
        }

    async def voice(self, args):
        encoded = args.get("audio", "")
        if not isinstance(encoded, str) or len(encoded) > 540000:
            raise ValueError("语音超过 12 秒，请缩短后重试")
        raw = base64.b64decode(encoded, validate=True)
        with wave.open(io.BytesIO(raw), "rb") as sound:
            if (sound.getnchannels(), sound.getsampwidth(), sound.getframerate()) != (1, 2, 16000):
                raise ValueError("语音格式不匹配")
            if not 1 <= sound.getnframes() <= 12 * 16000:
                raise ValueError("语音长度不匹配")
        path = self.settings.root / "staging" / ("question-" + identifier() + ".wav")
        try:
            path.write_bytes(raw)
            text, _ = await asyncio.to_thread(self.memory.ai.transcribe, path)
        finally:
            path.unlink(missing_ok=True)
        if not text.strip():
            raise ValueError("没有听清，请靠近麦克风再说一次")
        mode = args.get("mode", "query")
        if mode == "project.name":
            name = text.strip(" ，。,.！!？?")
            for prefix in ("这个项目叫", "项目名称是", "项目叫", "命名为", "叫做"):
                if name.startswith(prefix):
                    name = name[len(prefix):].strip(" ，。,.：:")
                    break
            return {"mode": mode, "transcript": name[:120]}
        if mode == "note":
            return {"mode": mode, "transcript": text[:600]}
        return {**await self.query(text, args.get("project_id")), "mode": "query"}

    async def handle(self, device_id, peer, action, args):
        if action == "query":
            return await self.query(str(args.get("text", "")), args.get("project_id"))
        if action == "voice":
            return await self.voice(args)
        if action == "speak":
            text = str(args.get("text", "")).strip()
            if not text:
                raise ValueError("没有可朗读的回答")
            path = self.settings.root / "staging" / ("answer-" + identifier() + ".wav")
            try:
                await asyncio.to_thread(self.memory.ai.speak, text[:80], path)
                await self.devices.send_audio(device_id, path)
            finally:
                path.unlink(missing_ok=True)
            return {"spoken": True}

        if action == "history.sessions":
            offset = self.page(args)
            project_id = args.get("project_id")
            where, values = "s.device_id=?", [device_id]
            if project_id:
                where += " AND s.project_id=?"
                values.append(project_id)
            rows = self.store.rows(
                "SELECT s.id,s.project_id,s.title,s.state,s.started_us,s.last_us,"
                "s.ended_us,s.created_ms,p.name project_name,"
                "(SELECT count(*) FROM anchors a WHERE a.session_id=s.id AND a.hidden=0) steps "
                "FROM sessions s JOIN projects p ON p.id=s.project_id WHERE " + where +
                " ORDER BY s.created_ms DESC,s.rowid DESC LIMIT ? OFFSET ?",
                (*values, self.PAGE_SIZE + 1, offset),
            )
            return {"items": rows[:self.PAGE_SIZE], "offset": offset,
                    "more": len(rows) > self.PAGE_SIZE}
        if action == "history.anchors":
            session = self.store.session(str(args.get("session_id", "")))
            offset = self.page(args)
            rows = self.store.rows(
                "SELECT id,kind,source_time_us,substr(note,1,180) note,"
                "substr(asr_text,1,180) asr_text,substr(caption,1,180) caption "
                "FROM anchors WHERE session_id=? AND hidden=0 "
                "ORDER BY source_time_us,id LIMIT ? OFFSET ?",
                (session["id"], self.PAGE_SIZE + 1, offset),
            )
            return {"session": {k: session[k] for k in (
                "id", "project_id", "project_name", "title", "state", "started_us"
            )}, "items": rows[:self.PAGE_SIZE], "offset": offset,
                "more": len(rows) > self.PAGE_SIZE}
        if action == "session.summary":
            result = await asyncio.to_thread(self.memory.summary, str(args.get("session_id", "")))
            return {"summary": result["summary"][:2000], "source": result["source"]}
        if action == "session.continue":
            previous = self.store.session(str(args.get("session_id", "")))
            if peer["capture_state"] not in ("stopped", "unknown"):
                raise Conflict("请先结束正在进行的记忆")
            result = await self.start_session(SessionCreate(
                project_id=previous["project_id"], device_id=device_id,
                boot_id=peer["boot_id"], source_time_us=peer["source_time_us"],
                title=("继续：" + (previous["title"] or previous["project_name"]))[:200],
            ))
            return {"session_id": result["id"], "project_id": previous["project_id"]}
        if action == "anchor.open":
            evidence = self.store.evidence(str(args.get("anchor_id", "")))
            a = evidence["anchor"]
            result = {k: a[k] for k in ("id", "session_id", "revision", "source_time_us")}
            result.update(note=a["note"][:600], asr_text=a["asr_text"][:600],
                          caption=a["caption"][:300],
                          seconds=max(0, (a["source_time_us"] - a["started_us"]) // 1000000))
            video = next((m for m in evidence["media"] if m["kind"] == "video"
                          and m["start_us"] <= a["source_time_us"] < m["end_us"]), None)
            result["pending"] = False
            result["media_message"] = "没有覆盖这一步的画面"
            if video:
                if not video["available"]:
                    try:
                        await self.devices.fetch(video["id"])
                        result.update(pending=True, media_message="原片正在传输，稍后刷新画面")
                    except Conflict:
                        result["media_message"] = "原片在记录端，恢复连接后可查看"
                else:
                    try:
                        result["thumbnail"] = await asyncio.to_thread(
                            self.memory.terminal_thumbnail, {"hits": [{"anchor": a}]})
                        result["media_message"] = "原始画面"
                    except (NotFound, Conflict, ValueError, OSError, subprocess.SubprocessError):
                        result["media_message"] = "此片段暂时不能解码，文字记录仍可查看"
            return result
        raise ValueError("小屏操作暂不支持：" + action)
