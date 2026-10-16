"""Persistent conversations and server-issued, session-bound, one-use write proposals."""

import json
import secrets
import time
from uuid import uuid4

from scheduler.calendar import Calendar, CalendarError


class Store:
    def __init__(self, path, timezone):
        self.calendar = Calendar(path, timezone)
        with self.calendar.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, turns TEXT NOT NULL DEFAULT '[]',
                    state TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS proposals (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    action TEXT NOT NULL, payload TEXT NOT NULL, before_state TEXT,
                    event_id TEXT, version INTEGER, expires REAL NOT NULL,
                    status TEXT NOT NULL, result TEXT);
                CREATE INDEX IF NOT EXISTS proposal_session ON proposals(session_id,status);
                CREATE TABLE IF NOT EXISTS requests (
                    session_id TEXT NOT NULL, id TEXT NOT NULL, result TEXT,
                    PRIMARY KEY(session_id,id));
                PRAGMA user_version=1;
            """)

    def new_session(self):
        sid = secrets.token_urlsafe(32)
        with self.calendar.connect() as conn:
            conn.execute("INSERT INTO sessions(id) VALUES (?)", (sid,))
        return sid

    def session(self, sid):
        with self.calendar.connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if not row:
            raise CalendarError("session_not_found")
        return {"turns": json.loads(row["turns"]), "state": json.loads(row["state"])}

    def save_state(self, sid, state):
        with self.calendar.connect() as conn:
            conn.execute(
                "UPDATE sessions SET state=? WHERE id=?",
                (json.dumps(state, ensure_ascii=False), sid),
            )

    def append_turn(self, sid, messages):
        with self.calendar.transaction() as conn:
            row = conn.execute("SELECT turns FROM sessions WHERE id=?", (sid,)).fetchone()
            turns = (json.loads(row["turns"]) + [messages])[-12:]
            conn.execute(
                "UPDATE sessions SET turns=? WHERE id=?",
                (json.dumps(turns, ensure_ascii=False), sid),
            )

    def select(self, sid, event_id):
        state = self.session(sid)["state"]
        if event_id not in {x["id"] for x in state.get("candidates", [])}:
            raise CalendarError("not_observed")
        state["selected_id"] = event_id
        self.save_state(sid, state)

    def invalidate(self, sid):
        with self.calendar.connect() as conn:
            conn.execute(
                "UPDATE proposals SET status='superseded' WHERE session_id=? AND status='pending'",
                (sid,),
            )

    def propose(self, sid, action, payload, before=None):
        self.invalidate(sid)
        proposal_id = uuid4().hex
        expires = time.time() + 15 * 60
        with self.calendar.connect() as conn:
            conn.execute(
                "INSERT INTO proposals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (
                    proposal_id,
                    sid,
                    action,
                    json.dumps(payload, ensure_ascii=False),
                    json.dumps(before, ensure_ascii=False) if before else None,
                    before["id"] if before else None,
                    before["version"] if before else None,
                    expires,
                    "pending",
                ),
            )
        return self.pending(sid)

    def pending(self, sid):
        with self.calendar.connect() as conn:
            row = conn.execute(
                "SELECT * FROM proposals WHERE session_id=? AND status='pending' AND expires>? ORDER BY rowid DESC LIMIT 1",
                (sid, time.time()),
            ).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "action": row["action"],
            "event": json.loads(row["payload"]),
            "before": json.loads(row["before_state"]) if row["before_state"] else None,
            "expires": row["expires"],
        }

    def confirm(self, sid, proposal_id, approve):
        # Transaction covers proposal status + conflict check + write + readback.
        # Duplicate approvals return the same receipt, even after process restart.
        with self.calendar.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM proposals WHERE id=? AND session_id=?", (proposal_id, sid)
            ).fetchone()
            if not row:
                raise CalendarError("proposal_not_found")
            if row["result"]:
                return json.loads(row["result"])
            if row["status"] != "pending" or row["expires"] <= time.time():
                raise CalendarError(
                    "proposal_expired", hint="提案已过期或被新消息替代，请重新提出请求"
                )
            if not approve:
                result = {"status": "cancelled", "message": "已取消这次操作，日程未改变。"}
            else:
                conn.execute("SAVEPOINT calendar_write")
                try:
                    payload = json.loads(row["payload"])
                    if row["action"] == "create":
                        event = self.calendar.create(payload, conn=conn)
                    elif row["action"] == "update":
                        event = self.calendar.update(
                            row["event_id"], row["version"], payload, conn=conn
                        )
                    else:
                        event = self.calendar.delete(row["event_id"], row["version"], conn=conn)
                    verb = {"create": "添加", "update": "修改", "delete": "删除"}[row["action"]]
                    result = {
                        "status": "committed",
                        "action": row["action"],
                        "event": event,
                        "message": f"已{verb}并核验日程：{describe(event)}。",
                    }
                    conn.execute("RELEASE calendar_write")
                except CalendarError as exc:
                    # Keep a durable failed receipt, but never commit a mutation whose
                    # verification failed. Catching inside the outer transaction alone
                    # would otherwise commit the INSERT/UPDATE/DELETE along with it.
                    conn.execute("ROLLBACK TO calendar_write")
                    conn.execute("RELEASE calendar_write")
                    result = {
                        "status": "error",
                        "error": exc.code,
                        "details": exc.details,
                        "message": (
                            "状态核验失败，本次操作已回滚，请重新查询。"
                            if exc.code == "verification_failed"
                            else "操作未执行：日程已变化或出现冲突，请重新查询。"
                        ),
                    }
            conn.execute(
                "UPDATE proposals SET status=?, result=? WHERE id=?",
                (result["status"], json.dumps(result, ensure_ascii=False), proposal_id),
            )
            conn.execute("UPDATE sessions SET state=? WHERE id=?", ("{}", sid))
            # Persist the factual receipt in the same transaction as the mutation.
            turns = json.loads(
                conn.execute("SELECT turns FROM sessions WHERE id=?", (sid,)).fetchone()["turns"]
            )
            turns.append(
                [
                    {"role": "user", "content": "确认上述提案" if approve else "取消上述提案"},
                    {"role": "assistant", "content": result["message"]},
                ]
            )
            conn.execute(
                "UPDATE sessions SET turns=? WHERE id=?",
                (json.dumps(turns[-12:], ensure_ascii=False), sid),
            )
            return result

    def claim_request(self, sid, request_id):
        with self.calendar.transaction() as conn:
            row = conn.execute(
                "SELECT result FROM requests WHERE session_id=? AND id=?", (sid, request_id)
            ).fetchone()
            if row:
                return (
                    json.loads(row["result"])
                    if row["result"]
                    else {
                        "status": "busy",
                        "message": "该请求正在处理或曾被中断，请刷新状态后使用新的请求。",
                    }
                )
            conn.execute("INSERT INTO requests VALUES (?, ?, NULL)", (sid, request_id))
        return None

    def finish_request(self, sid, request_id, result):
        with self.calendar.connect() as conn:
            conn.execute(
                "UPDATE requests SET result=? WHERE session_id=? AND id=?",
                (json.dumps(result, ensure_ascii=False), sid, request_id),
            )


def describe(event):
    start = event["start"].replace("T", " ")[:16]
    end = event["end"].replace("T", " ")[:16]
    location = f"，地点：{event['location']}" if event["location"] else ""
    return f"{event['title']}（{start} 至 {end}{location}）"


def describe_slots(slots):
    if not slots:
        return "在指定范围内没有找到可用时段，请扩大范围或缩短时长。"
    return "以下时段没有冲突：\n" + "\n".join(
        s["start"].replace("T", " ")[:16] + " 至 " + s["end"].replace("T", " ")[:16] for s in slots
    )
