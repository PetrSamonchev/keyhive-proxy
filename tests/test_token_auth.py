"""Tests for app-token isolation — token A cannot access token B's bundle."""
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from keyhive_proxy.key_manager import Bundle, KeyManager, Slot


@pytest.fixture
def log_store_mock():
    m = MagicMock()
    m.save_tokens_cache = AsyncMock()
    m.load_tokens_cache = AsyncMock(return_value=[])
    return m


def _km(log_store):
    km = KeyManager(
        khg_base_url="https://khg.test",
        khg_api_key="master-key",
        log_store=log_store,
    )
    # Pre-load two tokens
    km._tokens = {
        "tid-a": {"token_id": "tid-a", "token_value": "sk-khg-aaa", "label": "App A", "is_active": 1},
        "tid-b": {"token_id": "tid-b", "token_value": "sk-khg-bbb", "label": "App B", "is_active": 1},
    }
    km._token_by_value = {"sk-khg-aaa": "tid-a", "sk-khg-bbb": "tid-b"}
    return km


def _bundle_for(token_id: str) -> Bundle:
    return Bundle(
        bundle_id=f"bundle-{token_id}",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        slots=[
            Slot(
                slot_id=f"slot-{token_id}",
                api_key=f"provider-key-{token_id}",
                endpoint="https://api.openai.com/v1/chat/completions",
                model="gpt-4o-mini",
                provider="openai",
            )
        ],
    )


async def test_token_per_app_isolation(log_store_mock):
    """Token A's bundle should be isolated from token B's bundle."""
    km = _km(log_store_mock)
    km._bundles["tid-a"] = _bundle_for("tid-a")
    km._bundles["tid-b"] = _bundle_for("tid-b")

    slot_a = await km.get_slot("tid-a")
    slot_b = await km.get_slot("tid-b")

    assert slot_a is not None
    assert slot_b is not None
    assert slot_a.api_key != slot_b.api_key
    assert slot_a.slot_id != slot_b.slot_id


async def test_revoked_token_returns_none(log_store_mock):
    """A token not in the in-memory map → validate_token returns None."""
    km = _km(log_store_mock)
    km._sync_tokens = AsyncMock()  # sync will not add the revoked token

    result = await km.validate_token("sk-khg-revoked")
    assert result is None


async def test_active_token_validates(log_store_mock):
    km = _km(log_store_mock)
    result = await km.validate_token("sk-khg-aaa")
    assert result == "tid-a"


async def test_inactive_token_not_in_map(log_store_mock):
    """Inactive tokens (is_active=0) are excluded from the in-memory map on sync."""
    km = _km(log_store_mock)
    tokens = [
        {"token_id": "tid-c", "token_value": "sk-khg-ccc", "label": "C", "is_active": 0},
    ]
    log_store_mock.load_tokens_cache = AsyncMock(return_value=tokens)

    # Simulate a sync that gets inactive token from cache
    import httpx
    from unittest.mock import patch

    with patch("httpx.AsyncClient") as mock_cls:
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"tokens": tokens}
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_cls.return_value = mock_client

        await km._sync_tokens()

    result = await km.validate_token("sk-khg-ccc")
    assert result is None


async def test_separate_locks_per_token(log_store_mock):
    """Each token_id gets its own asyncio.Lock — concurrent access is safe."""
    km = _km(log_store_mock)
    lock_a = km._get_lock("tid-a")
    lock_b = km._get_lock("tid-b")
    lock_a2 = km._get_lock("tid-a")

    assert lock_a is lock_a2      # same object returned for same token_id
    assert lock_a is not lock_b   # different tokens get different locks
