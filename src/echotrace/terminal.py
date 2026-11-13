"""Bounded small-screen views over the existing store and memory services."""
from __future__ import annotations
import asyncio
import subprocess
from .protocol import SessionCreate
from .store import Conflict, NotFound


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

    async def handle(self, device_id, peer, action, args):
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
