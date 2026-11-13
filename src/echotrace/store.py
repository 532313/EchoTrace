from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from .protocol import EventIn, MediaIn, SessionCreate
from .text import fts_query, fts_text, overlap


class Conflict(ValueError):
    pass


class NotFound(LookupError):
    pass


def identifier():
    return str(uuid.uuid4())


def canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY,name TEXT NOT NULL,created_ms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(
 id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),device_id TEXT NOT NULL,
 boot_id TEXT NOT NULL,title TEXT NOT NULL,state TEXT NOT NULL,started_us INTEGER NOT NULL,
 last_us INTEGER NOT NULL,ended_us INTEGER,utc_ms INTEGER,clock_quality TEXT NOT NULL,
 summary TEXT NOT NULL DEFAULT '',created_ms INTEGER NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS one_recording_per_device ON sessions(device_id)
 WHERE state IN ('recording','paused');
CREATE TABLE IF NOT EXISTS events(
 id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
 device_id TEXT NOT NULL,boot_id TEXT NOT NULL,seq INTEGER NOT NULL,source_time_us INTEGER NOT NULL,
 kind TEXT NOT NULL,body TEXT NOT NULL,digest TEXT NOT NULL,created_ms INTEGER NOT NULL,
 UNIQUE(device_id,boot_id,seq));
