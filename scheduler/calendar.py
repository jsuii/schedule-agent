"""Deterministic calendar. Half-open UTC intervals; every write rechecks in one transaction."""

from __future__ import annotations

import sqlite3
import json
from typing import Literal
from contextlib import contextmanager
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator


CORE = {"title", "start", "end", "location", "notes"}


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

    importance: Literal["unknown", "high", "medium", "low"] = "unknown"
    flexibility: Literal["unknown", "fixed", "movable", "shortenable", "splittable"] = "unknown"
    interruptibility: Literal[
        "unknown", "noninterruptible", "pausable", "resumable", "parallel"
    ] = "unknown"
    keep_time: bool = False
    minimum_minutes: int = Field(default=1, ge=1, le=10080)
    overlap_allowed: bool = False
    coexistence_verified: bool = False

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

            columns = {r["name"] for r in conn.execute("PRAGMA table_info(events)")}
            if "attributes" not in columns:
                conn.execute("ALTER TABLE events ADD COLUMN attributes TEXT NOT NULL DEFAULT '{}'")

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
        attributes = json.loads(result.pop("attributes", "{}"))
        defaults = EventInput(title=result["title"], start="", end="").model_dump()
        result.update({k: attributes.get(k, v) for k, v in defaults.items() if k not in CORE})
        result["start"], result["end"] = self.iso(row["start"]), self.iso(row["end"])
        return result

    def validate(self, payload: dict) -> dict:
        data = EventInput.model_validate(payload).model_dump()
        start, end = self.timestamp(data["start"]), self.timestamp(data["end"])
        if end <= start or end - start > 7 * 86400 or end - start < data["minimum_minutes"] * 60:
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
        merged = {k: old[k] for k in EventInput.model_fields} if old else {}
        merged.update(payload)
        data = self.validate(merged)
        conflicts = self.conflicts(self.iso(data["start"]), self.iso(data["end"]), event_id, conn)
        candidate = {**data, "start": self.iso(data["start"]), "end": self.iso(data["end"])}
        conflicts = [e for e in conflicts if not self.compatible(candidate, e)]
        if old:
            self.check_constraints(old, candidate)
        if conflicts:
            raise CalendarError("conflict", events=conflicts)
        if action == "create":
            event_id = uuid4().hex
            conn.execute(
                "INSERT INTO events(id,title,start,end,location,notes,version,attributes) VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
                (
                    event_id,
                    data["title"],
                    data["start"],
                    data["end"],
                    data["location"],
                    data["notes"],
                    json.dumps({k: v for k, v in data.items() if k not in CORE}),
                ),
            )
        else:
            conn.execute(
                "UPDATE events SET title=?, start=?, end=?, location=?, notes=?, attributes=?, version=version+1 WHERE id=? AND version=?",
                (
                    data["title"],
                    data["start"],
                    data["end"],
                    data["location"],
                    data["notes"],
                    json.dumps({k: v for k, v in data.items() if k not in CORE}),
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

    def compatible(self, a, b):
        """Explicit same-location, attention/travel consent; titles never authorize overlap."""
        if not all(e.get("overlap_allowed") and e.get("coexistence_verified") for e in (a, b)):
            return False
        if not a.get("location") or a["location"] != b.get("location"):
            return False
        if any(e.get("interruptibility") == "parallel" for e in (a, b)):
            return True
        for outer, inner in ((a, b), (b, a)):
            if (
                outer.get("interruptibility") in ("pausable", "resumable")
                and self.timestamp(outer["start"]) <= self.timestamp(inner["start"])
                and self.timestamp(outer["end"]) >= self.timestamp(inner["end"])
            ):
                return True
        return False

    def check_constraints(self, before, after):
        changed = any(
            self.timestamp(before[k]) != self.timestamp(after[k]) for k in ("start", "end")
        )
        if changed and (before.get("keep_time") or before.get("flexibility") == "fixed"):
            raise CalendarError(
                "hard_constraint", hint="目标有固定时间/必须保留约束，请先明确解除约束"
            )
        if changed and before.get("flexibility") == "shortenable":
            if self.timestamp(after["start"]) != self.timestamp(before["start"]) or self.timestamp(
                after["end"]
            ) > self.timestamp(before["end"]):
                raise CalendarError("hard_constraint", hint="此事件仅允许缩短")
        if (
            self.timestamp(after["end"]) - self.timestamp(after["start"])
            < before.get("minimum_minutes", 1) * 60
        ):
            raise CalendarError("hard_constraint", hint="低于原事件最短时长")

    def prepare_batch(self, changes, conn=None):
        if conn is None:
            with self.transaction() as db:
                return self.prepare_batch(changes, db)
        prepared, ids = [], set()
        for change in changes:
            action = change["action"]
            before = None
            if action == "update":
                target = change["event_id"]
                if target in ids:
                    raise CalendarError("duplicate_target")
                ids.add(target)
                before = self.get(target, conn)
                if not before or before["version"] != change["version"]:
                    raise CalendarError("stale", hint="方案中的日程已变化，请重新分析")
                payload = {k: before[k] for k in EventInput.model_fields}
                payload.update(change["patch"])
            elif action == "create":
                payload = change["event"]
            else:
                raise CalendarError("invalid_action")
            data = self.validate(payload)
            after = {**data, "start": self.iso(data["start"]), "end": self.iso(data["end"])}
            if before:
                self.check_constraints(before, after)
            prepared.append({"action": action, "before": before, "after": after})
        existing = [
            self.serialize(r) for r in conn.execute("SELECT * FROM events") if r["id"] not in ids
        ]
        final = existing + [p["after"] for p in prepared]
        # Existing-existing overlaps need not be re-authorized; every touched pair is checked.
        for i in range(len(existing), len(final)):
            a = final[i]
            for b in final[:i]:
                if (
                    self.timestamp(a["start"]) < self.timestamp(b["end"])
                    and self.timestamp(a["end"]) > self.timestamp(b["start"])
                    and not self.compatible(a, b)
                ):
                    raise CalendarError("conflict", events=[b], hint="组合的最终状态仍有冲突")
        return prepared

    def apply_batch(self, changes, conn):
        prepared = self.prepare_batch(changes, conn)
        # Remove all changed originals before inserting final rows: swaps have no transient conflict.
        for item in prepared:
            if item["before"]:
                conn.execute(
                    "DELETE FROM events WHERE id=? AND version=?",
                    (item["before"]["id"], item["before"]["version"]),
                )
        expected = []
        for item in prepared:
            old, after = item["before"], item["after"]
            target = old["id"] if old else uuid4().hex
            version = old["version"] + 1 if old else 1
            data = self.validate(after)
            conn.execute(
                "INSERT INTO events(id,title,start,end,location,notes,version,attributes) VALUES(?,?,?,?,?,?,?,?)",
                (
                    target,
                    data["title"],
                    data["start"],
                    data["end"],
                    data["location"],
                    data["notes"],
                    version,
                    json.dumps({k: v for k, v in data.items() if k not in CORE}),
                ),
            )
            expected.append({**after, "id": target, "version": version})
        actual = [self.get(e["id"], conn) for e in expected]
        if actual != expected:
            raise CalendarError("verification_failed")
        return actual
