"""Deterministic calendar. Half-open UTC intervals; every write rechecks in one transaction."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CalendarError(ValueError):
    def __init__(self, code: str, **details):
        super().__init__(code)
        self.code, self.details = code, details


class EventInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(min_length=1, max_length=200)
    start: str
    end: str
    location: str = Field(default="", max_length=500)
    notes: str = Field(default="", max_length=2000)

    @field_validator("title")
    @classmethod
    def title_present(cls, value):
        if not value.strip():
            raise ValueError("标题不能为空")
        return value.strip()


class Calendar:
    def __init__(self, path: str | Path, timezone: str = "Asia/Shanghai"):
        self.path, self.tz = Path(path), ZoneInfo(timezone)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, start INTEGER NOT NULL,
                    end INTEGER NOT NULL CHECK(end > start), location TEXT NOT NULL,
                    notes TEXT NOT NULL, version INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS events_time ON events(start, end);
            """)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def timestamp(self, value: str) -> int:
        if not isinstance(value, str) or "T" not in value or len(value) < 16:
            raise CalendarError(
                "invalid_datetime", hint="使用 ISO 日期与时间，例如 2026-10-18T15:00:00+08:00"
            )
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise CalendarError("invalid_datetime") from exc
        if dt.tzinfo is None:
            # Reject gaps and folds; explicit offset is required to disambiguate DST.
            a, b = dt.replace(tzinfo=self.tz, fold=0), dt.replace(tzinfo=self.tz, fold=1)
            if (
                a.utcoffset() != b.utcoffset()
                or a.astimezone(UTC).astimezone(self.tz).replace(tzinfo=None) != dt
            ):
                raise CalendarError("ambiguous_or_nonexistent_time", hint="请给出明确 UTC 偏移量")
            dt = a
        if not 1970 <= dt.year <= 2100:
            raise CalendarError("date_out_of_range")
        return int(dt.timestamp())

    def iso(self, ts: int) -> str:
        return datetime.fromtimestamp(ts, self.tz).isoformat()

    def serialize(self, row) -> dict | None:
        if row is None:
            return None
        result = dict(row)
        result["start"], result["end"] = self.iso(row["start"]), self.iso(row["end"])
        return result

    def validate(self, payload: dict) -> dict:
        data = EventInput.model_validate(payload).model_dump()
        start, end = self.timestamp(data["start"]), self.timestamp(data["end"])
        if end <= start or end - start > 7 * 86400:
            raise CalendarError("invalid_duration", hint="结束须晚于开始，最长7天")
        return {**data, "start": start, "end": end}

    def get(self, event_id: str, conn=None):
        if conn is not None:
            return self.serialize(
                conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
            )
        with self.connect() as db:
            return self.get(event_id, db)

    def search(self, query="", start=None, end=None, limit=100):
        clauses, params = [], []
        if start:
            clauses.append("end > ?")
            params.append(self.timestamp(start))
        if end:
            clauses.append("start < ?")
            params.append(self.timestamp(end))
        if start and end and self.timestamp(end) <= self.timestamp(start):
            raise CalendarError("invalid_range")
        sql = (
            "SELECT * FROM events"
            + (" WHERE " + " AND ".join(clauses) if clauses else "")
            + " ORDER BY start, id"
        )
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        # A bounded local prototype; fuzzy scores are retrieval hints, never authorization.
        query = query.strip().casefold()
        matches = []
        for row in rows:
            text = " ".join(row[k] for k in ["title", "location", "notes"]).casefold()
            title = row["title"].casefold()
            # Short Chinese references are often only part of the title. Compare
            # equally sized windows; low recall scores only widen candidates.
            fuzzy = len(query) >= 2 and any(
                SequenceMatcher(None, query, title[i : i + len(query)]).ratio()
                >= (0.5 if len(query) == 2 else 0.6)
                for i in range(max(1, len(title) - len(query) + 1))
            )
            if not query or query in text or fuzzy:
                matches.append(self.serialize(row))
        return matches[:limit]

    def conflicts(self, start, end, exclude_id=None, conn=None):
        start_ts, end_ts = self.timestamp(start), self.timestamp(end)
        if end_ts <= start_ts:
            raise CalendarError("invalid_range")
        if conn is None:
            with self.connect() as db:
                return self.conflicts(start, end, exclude_id, db)
        rows = conn.execute(
            "SELECT * FROM events WHERE start < ? AND end > ? AND id != ? ORDER BY start",
            (end_ts, start_ts, exclude_id or ""),
        ).fetchall()
        return [self.serialize(r) for r in rows]

    def _write(self, action, payload, event_id=None, version=None, conn=None):
        if conn is None:
            with self.transaction() as db:
                return self._write(action, payload, event_id, version, db)
        old = self.get(event_id, conn) if event_id else None
        if action != "create" and (old is None or old["version"] != version):
            raise CalendarError("stale", hint="日程已变化，请重新查询并确认")
        if action == "delete":
            conn.execute("DELETE FROM events WHERE id=? AND version=?", (event_id, version))
            if self.get(event_id, conn) is not None:
                raise CalendarError("verification_failed")
            return old
        merged = {k: old[k] for k in ["title", "start", "end", "location", "notes"]} if old else {}
        merged.update(payload)
        data = self.validate(merged)
        conflicts = self.conflicts(self.iso(data["start"]), self.iso(data["end"]), event_id, conn)
        if conflicts:
            raise CalendarError("conflict", events=conflicts)
        if action == "create":
            event_id = uuid4().hex
            conn.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, 1)",
                (
                    event_id,
                    data["title"],
                    data["start"],
                    data["end"],
                    data["location"],
                    data["notes"],
                ),
            )
        else:
            conn.execute(
                "UPDATE events SET title=?, start=?, end=?, location=?, notes=?, version=version+1 WHERE id=? AND version=?",
                (
                    data["title"],
                    data["start"],
                    data["end"],
                    data["location"],
                    data["notes"],
                    event_id,
                    version,
                ),
            )
        verified = self.get(event_id, conn)
        expected = {**data, "start": self.iso(data["start"]), "end": self.iso(data["end"])}
        if not verified or any(verified[k] != v for k, v in expected.items()):
            raise CalendarError("verification_failed")
        return verified

    def create(self, payload, conn=None):
        return self._write("create", payload, conn=conn)

    def update(self, event_id, version, patch, conn=None):
        return self._write("update", patch, event_id, version, conn)

    def delete(self, event_id, version, conn=None):
        return self._write("delete", {}, event_id, version, conn)

    def slots(self, start, end, duration_minutes, exclude_id=None):
        begin, stop = self.timestamp(start), self.timestamp(end)
        if not 5 <= duration_minutes <= 1440 or stop <= begin or stop - begin > 31 * 86400:
            raise CalendarError("invalid_slot_range")
        found = []
        # Advance in real UTC minutes, preserving correctness at DST boundaries.
        current = begin
        while current + duration_minutes * 60 <= stop and len(found) < 5:
            finish = current + duration_minutes * 60
            if not self.conflicts(self.iso(current), self.iso(finish), exclude_id):
                found.append({"start": self.iso(current), "end": self.iso(finish)})
            current += 15 * 60
        return found