CREATE TABLE IF NOT EXISTS anchors(
 id TEXT PRIMARY KEY,
 session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,source_time_us INTEGER NOT NULL,
 kind TEXT NOT NULL,note TEXT NOT NULL,asr_text TEXT NOT NULL DEFAULT '',caption TEXT NOT NULL DEFAULT '',
 revision INTEGER NOT NULL DEFAULT 0,frame_path TEXT,derivation TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS note_history(
 anchor_id TEXT NOT NULL REFERENCES anchors(id) ON DELETE CASCADE,revision INTEGER NOT NULL,
 note TEXT NOT NULL,edited_ms INTEGER NOT NULL,PRIMARY KEY(anchor_id,revision));
CREATE VIRTUAL TABLE IF NOT EXISTS anchor_fts USING fts5(anchor_id UNINDEXED,body);
CREATE TABLE IF NOT EXISTS embeddings(
 anchor_id TEXT PRIMARY KEY REFERENCES anchors(id) ON DELETE CASCADE,
 model TEXT NOT NULL,text_digest TEXT NOT NULL,vector BLOB NOT NULL,dimensions INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS media(
 id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
 device_id TEXT NOT NULL,boot_id TEXT NOT NULL,start_us INTEGER NOT NULL,end_us INTEGER NOT NULL,
 kind TEXT NOT NULL,size_bytes INTEGER NOT NULL,sha256 TEXT NOT NULL,mime TEXT NOT NULL,
 discontinuity INTEGER NOT NULL,path TEXT,status TEXT NOT NULL DEFAULT 'remote',
 accessed_ms INTEGER NOT NULL DEFAULT 0,UNIQUE(session_id,kind,start_us));
CREATE TABLE IF NOT EXISTS jobs(
 id TEXT PRIMARY KEY,kind TEXT NOT NULL,anchor_id TEXT REFERENCES anchors(id) ON DELETE CASCADE,
 priority INTEGER NOT NULL,status TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,
 available_ms INTEGER NOT NULL,detail TEXT NOT NULL DEFAULT '',created_ms INTEGER NOT NULL,
 UNIQUE(kind,anchor_id));
CREATE INDEX IF NOT EXISTS anchor_time ON anchors(session_id,source_time_us);
CREATE INDEX IF NOT EXISTS media_time ON media(session_id,boot_id,start_us,end_us);
CREATE INDEX IF NOT EXISTS jobs_pending ON jobs(status,priority,created_ms);
CREATE TABLE IF NOT EXISTS tombstones(
 session_id TEXT PRIMARY KEY,device_id TEXT NOT NULL,boot_id TEXT NOT NULL,
 created_ms INTEGER NOT NULL,source_deleted INTEGER NOT NULL DEFAULT 0);
"""


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._owner = (self.root / ".owner.lock").open("a+")
        try:
            fcntl.flock(self._owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self._owner.close()
            raise Conflict(
                "data directory is already owned by another memory service"
            ) from error
        self.lock = threading.RLock()
        self._verified = {}
        self.db = sqlite3.connect(
            str(root / "memory.sqlite3"), check_same_thread=False, isolation_level=None
        )
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA busy_timeout=5000")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2):
            raise RuntimeError("unsupported database schema; refuse to overwrite")
        self.db.executescript(SCHEMA)
        if "authoritative" not in {
            r[1] for r in self.db.execute("PRAGMA table_info(media)")
        }:
            self.db.execute(
                "ALTER TABLE media ADD COLUMN authoritative INTEGER NOT NULL DEFAULT 0"
            )
        if "parent_event_id" not in {
            r[1] for r in self.db.execute("PRAGMA table_info(anchors)")
        }:
            self.db.execute(
                "ALTER TABLE anchors ADD COLUMN parent_event_id TEXT REFERENCES events(id) ON DELETE CASCADE"
            )
        if "hidden" not in {
            r[1] for r in self.db.execute("PRAGMA table_info(anchors)")
        }:
            self.db.execute(
                "ALTER TABLE anchors ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0"
            )
        if "end_reason" not in {
            r[1] for r in self.db.execute("PRAGMA table_info(sessions)")
        }:
            self.db.execute(
                "ALTER TABLE sessions ADD COLUMN end_reason TEXT NOT NULL DEFAULT ''"
            )
        if any(
            row[3] == "id"
            for row in self.db.execute("PRAGMA foreign_key_list(anchors)")
        ):
            self.db.execute("PRAGMA foreign_keys=OFF")
            try:
                with self.transaction():
                    self.db.execute(
                        """CREATE TABLE anchors_new(
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                        source_time_us INTEGER NOT NULL,kind TEXT NOT NULL,note TEXT NOT NULL,
                        asr_text TEXT NOT NULL DEFAULT '',caption TEXT NOT NULL DEFAULT '',
                        revision INTEGER NOT NULL DEFAULT 0,frame_path TEXT,
                        derivation TEXT NOT NULL DEFAULT '{}',
                        parent_event_id TEXT REFERENCES events(id) ON DELETE CASCADE,
                        hidden INTEGER NOT NULL DEFAULT 0)"""
                    )
                    self.db.execute("INSERT INTO anchors_new SELECT * FROM anchors")
                    self.db.execute("DROP TABLE anchors")
                    self.db.execute("ALTER TABLE anchors_new RENAME TO anchors")
                    self.db.execute(
                        "CREATE INDEX anchor_time ON anchors(session_id,source_time_us)"
                    )
                    if self.db.execute("PRAGMA foreign_key_check").fetchone():
                        raise Conflict(
                            "database migration failed foreign key validation"
                        )
            finally:
                self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA user_version=2")
        self.db.execute(
            "UPDATE jobs SET status='pending',detail='recovered after restart' WHERE status='running'"
        )
        staging = self.root / "staging"
        if staging.is_dir():
            for item in staging.iterdir():
                if item.is_file():
                    item.unlink()

    def close(self):
        with self.lock:
            self.db.close()
            self._owner.close()

    @contextlib.contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def rows(self, sql, args=()):
        with self.lock:
            return [dict(row) for row in self.db.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        values = self.rows(sql, args)
        if not values:
            raise NotFound("record does not exist")
        return values[0]

    def create_project(self, name):
        name = name.strip()
        if not name:
            raise ValueError("project name is empty")
        project = dict(id=identifier(), name=name, created_ms=int(time.time() * 1000))
        with self.transaction():
            self.db.execute(
                "INSERT INTO projects VALUES(:id,:name,:created_ms)", project
            )
        return project

    def rename_project(self, project_id, name):
        name = name.strip()
        if not name or len(name) > 120:
            raise ValueError("项目名称需要 1～120 个字")
        self.one("SELECT id FROM projects WHERE id=?", (project_id,))
        with self.transaction():
            self.db.execute("UPDATE projects SET name=? WHERE id=?", (name, project_id))
        return self.one("SELECT * FROM projects WHERE id=?", (project_id,))

    def create_session(self, data: SessionCreate, session_id: Optional[str] = None):
        self.one("SELECT id FROM projects WHERE id=?", (data.project_id,))
        item = dict(
            id=session_id or identifier(),
            project_id=data.project_id,
            device_id=data.device_id,
            boot_id=data.boot_id,
            title=data.title,
            state="recording",
            started_us=data.source_time_us,
            last_us=data.source_time_us,
            ended_us=None,
            utc_ms=data.utc_ms,
            clock_quality=data.clock_quality,
            summary="",
            created_ms=int(time.time() * 1000),
        )
        if session_id and self.rows(
            "SELECT 1 FROM tombstones WHERE session_id=?", (session_id,)
        ):
            raise Conflict("deleted session id cannot be reused")
        try:
            with self.transaction():
                self.db.execute(
                    """INSERT INTO sessions(id,project_id,device_id,boot_id,title,state,
                    started_us,last_us,ended_us,utc_ms,clock_quality,summary,created_ms) VALUES(:id,:project_id,:device_id,:boot_id,:title,:state,
                    :started_us,:last_us,:ended_us,:utc_ms,:clock_quality,:summary,:created_ms)""",
                    item,
                )
        except sqlite3.IntegrityError as e:
            raise Conflict(
                "device already has an active session, or session id already exists"
            ) from e
        return item

    def session(self, session_id):
        return self.one(
            "SELECT s.*,p.name project_name FROM sessions s JOIN projects p ON p.id=s.project_id WHERE s.id=?",
            (session_id,),
        )

    def set_state(self, session_id, state, stamp, reason=""):
        with self.transaction():
            old = self.session(session_id)
            allowed = {
                "recording": {"paused", "stopped"},
                "paused": {"recording", "stopped"},
                "interrupted": {"recording", "paused", "stopped"},
                "stopped": set(),
            }
            if state == old["state"]:
                return old
            if state not in allowed.get(old["state"], set()):
                raise Conflict("invalid session transition")
            if stamp < old["last_us"]:
                raise Conflict("source clock cannot go backwards")
            self.db.execute(
                "UPDATE sessions SET state=?,last_us=?,ended_us=?,end_reason=? WHERE id=?",
                (
                    state,
                    stamp,
                    stamp if state == "stopped" else None,
                    reason if state == "stopped" else "",
                    session_id,
                ),
            )
        return self.session(session_id)

    def mark_interrupted(self, device_id, boot_id):
        with self.transaction():
            self.db.execute(
                "UPDATE sessions SET state='interrupted' WHERE device_id=? AND boot_id=? AND state IN ('recording','paused')",
                (device_id, boot_id),
            )

    def _index(self, anchor_id):
        anchor = self.anchor(anchor_id)
        text = " ".join(anchor[k] for k in ("note", "asr_text", "caption"))
        self.db.execute("DELETE FROM anchor_fts WHERE anchor_id=?", (anchor_id,))
        self.db.execute("DELETE FROM embeddings WHERE anchor_id=?", (anchor_id,))
        if anchor["hidden"]:
            return
        self.db.execute(
            "INSERT INTO anchor_fts VALUES(?,?)", (anchor_id, fts_text(text))
        )

    def ingest(self, event: EventIn):
        raw = canonical(event.model_dump())
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        with self.transaction():
            duplicate = self.rows(
                "SELECT id,digest FROM events WHERE id=? OR (device_id=? AND boot_id=? AND seq=?)",
                (event.id, event.device_id, event.boot_id, event.seq),
            )
            if duplicate:
                if len(duplicate) != 1 or duplicate[0]["digest"] != digest:
                    raise Conflict("replay identity matches different content")
                return {"event_id": duplicate[0]["id"], "duplicate": True}
            session = self.session(event.session_id)
            if (
                session["device_id"] != event.device_id
                or session["boot_id"] != event.boot_id
            ):
                raise Conflict("event belongs to a different device or boot clock")
            if event.source_time_us < session["started_us"]:
                raise ValueError("event predates session")
            # Replayed observations can arrive out of order, including after session.stop.
            if (
                session["ended_us"] is not None
                and event.source_time_us > session["ended_us"]
            ):
                raise Conflict("event is outside the closed session")
            if event.kind == "tray":
                for key in ("delta_g", "weight_g"):
                    if key in event.payload and (
                        isinstance(event.payload[key], bool)
                        or not isinstance(event.payload[key], (int, float))
                        or not math.isfinite(event.payload[key])
                    ):
                        raise ValueError("tray measurements must be finite")
            self.db.execute(
                "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    event.id,
                    event.session_id,
                    event.device_id,
                    event.boot_id,
                    event.seq,
                    event.source_time_us,
                    event.kind,
                    raw,
                    digest,
                    int(time.time() * 1000),
                ),
            )
            if event.kind in ("mark", "pedal", "voice", "tray", "scene"):
                self.db.execute(
                    "INSERT INTO anchors(id,session_id,source_time_us,kind,note) VALUES(?,?,?,?,?)",
                    (
                        event.id,
                        event.session_id,
                        event.source_time_us,
                        event.kind,
                        event.note,
                    ),
                )
                if (
                    event.kind == "voice"
                    and event.payload.get("media_id")
                    and not event.note
                ):
                    self.db.execute(
                        "UPDATE anchors SET hidden=1 WHERE id=?", (event.id,)
                    )
                self._index(event.id)
            self.db.execute(
                "UPDATE sessions SET last_us=max(last_us,?) WHERE id=?",
                (event.source_time_us, event.session_id),
            )
        return {"event_id": event.id, "duplicate": False}

    def anchor(self, anchor_id):
        return self.one(
            """SELECT a.*,s.project_id,s.device_id,s.boot_id,s.started_us,s.ended_us,s.title,
                      s.clock_quality,s.utc_ms FROM anchors a JOIN sessions s ON s.id=a.session_id WHERE a.id=?""",
            (anchor_id,),
        )

    def update_note(self, anchor_id, note, revision):
        with self.transaction():
            old = self.anchor(anchor_id)
            if old["revision"] != revision:
                raise Conflict("note changed concurrently; reload before editing")
            self.db.execute(
                "INSERT INTO note_history VALUES(?,?,?,?)",
                (anchor_id, revision, old["note"], int(time.time() * 1000)),
            )
            self.db.execute(
                "UPDATE anchors SET note=?,revision=revision+1 WHERE id=?",
                (note, anchor_id),
            )
            self._index(anchor_id)
        return self.anchor(anchor_id)

    def derive(self, anchor_id, field, text, model, frame_path=None):
        if field not in ("asr_text", "caption"):
            raise ValueError("invalid derived field")
        with self.transaction():
            anchor = self.anchor(anchor_id)
            provenance = json.loads(anchor["derivation"])
            provenance[field] = {
                "model": model,
                "generated_ms": int(time.time() * 1000),
            }
            self.db.execute(
                f"UPDATE anchors SET {field}=?,derivation=?,frame_path=coalesce(?,frame_path) WHERE id=?",
                (text[:8000], canonical(provenance), frame_path, anchor_id),
            )
            self._index(anchor_id)

    def register_media(self, media: MediaIn):
        if media.end_us <= media.start_us:
            raise ValueError("media end must follow start")
        with self.transaction():
            session = self.session(media.session_id)
            if (media.device_id, media.boot_id) != (
                session["device_id"],
                session["boot_id"],
            ):
                raise Conflict("media is in a different source clock")
            if media.start_us < session["started_us"]:
                raise ValueError("media predates session")
            if (
                session["ended_us"] is not None
                and media.end_us > session["ended_us"] + 1_000_000
            ):
                raise ValueError("media exceeds closed session")
            old = self.rows("SELECT * FROM media WHERE id=?", (media.id,))
            if old:
                for key, value in media.model_dump().items():
                    if old[0][key] != value:
                        raise Conflict(
                            "media id cannot identify different bytes or time ranges"
                        )
                return old[0]
            item = media.model_dump()
            item["discontinuity"] = int(item["discontinuity"])
            try:
                self.db.execute(
                    """INSERT INTO media(id,session_id,device_id,boot_id,start_us,end_us,kind,
                   size_bytes,sha256,mime,discontinuity) VALUES(:id,:session_id,:device_id,:boot_id,:start_us,
                   :end_us,:kind,:size_bytes,:sha256,:mime,:discontinuity)""",
                    item,
                )
            except sqlite3.IntegrityError as e:
                raise Conflict("duplicate media time range") from e
        return self.media(media.id)

    def derive_speech(
        self,
        parent_id,
        start_sample,
        end_sample,
        text,
        model,
        index,
        media_start_us=None,
        context_ids=None,
    ):
        parent = self.anchor(parent_id)
        anchor_id = parent_id + "-speech-" + str(index)
        stamp = (
            parent["source_time_us"] if media_start_us is None else media_start_us
        ) + start_sample * 1_000_000 // 16000
        provenance = canonical(
            {
                "asr_text": {
                    "model": model,
                    "generated_ms": int(time.time() * 1000),
                    "source_event_id": parent_id,
                    "start_sample": start_sample,
                    "end_sample": end_sample,
                    "audio_source_begin_us": media_start_us,
                    "context_media_ids": context_ids or [],
                }
            }
        )
        with self.transaction():
            self.db.execute(
                """INSERT INTO anchors(id,session_id,source_time_us,kind,note,asr_text,derivation,parent_event_id)
                VALUES(?,?,?,'voice','',?,?,?) ON CONFLICT(id) DO UPDATE SET asr_text=excluded.asr_text,derivation=excluded.derivation""",
                (anchor_id, parent["session_id"], stamp, text, provenance, parent_id),
            )
            self._index(anchor_id)
        return anchor_id

    def media(self, media_id):
        return self.one("SELECT * FROM media WHERE id=?", (media_id,))

    def managed_path(self, relative):
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root) or path == self.root:
            raise ValueError("path escapes data root")
        return path

    def attach_media(self, media_id, relative_path):
        item = self.media(media_id)
        path = self.managed_path(relative_path)
        if not path.is_file() or path.stat().st_size != item["size_bytes"]:
            raise ValueError("media size mismatch")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != item["sha256"]:
            raise ValueError("media checksum mismatch")
        with self.transaction():
            self.db.execute(
                "UPDATE media SET path=?,status='ready',accessed_ms=? WHERE id=?",
                (relative_path, int(time.time() * 1000), media_id),
            )
        return self.media(media_id)

    def verify_media(self, item):
        if not item["path"] or item["status"] == "unreadable":
            return False
        path = self.managed_path(item["path"])
        valid = path.is_file() and path.stat().st_size == item["size_bytes"]
        if valid:
            stat = path.stat()
            fingerprint = (
                str(path),
                stat.st_mtime_ns,
                stat.st_ctime_ns,
                stat.st_size,
                item["sha256"],
            )
            with self.lock:
                cached = self._verified.get(item["id"])
            if cached != fingerprint:
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(65536), b""):
                        digest.update(chunk)
                valid = digest.hexdigest() == item["sha256"]
                if valid:
                    with self.lock:
                        self._verified[item["id"]] = fingerprint
        if not valid:
            with self.lock:
                self.db.execute(
                    "UPDATE media SET status='missing' WHERE id=?", (item["id"],)
                )
        return valid

    def evidence(self, anchor_id):
        anchor = self.anchor(anchor_id)
        begin = max(anchor["started_us"], anchor["source_time_us"] - 10_000_000)
        end = anchor["source_time_us"] + 10_000_000
        items = self.rows(
            """SELECT * FROM media WHERE session_id=? AND boot_id=? AND start_us<? AND end_us>?
                             ORDER BY start_us,kind""",
            (anchor["session_id"], anchor["boot_id"], end, begin),
        )
        evidence = []
        for item in items:
            available = self.verify_media(item)
            evidence.append(
                {
                    **item,
                    "available": available,
                    "offset_us": max(0, anchor["source_time_us"] - item["start_us"]),
                    "url": f"/api/media/{item['id']}/content" if available else None,
                }
            )
        return {
            "anchor": anchor,
            "range_start_us": begin,
            "range_end_us": end,
            "media": evidence,
            "status": (
                "found"
                if any(i["available"] for i in evidence)
                else ("remote" if items else "unavailable")
            ),
        }

    def search(self, query, project_id=None, session_id=None, top_k=5, semantic=None):
        clauses, args = ["a.hidden=0"], []
        if project_id:
            clauses.append("s.project_id=?")
            args.append(project_id)
        if session_id:
            clauses.append("a.session_id=?")
            args.append(session_id)
        where = " AND " + " AND ".join(clauses) if clauses else ""
        match = fts_query(query)
        lexical = (
            self.rows(
                """SELECT a.id,bm25(anchor_fts) rank FROM anchor_fts
                   JOIN anchors a ON a.id=anchor_fts.anchor_id JOIN sessions s ON s.id=a.session_id
                   WHERE anchor_fts MATCH ?"""
                + where
                + " ORDER BY rank LIMIT 40",
                [match] + args,
            )
            if match
            else []
        )
        ranks = {}
        for i, row in enumerate(lexical):
            ranks[row["id"]] = 1.0 / (60 + i + 1)
        semantic_ids = set()
        for i, (anchor_id, score) in enumerate(semantic or []):
            if score >= 0.60:
                ranks[anchor_id] = ranks.get(anchor_id, 0.0) + 1.0 / (60 + i + 1)
                semantic_ids.add(anchor_id)
        results = []
        progress = any(
            word in query for word in ("做到哪", "进度", "接着做", "继续上次")
        )
        latest = progress or any(
            word in query for word in ("最后", "放哪", "哪里", "最近")
        )
        for anchor_id, rank in ranks.items():
            anchor = self.anchor(anchor_id)
            if (
                anchor["hidden"]
                or project_id
                and anchor["project_id"] != project_id
                or session_id
                and anchor["session_id"] != session_id
            ):
                continue
            text = " ".join(anchor[k] for k in ("note", "asr_text", "caption"))
            ratio = overlap(query, text)
            if ratio < 0.12 and anchor_id not in semantic_ids:
                continue
            results.append(
                {
                    "anchor": anchor,
                    "rank": rank,
                    "lexical_overlap": ratio,
                    "evidence": self.evidence(anchor_id),
                }
            )
        # A progress query can use only this project's latest explicit observations.
        if progress and project_id:
            recent = self.rows(
                "SELECT a.id FROM anchors a JOIN sessions s ON s.id=a.session_id WHERE a.hidden=0 AND s.project_id=?"
                + (" AND s.id=?" if session_id else "")
                + " ORDER BY s.created_ms DESC,a.source_time_us DESC LIMIT 5",
                [project_id] + ([session_id] if session_id else []),
            )
            for row in recent:
                if row["id"] not in {x["anchor"]["id"] for x in results}:
                    results.append(
                        {
                            "anchor": self.anchor(row["id"]),
                            "rank": 0.0,
                            "lexical_overlap": 0,
                            "evidence": self.evidence(row["id"]),
                        }
                    )
            results.sort(
                key=lambda r: (
                    self.session(r["anchor"]["session_id"])["created_ms"],
                    r["anchor"]["source_time_us"],
                ),
                reverse=True,
            )
        elif latest:
            results.sort(
                key=lambda r: (
                    self.session(r["anchor"]["session_id"])["created_ms"],
                    r["anchor"]["source_time_us"],
                ),
                reverse=True,
            )
        else:
            results.sort(key=lambda r: (-r["rank"], -r["anchor"]["source_time_us"]))
            if any(word in query for word in ("为什么", "原因", "为何")):
                results.sort(
                    key=lambda r: not any(
                        word in (r["anchor"]["note"] + r["anchor"]["asr_text"])
                        for word in ("因为", "原因", "由于", "所以", "为了")
                    )
                )
        return results[:top_k]

    def enqueue_job(self, kind, anchor_id, priority=10, max_pending=32):
        with self.transaction():
            if (
                priority > 1
                and self.db.execute(
                    "SELECT count(*) FROM jobs WHERE status='pending'"
                ).fetchone()[0]
                >= max_pending
            ):
                return None
            old = self.rows(
                "SELECT id FROM jobs WHERE kind=? AND anchor_id=?", (kind, anchor_id)
            )
            if old:
                return old[0]["id"]
            job = identifier()
            now = int(time.time() * 1000)
            self.db.execute(
                "INSERT INTO jobs(id,kind,anchor_id,priority,status,available_ms,created_ms) VALUES(?,?,?,?,'pending',?,?)",
                (job, kind, anchor_id, priority, now, now),
            )
            return job

    def replenish_caption_jobs(self, max_pending=32, batch=4):
        """Recover low-priority anchors skipped while the AI queue was full."""
        with self.transaction():
            pending = self.db.execute(
                "SELECT count(*) FROM jobs WHERE status='pending'"
            ).fetchone()[0]
            room = max(0, min(batch, max_pending - pending))
            if room == 0:
                return []
            candidates = self.rows(
                """SELECT a.id,a.kind FROM anchors a
                   JOIN sessions s ON s.id=a.session_id
                   WHERE a.kind IN ('mark','pedal','scene')
                     AND a.hidden=0 AND a.caption=''
                     AND NOT EXISTS (
                       SELECT 1 FROM jobs j WHERE j.kind='caption' AND j.anchor_id=a.id
                     )
                     AND EXISTS (
                       SELECT 1 FROM media m WHERE m.session_id=a.session_id
                         AND m.kind='video' AND m.start_us<=a.source_time_us
                         AND a.source_time_us<m.end_us
                     )
                   ORDER BY CASE WHEN a.kind IN ('mark','pedal') THEN 0 ELSE 1 END,
                            s.created_ms DESC,a.source_time_us ASC LIMIT ?""",
                (room,),
            )
            now = int(time.time() * 1000)
            for anchor in candidates:
                self.db.execute(
                    "INSERT INTO jobs(id,kind,anchor_id,priority,status,available_ms,created_ms) "
                    "VALUES(?, 'caption', ?, ?, 'pending', ?, ?)",
                    (identifier(), anchor["id"],
                     1 if anchor["kind"] in ("mark", "pedal") else 10, now, now),
                )
            return [item["id"] for item in candidates]

    def claim_job(self, kinds=None):
        with self.transaction():
            kind_filter = (
                " AND kind IN (" + ",".join("?" for _ in kinds) + ")" if kinds else ""
            )
            jobs = self.rows(
                "SELECT * FROM jobs WHERE status='pending' AND available_ms<=?"
                + kind_filter
                + " ORDER BY priority,created_ms LIMIT 1",
                [int(time.time() * 1000)] + (list(kinds) if kinds else []),
            )
            if not jobs:
                return None
            self.db.execute(
                "UPDATE jobs SET status='running',attempts=attempts+1 WHERE id=?",
                (jobs[0]["id"],),
            )
            return jobs[0]

    def finish_job(self, job_id, status, detail="", retry_seconds=0):
        with self.transaction():
            self.db.execute(
                "UPDATE jobs SET status=?,detail=?,available_ms=? WHERE id=?",
                (
                    status,
                    detail[:1000],
                    int((time.time() + retry_seconds) * 1000),
                    job_id,
                ),
            )

    def delete_session(self, session_id):
        with self.transaction():
            session = self.session(session_id)
            if session["state"] != "stopped":
                raise Conflict("stop recording before deleting a session")
            media = self.rows(
                "SELECT path FROM media WHERE session_id=? AND path IS NOT NULL",
                (session_id,),
            )
            frames = self.rows(
                "SELECT frame_path path FROM anchors WHERE session_id=? AND frame_path IS NOT NULL",
                (session_id,),
            )
            self.db.execute(
                "DELETE FROM anchor_fts WHERE anchor_id IN (SELECT id FROM anchors WHERE session_id=?)",
                (session_id,),
            )
            self.db.execute(
                "INSERT OR IGNORE INTO tombstones(session_id,device_id,boot_id,created_ms) VALUES(?,?,?,?)",
                (
                    session_id,
                    session["device_id"],
                    session["boot_id"],
                    int(time.time() * 1000),
                ),
            )
            self.db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        for item in media + frames:
            path = self.managed_path(item["path"])
            with contextlib.suppress(FileNotFoundError):
                path.unlink()

    def trim_cache(self, limit, protected_id=None):
        cached = self.rows(
            "SELECT * FROM media WHERE path IS NOT NULL ORDER BY accessed_ms,start_us"
        )
        total = sum(m["size_bytes"] for m in cached)
        for item in cached:
            if total <= limit:
                break
            if item["authoritative"] or item["id"] == protected_id:
                continue
            self.managed_path(item["path"]).unlink(missing_ok=True)
            with self.transaction():
                self.db.execute(
                    "UPDATE media SET path=NULL,status='remote' WHERE id=?",
                    (item["id"],),
                )
            total -= item["size_bytes"]
        return total

    def stats(self):
        return {
            name: self.rows(f"SELECT count(*) n FROM {name}")[0]["n"]
            for name in ("projects", "sessions", "anchors", "media", "jobs")
        }
