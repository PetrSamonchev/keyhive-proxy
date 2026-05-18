"""Tests for LogStore — insert, retrieve, purge, no body content in schema."""
import pytest
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from keyhive_proxy.log_store import LogStore


@pytest.fixture
async def store(tmp_path):
    s = LogStore(db_path=tmp_path / "test.db")
    await s.init()
    return s


async def test_insert_and_retrieve(store):
    ts = datetime.now(timezone.utc).isoformat()
    await store.insert(
        {
            "ts": ts,
            "provider": "openai",
            "model": "gpt-4o-mini",
            "outcome": "success",
            "latency_ms": 234,
            "tokens_in": 10,
            "tokens_out": 50,
            "http_status": 200,
        }
    )
    rows = await store.get_logs("1970-01-01T00:00:00", 10)
    assert len(rows) == 1
    assert rows[0]["provider"] == "openai"
    assert rows[0]["model"] == "gpt-4o-mini"
    assert rows[0]["outcome"] == "success"
    assert rows[0]["tokens_in"] == 10
    assert rows[0]["tokens_out"] == 50


async def test_purge_old_records(store):
    old_ts = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    new_ts = datetime.now(timezone.utc).isoformat()
    await store.insert({"ts": old_ts, "outcome": "success"})
    await store.insert({"ts": new_ts, "outcome": "success"})

    await store.purge_old(retention_days=30)

    rows = await store.get_logs("1970-01-01T00:00:00", 100)
    assert len(rows) == 1
    assert rows[0]["ts"][:10] == new_ts[:10]


async def test_no_content_stored(store):
    """Verify the schema has no body/content/prompt/response fields."""
    import aiosqlite

    async with aiosqlite.connect(store._path) as db:
        async with db.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'proxy_requests'"
        ) as cur:
            row = await cur.fetchone()
    schema = row[0].lower()
    for forbidden in ("body", "content", "prompt", "response", "message"):
        assert forbidden not in schema, f"Schema must not contain '{forbidden}' column"


async def test_tokens_cache_roundtrip(store):
    tokens = [
        {"token_id": "t1", "token_value": "sk-khg-aaa", "label": "Cursor", "is_active": 1},
        {"token_id": "t2", "token_value": "sk-khg-bbb", "label": "Cline", "is_active": 0},
    ]
    await store.save_tokens_cache(tokens)
    cached = await store.load_tokens_cache()
    # Only active tokens returned
    assert len(cached) == 1
    assert cached[0]["token_id"] == "t1"


async def test_get_logs_since_filter(store):
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    recent = datetime.now(timezone.utc).isoformat()
    await store.insert({"ts": past, "outcome": "success"})
    await store.insert({"ts": recent, "outcome": "error"})

    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    rows = await store.get_logs(one_hour_ago, 10)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "error"
