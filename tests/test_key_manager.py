"""Tests for KeyManager — bundle fetch, rotation, TTL expiry, token validation."""
import hashlib
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from keyhive_proxy.key_manager import Bundle, KeyManager, Slot


def _h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def log_store_mock(tmp_path):
    m = MagicMock()
    m.save_tokens_cache = AsyncMock()
    m.load_tokens_cache = AsyncMock(return_value=[])
    return m


@pytest.fixture(autouse=True)
def mock_session(monkeypatch):
    monkeypatch.setattr("keyhive_proxy.auth.get_session_token", lambda: "fake-session-token")


def _make_km(log_store, base_url="https://khg.test"):
    return KeyManager(khg_base_url=base_url, log_store=log_store)


def _future(hours=1):
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def _past(hours=1):
    return datetime.now(timezone.utc) - timedelta(hours=hours)


def _bundle(n_slots=2, expired=False):
    slots = [
        Slot(slot_id=f"s{i}", api_key=f"key{i}", endpoint="https://api.openai.com/v1/chat/completions",
             model="gpt-4o-mini", provider="openai")
        for i in range(n_slots)
    ]
    exp = _past() if expired else _future()
    return Bundle(bundle_id="b1", expires_at=exp, slots=slots)


# ---------------------------------------------------------------------------
# Token validation
# ---------------------------------------------------------------------------

async def test_token_validation_hit(log_store_mock):
    km = _make_km(log_store_mock)
    km._token_by_hash[_h("sk-khg-abc")] = "tid-1"
    result = await km.validate_token("sk-khg-abc")
    assert result == "tid-1"


async def test_token_validation_miss_triggers_sync(log_store_mock):
    km = _make_km(log_store_mock)

    async def fake_sync():
        km._token_by_hash[_h("sk-khg-new")] = "tid-2"
    km._sync_tokens = AsyncMock(side_effect=fake_sync)

    result = await km.validate_token("sk-khg-new")
    assert result == "tid-2"
    km._sync_tokens.assert_called_once()


async def test_token_cache_miss_after_sync_returns_none(log_store_mock):
    km = _make_km(log_store_mock)
    km._sync_tokens = AsyncMock()
    result = await km.validate_token("sk-khg-unknown")
    assert result is None


# ---------------------------------------------------------------------------
# Bundle fetch success
# ---------------------------------------------------------------------------

async def test_bundle_fetch_success(log_store_mock):
    km = _make_km(log_store_mock)
    km._token_by_hash[_h("sk-khg-tok")] = "tid"

    bundle_response = {
        "bundle_id": "b123",
        "expires_at": _future(1).isoformat(),
        "slots": [
            {"slot_id": "s1", "api_key": "sk-real", "endpoint": "https://api.openai.com/v1/chat/completions",
             "model": "gpt-4o-mini", "provider": "openai", "priority": 0}
        ],
    }

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = bundle_response
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        slot = await km.get_slot("tid")

    assert slot is not None
    assert slot.slot_id == "s1"
    assert slot.api_key == "sk-real"


# ---------------------------------------------------------------------------
# Rotation on 429
# ---------------------------------------------------------------------------

async def test_rotation_on_429(log_store_mock):
    km = _make_km(log_store_mock)
    b = _bundle(n_slots=3)
    km._bundles["tid"] = b

    slot1 = await km.get_slot("tid")
    assert slot1.slot_id == "s0"

    slot2 = await km.rotate_slot("tid")
    assert slot2 is not None
    assert slot2.slot_id == "s1"

    slot3 = await km.rotate_slot("tid")
    assert slot3 is not None
    assert slot3.slot_id == "s2"


# ---------------------------------------------------------------------------
# All slots exhausted triggers re-fetch
# ---------------------------------------------------------------------------

async def test_all_slots_exhausted_triggers_refetch(log_store_mock):
    km = _make_km(log_store_mock)
    b = _bundle(n_slots=1)
    km._bundles["tid"] = b

    new_bundle_response = {
        "bundle_id": "b-new",
        "expires_at": _future(1).isoformat(),
        "slots": [
            {"slot_id": "s-fresh", "api_key": "fresh-key",
             "endpoint": "https://api.openai.com/v1/chat/completions",
             "model": "gpt-4o-mini", "provider": "openai", "priority": 0}
        ],
    }

    with patch("httpx.AsyncClient") as mock_cls:
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = new_bundle_response
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        # Exhaust the single slot
        await km.rotate_slot("tid")
        # Now get_slot should trigger re-fetch
        slot = await km.get_slot("tid")

    assert slot is not None
    assert slot.slot_id == "s-fresh"


# ---------------------------------------------------------------------------
# TTL expiry triggers re-fetch
# ---------------------------------------------------------------------------

async def test_ttl_expiry_triggers_refetch(log_store_mock):
    km = _make_km(log_store_mock)
    b = _bundle(n_slots=1, expired=True)
    km._bundles["tid"] = b

    assert km._bundle_expired(b) is True

    fresh_response = {
        "bundle_id": "b-fresh",
        "expires_at": _future(1).isoformat(),
        "slots": [
            {"slot_id": "s-new", "api_key": "new-key",
             "endpoint": "https://api.openai.com/v1/chat/completions",
             "model": "gpt-4o-mini", "provider": "openai", "priority": 0}
        ],
    }

    with patch("httpx.AsyncClient") as mock_cls:
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = fresh_response
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        slot = await km.get_slot("tid")

    assert slot is not None
    assert slot.slot_id == "s-new"


# ---------------------------------------------------------------------------
# status_response with expires_at
# ---------------------------------------------------------------------------

async def test_status_response_with_expires_at(log_store_mock):
    km = _make_km(log_store_mock)
    b = _bundle(n_slots=2)
    km._bundles["tid"] = b

    assert km._bundle_expired(b) is False
    km.handle_status_response("tid", {"expires_at": "2000-01-01T00:00:00Z"})
    assert km._bundle_expired(b) is True
