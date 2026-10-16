import concurrent.futures
import time

import pytest
from fastapi.testclient import TestClient

from scheduler.app import create_app
from scheduler.config import Settings
from tests.helpers import ScriptedModel, call
from tests.test_calendar import event


@pytest.fixture
def env(tmp_path):
    settings = Settings(database=str(tmp_path / "web.sqlite3"))
    app = create_app(settings, ScriptedModel(call("propose_create", **event())))
    return app, TestClient(app)


def send(client, text="明天下午3点和老王开会一小时", request_id="req-1"):
    return client.post("/api/chat", json={"text": text, "request_id": request_id})


def test_full_http_flow_confirmation_restart_and_cancel(env):
    app, client = env
    assert client.get("/").status_code == 200
    assert client.get("/api/state").json()["events"] == []
    response = send(client)
    assert response.status_code == 200
    proposal = response.json()["proposal"]
    assert client.get("/api/state").json()["events"] == []
    # Restart app using same DB and same cookie.
    second = TestClient(create_app(app.state.settings, ScriptedModel()))
    second.cookies.update(client.cookies)
    assert second.get("/api/state").json()["proposal"]["id"] == proposal["id"]
    result = second.post("/api/proposals/" + proposal["id"], json={"approve": True}).json()
    assert result["status"] == "committed"
    assert len(second.get("/api/state").json()["events"]) == 1
    assert second.post("/api/proposals/" + proposal["id"], json={"approve": True}).json() == result
    assert app.state.store.calendar.get(result["event"]["id"])


def test_duplicate_request_no_duplicate_proposal(env):
    app, client = env
    one = send(client).json()
    two = send(client).json()
    assert one == two
    assert len(app.state.model.messages) == 1
    result = client.post("/api/proposals/" + one["proposal"]["id"], json={"approve": False}).json()
    assert result["status"] == "cancelled"
    assert app.state.store.calendar.search() == []


def test_cross_session_and_cross_origin_rejected(env):
    app, client = env
    proposal = send(client).json()["proposal"]
    other = TestClient(app)
    denied = other.post("/api/proposals/" + proposal["id"], json={"approve": True})
    assert denied.status_code == 404
    assert (
        client.post(
            "/api/proposals/" + proposal["id"],
            json={"approve": True},
            headers={"Origin": "https://evil.example"},
        ).status_code
        == 403
    )
    assert client.get("/api/state", headers={"Host": "evil.example"}).status_code == 400
    assert app.state.store.calendar.search() == []


def test_stale_and_conflicting_proposals_do_not_write(env):
    app, client = env
    proposal = send(client).json()["proposal"]
    app.state.store.calendar.create(event())
    result = client.post("/api/proposals/" + proposal["id"], json={"approve": True}).json()
    assert result["status"] == "error"
    assert result["error"] == "conflict"
    assert len(app.state.store.calendar.search()) == 1


def test_expiry_new_turn_and_input_validation(env):
    app, client = env
    proposal = send(client).json()["proposal"]
    with app.state.store.calendar.connect() as conn:
        conn.execute("UPDATE proposals SET expires=?", (time.time() - 1,))
    assert (
        client.post("/api/proposals/" + proposal["id"], json={"approve": True}).status_code == 409
    )
    assert client.post("/api/chat", json={"text": "", "request_id": "x"}).status_code == 422
    assert client.post("/api/chat", json={"text": "x" * 4001, "request_id": "x"}).status_code == 422
    assert (
        client.post(
            "/api/chat", json={"text": "x", "request_id": "x", "session_id": "fake"}
        ).status_code
        == 422
    )
    assert (
        client.post("/api/proposals/" + proposal["id"], json={"approve": "true"}).status_code == 422
    )
    assert client.post("/api/select", json={"event_id": "invented"}).status_code == 409


def test_missing_configuration_does_not_fake_success(tmp_path):
    client = TestClient(create_app(Settings(database=str(tmp_path / "no-key.sqlite3"))))
    state = client.get("/api/state").json()
    assert state["model_configured"] is False
    result = send(client).json()
    assert result["status"] == "error"
    assert "配置" in result["message"]
    assert client.get("/api/state").json()["events"] == []


def test_concurrent_confirm_once(env):
    app, client = env
    proposal = send(client).json()["proposal"]
    sid = client.cookies["schedule_session"]

    def approve(_):
        return app.state.store.confirm(sid, proposal["id"], True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(approve, range(2)))
    assert results[0] == results[1]
    assert len(app.state.store.calendar.search()) == 1


def test_new_user_message_invalidates_previous_proposal(env):
    app, client = env
    proposal = send(client).json()["proposal"]
    app.state.model = ScriptedModel(call("ask_user", fields=["start", "duration"]))
    app.state.agent.model = app.state.model
    assert send(client, "改一下，我还没决定日期", "req-2").json()["status"] == "clarification"
    assert (
        client.post("/api/proposals/" + proposal["id"], json={"approve": True}).status_code == 409
    )
    assert app.state.store.calendar.search() == []
