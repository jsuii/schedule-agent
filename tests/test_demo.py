"""Offline integration: scripted MODEL, real agent / tools / HTTP / SQLite."""

from fastapi.testclient import TestClient

from scheduler.app import create_app
from scheduler.config import Settings
from scheduler.demo import DEMO_PROMPTS, ReplayModel


def test_offline_guided_scenario_crud_clarification_conflict_ambiguity(tmp_path):
    app = create_app(Settings(database=str(tmp_path / "demo.sqlite3")), ReplayModel(), demo=True)
    client = TestClient(app)

    def chat(i):
        return client.post(
            "/api/chat", json={"text": DEMO_PROMPTS[i], "request_id": f"step-{i}"}
        ).json()

    def approve(result):
        return client.post(
            "/api/proposals/" + result["proposal"]["id"], json={"approve": True}
        ).json()

    assert chat(0)["status"] == "clarification"
    assert approve(chat(1))["status"] == "committed"
    query = chat(2)
    assert query["status"] == "query"
    assert len(query["events"]) == 1
    assert approve(chat(3))["event"]["location"] == "会议室B"
    conflict = chat(4)
    assert conflict["status"] == "clarification"
    assert conflict["trace"][0]["result"]["error"] == "conflict"
    assert approve(chat(5))["status"] == "committed"
    ambiguous = chat(6)
    assert len(ambiguous["candidates"]) == 2
    intended = ambiguous["candidates"][0]["id"]
    assert client.post("/api/select", json={"event_id": intended}).status_code == 200
    result = client.post(
        "/api/chat", json={"text": "请继续处理我选择的这条日程", "request_id": "choice"}
    ).json()
    assert approve(result)["action"] == "delete"
    remaining = client.get("/api/state").json()["events"]
    assert len(remaining) == 1 and remaining[0]["id"] != intended


def test_offline_mode_does_not_pretend_to_parse_arbitrary_input(tmp_path):
    client = TestClient(
        create_app(Settings(database=str(tmp_path / "demo.sqlite3")), ReplayModel(), demo=True)
    )
    result = client.post(
        "/api/chat", json={"text": "随便安排我的事情", "request_id": "other"}
    ).json()
    assert "固定脚本" in result["message"]
    assert client.get("/api/state").json()["demo"] is True
