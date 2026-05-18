"""Tests for ProxyServer — auth, routing, retry on 429, 503 on exhaustion."""
import json
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp.test_utils import TestClient, TestServer

from keyhive_proxy.key_manager import Bundle, Slot
from keyhive_proxy.server import ProxyServer


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _slot(slot_id="s1", api_key="sk-test", model="gpt-4o-mini", provider="openai"):
    return Slot(
        slot_id=slot_id,
        api_key=api_key,
        endpoint="https://api.openai.com/v1/chat/completions",
        model=model,
        provider=provider,
    )


@pytest.fixture
def log_store_mock():
    m = MagicMock()
    m.insert = AsyncMock()
    return m


@pytest.fixture
def reporter_mock():
    m = MagicMock()
    m.report = AsyncMock()
    return m


@pytest.fixture
def km_mock():
    m = MagicMock()
    m.validate_token = AsyncMock(return_value=None)
    m.get_slot = AsyncMock(return_value=None)
    m.rotate_slot = AsyncMock(return_value=None)
    m.get_active_slot_count = MagicMock(return_value=0)
    m._bundles = {}
    m._bundle_expired = MagicMock(return_value=False)
    return m


@pytest.fixture(autouse=True)
def mock_session(monkeypatch):
    monkeypatch.setattr("keyhive_proxy.auth.get_session_token", lambda: "fake-session-token")


@pytest.fixture
async def client(km_mock, reporter_mock, log_store_mock):
    server = ProxyServer(
        key_manager=km_mock,
        status_reporter=reporter_mock,
        log_store=log_store_mock,
        port=0,
    )
    ts = TestServer(server._app)
    c = TestClient(ts)
    await c.start_server()
    yield c
    await c.close()


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------

async def test_missing_auth_returns_401(client):
    resp = await client.post("/v1/chat/completions", json={"messages": []})
    assert resp.status == 401
    data = await resp.json()
    assert data["error"]["type"] == "authentication_error"


async def test_invalid_token_returns_401(client, km_mock):
    km_mock.validate_token = AsyncMock(return_value=None)
    resp = await client.post(
        "/v1/chat/completions",
        json={"messages": []},
        headers={"Authorization": "Bearer bad-token"},
    )
    assert resp.status == 401


# ---------------------------------------------------------------------------
# Valid token proxies request
# ---------------------------------------------------------------------------

async def test_valid_token_proxies_request(client, km_mock):
    km_mock.validate_token = AsyncMock(return_value="tid-1")
    km_mock.get_slot = AsyncMock(return_value=_slot())

    upstream_response = {
        "id": "chatcmpl-xxx",
        "choices": [{"message": {"role": "assistant", "content": "Hello"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 10},
    }

    with patch("httpx.AsyncClient") as mock_cls:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = json.dumps(upstream_response).encode()
        mock_resp.json.return_value = upstream_response
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        resp = await client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "Bearer sk-khg-valid"},
        )

    assert resp.status == 200
    data = await resp.json()
    assert "choices" in data


# ---------------------------------------------------------------------------
# Rotation on provider 429
# ---------------------------------------------------------------------------

async def test_rotation_on_provider_429(client, km_mock):
    km_mock.validate_token = AsyncMock(return_value="tid-1")
    slot_a = _slot(slot_id="sa")
    slot_b = _slot(slot_id="sb")
    km_mock.get_slot = AsyncMock(side_effect=[slot_a, slot_a, slot_b, slot_b])
    km_mock.rotate_slot = AsyncMock(return_value=slot_b)

    upstream_200 = {
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }

    call_count = 0

    def _side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        m = MagicMock()
        if call_count == 1:
            m.status_code = 429
            m.content = b""
            m.json.return_value = {}
        else:
            m.status_code = 200
            m.content = json.dumps(upstream_200).encode()
            m.json.return_value = upstream_200
        return m

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=_side_effect)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        resp = await client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "Bearer sk-khg-valid"},
        )

    assert resp.status == 200
    assert call_count == 2


# ---------------------------------------------------------------------------
# 503 when all slots fail
# ---------------------------------------------------------------------------

async def test_all_slots_fail_returns_503(client, km_mock):
    km_mock.validate_token = AsyncMock(return_value="tid-1")
    km_mock.get_slot = AsyncMock(return_value=_slot())
    km_mock.rotate_slot = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient") as mock_cls:
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.content = b""
        mock_resp.json.return_value = {}
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        resp = await client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "Bearer sk-khg-valid"},
        )

    assert resp.status == 503


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

async def test_health_endpoint(client, km_mock):
    km_mock.get_active_slot_count = MagicMock(return_value=3)
    resp = await client.get("/health")
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"
    assert data["slots_active"] == 3


# ---------------------------------------------------------------------------
# Models endpoint
# ---------------------------------------------------------------------------

async def test_models_endpoint_returns_list(client, km_mock):
    from keyhive_proxy.key_manager import Bundle
    km_mock.validate_token = AsyncMock(return_value="tid-1")
    b = Bundle(
        bundle_id="b1",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        slots=[_slot(model="gpt-4o-mini"), _slot(slot_id="s2", model="gpt-4o")],
    )
    km_mock._bundles = {"tid-1": b}
    km_mock._bundle_expired = MagicMock(return_value=False)

    resp = await client.get(
        "/v1/models",
        headers={"Authorization": "Bearer sk-khg-valid"},
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["object"] == "list"
    model_ids = [m["id"] for m in data["data"]]
    assert "gpt-4o-mini" in model_ids
    assert "gpt-4o" in model_ids
