"""
Tests for hermes_api routers.

Run with:  scripts/run_tests.sh tests/hermes_api/
"""

import json
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_db():
    db = MagicMock()
    db.session_count.return_value = 0
    db.get_session.return_value = None
    db.list_sessions_rich.return_value = []
    db.create_session.return_value = "test-session"
    db.delete_session.return_value = True
    db.get_messages.return_value = []
    db.message_count.return_value = 0
    db.search_messages.return_value = []
    db.get_compression_tip.return_value = None
    db.end_session.return_value = None
    db.set_session_title.return_value = True
    return db


@pytest.fixture
def app_with_mock_db(mock_db):
    """Build the FastAPI app with the DB dependency overridden."""
    from hermes_api.main import app
    from hermes_api.deps import get_db
    app.dependency_overrides[get_db] = lambda: mock_db
    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def client(app_with_mock_db):
    from fastapi.testclient import TestClient
    # Use lifespan=False to skip DB init in lifespan (we override the dep)
    with TestClient(app=app_with_mock_db, raise_server_exceptions=True) as c:
        yield c


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

def test_liveness(client):
    r = client.get("/v1/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_readiness(client, mock_db):
    mock_db.session_count.return_value = 5
    r = client.get("/v1/ready")
    assert r.status_code == 200
    assert r.json()["status"] == "ready"


def test_readiness_db_failure(client, mock_db):
    mock_db.session_count.side_effect = RuntimeError("DB down")
    r = client.get("/v1/ready")
    assert r.status_code == 503


def test_metrics(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "hermes_api_up" in r.text


# ---------------------------------------------------------------------------
# Conversations — create + list + get + delete
# ---------------------------------------------------------------------------

def test_create_conversation(client, mock_db):
    mock_db.get_session.return_value = {
        "session_id": "abc",
        "source": "api",
        "title": None,
        "created_at": None,
        "model": None,
    }
    r = client.post("/v1/conversations", json={"platform": "api"})
    assert r.status_code == 201
    data = r.json()
    assert "id" in data
    assert data["source"] == "api"


def test_list_conversations_empty(client, mock_db):
    r = client.get("/v1/conversations")
    assert r.status_code == 200
    assert r.json() == []


def test_get_conversation_not_found(client, mock_db):
    mock_db.get_session.return_value = None
    r = client.get("/v1/conversations/nonexistent")
    assert r.status_code == 404


def test_get_conversation_found(client, mock_db):
    mock_db.get_session.return_value = {
        "session_id": "sess1",
        "source": "api",
        "title": "Test",
        "created_at": None,
        "model": None,
    }
    mock_db.get_messages.return_value = [{"role": "user", "content": "hi"}]
    r = client.get("/v1/conversations/sess1")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "sess1"
    assert len(body["messages"]) == 1


def test_delete_conversation(client, mock_db):
    mock_db.delete_session.return_value = True
    r = client.delete("/v1/conversations/sess1")
    assert r.status_code == 204


def test_delete_conversation_not_found(client, mock_db):
    mock_db.delete_session.return_value = False
    r = client.delete("/v1/conversations/missing")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Send message (non-streaming)
# ---------------------------------------------------------------------------

def test_send_message_session_not_found(client, mock_db):
    mock_db.get_session.return_value = None
    r = client.post(
        "/v1/conversations/missing/messages",
        json={"content": "hello", "stream": False},
    )
    assert r.status_code == 404


def test_send_message_non_streaming(client, mock_db):
    mock_db.get_session.return_value = {
        "session_id": "sess1",
        "source": "api",
        "title": None,
        "created_at": None,
        "model": None,
    }

    mock_agent = MagicMock()
    mock_agent.run_conversation.return_value = {
        "final_response": "Hello back!",
        "messages": [],
    }

    # patch at the router's import location, not deps
    with patch("hermes_api.routers.conversations.get_agent", return_value=mock_agent):
        r = client.post(
            "/v1/conversations/sess1/messages",
            json={"content": "hello", "stream": False},
        )

    assert r.status_code == 200
    assert r.json()["content"] == "Hello back!"
    assert r.json()["session_id"] == "sess1"


# ---------------------------------------------------------------------------
# Sessions search
# ---------------------------------------------------------------------------

def test_sessions_search(client, mock_db):
    mock_db.search_messages.return_value = [
        {"session_id": "s1", "content": "result", "role": "assistant"}
    ]
    r = client.get("/v1/sessions/search?q=hello")
    assert r.status_code == 200
    body = r.json()
    assert body["query"] == "hello"
    assert body["count"] == 1


def test_sessions_search_missing_q(client):
    r = client.get("/v1/sessions/search")
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Sessions summary
# ---------------------------------------------------------------------------

def test_session_summary_not_found(client, mock_db):
    mock_db.get_session.return_value = None
    r = client.get("/v1/sessions/missing/summary")
    assert r.status_code == 404


def test_session_summary_from_cache(client, mock_db):
    mock_db.get_session.return_value = {"session_id": "s1", "source": "api"}
    mock_db.get_compression_tip.return_value = "Cached summary"
    r = client.get("/v1/sessions/s1/summary")
    assert r.status_code == 200
    assert r.json()["summary"] == "Cached summary"
    assert r.json()["source"] == "cached"


def test_session_summary_empty_messages(client, mock_db):
    mock_db.get_session.return_value = {"session_id": "s1", "source": "api"}
    mock_db.get_compression_tip.return_value = None
    mock_db.get_messages.return_value = []
    r = client.get("/v1/sessions/s1/summary")
    assert r.status_code == 200
    assert r.json()["source"] == "empty"


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

def test_list_memory_empty(client):
    with patch("hermes_api.routers.memory._load_store") as mock_store:
        store = MagicMock()
        store.memory_entries = []
        store.user_entries = []
        mock_store.return_value = store
        r = client.get("/v1/memory?kind=memory")
    assert r.status_code == 200
    assert r.json() == []


def test_add_memory(client):
    with patch("hermes_api.routers.memory._load_store") as mock_store, \
         patch("tools.memory_tool._scan_memory_content", return_value=None):
        store = MagicMock()
        store.memory_entries = []
        store.user_entries = []
        mock_store.return_value = store

        r = client.post("/v1/memory", json={"kind": "memory", "content": "Remember X"})

    assert r.status_code == 201
    assert r.json()["content"] == "Remember X"


# ---------------------------------------------------------------------------
# Cron
# ---------------------------------------------------------------------------

def test_list_cron_empty(client):
    # cron.jobs is lazily imported inside the handler, so patch at source
    try:
        import cron.jobs
        with patch("cron.jobs.list_jobs", return_value=[]):
            r = client.get("/v1/cron")
        assert r.status_code == 200
        assert r.json() == []
    except ImportError:
        pytest.skip("cron package not available")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def test_list_tools(client):
    mock_tools = [
        {"type": "function", "function": {"name": "web_search", "description": "Search the web"}},
    ]
    # get_tool_definitions is lazily imported from model_tools
    try:
        import model_tools
        with patch("model_tools.get_tool_definitions", return_value=mock_tools):
            r = client.get("/v1/tools")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1
        assert body["tools"][0]["name"] == "web_search"
    except ImportError:
        pytest.skip("model_tools not available")


# ---------------------------------------------------------------------------
# Platforms
# ---------------------------------------------------------------------------

def test_list_platforms_no_gateway_config(client):
    # load_gateway_config is lazily imported from gateway.config
    with patch("gateway.config.load_gateway_config", side_effect=RuntimeError("no gateway")):
        r = client.get("/v1/platforms")
    assert r.status_code == 200
    body = r.json()
    assert "connected" in body


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def test_get_config(client):
    mock_cfg = {"model": {"provider": "anthropic", "name": "claude-opus-4.6"}, "api_key": "secret"}
    # load_config is lazily imported from hermes_cli.config
    with patch("hermes_cli.config.load_config", return_value=mock_cfg):
        r = client.get("/v1/config")
    assert r.status_code == 200
    body = r.json()
    # Secret values should be redacted
    assert body["api_key"] == "***"
    assert body["model"]["provider"] == "anthropic"
