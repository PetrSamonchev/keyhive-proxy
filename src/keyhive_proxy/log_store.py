import aiosqlite
from datetime import datetime, timedelta, timezone
from pathlib import Path

_CREATE_REQUESTS_SQL = """
CREATE TABLE IF NOT EXISTS proxy_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    bundle_id   TEXT,
    slot_id     TEXT,
    token_id    TEXT,
    provider    TEXT,
    model       TEXT,
    tokens_in   INTEGER,
    tokens_out  INTEGER,
    latency_ms  INTEGER,
    outcome     TEXT,
    http_status INTEGER,
    error_code  TEXT
);
CREATE INDEX IF NOT EXISTS idx_ts ON proxy_requests(ts);
"""

_CREATE_TOKENS_CACHE_SQL = """
CREATE TABLE IF NOT EXISTS app_tokens (
    token_id    TEXT PRIMARY KEY,
    token_value TEXT NOT NULL,
    label       TEXT,
    is_active   INTEGER DEFAULT 1,
    synced_at   TEXT
);
"""


class LogStore:
    def __init__(self, db_path: Path | None = None):
        if db_path is None:
            from keyhive_proxy.config import get_db_path
            db_path = get_db_path()
        self._path = str(db_path)

    async def init(self) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.executescript(_CREATE_REQUESTS_SQL + _CREATE_TOKENS_CACHE_SQL)
            await db.commit()

    async def insert(self, record: dict) -> None:
        ts = record.get("ts") or datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO proxy_requests
                    (ts, bundle_id, slot_id, token_id, provider, model,
                     tokens_in, tokens_out, latency_ms, outcome, http_status, error_code)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    ts,
                    record.get("bundle_id"),
                    record.get("slot_id"),
                    record.get("token_id"),
                    record.get("provider"),
                    record.get("model"),
                    record.get("tokens_in"),
                    record.get("tokens_out"),
                    record.get("latency_ms"),
                    record.get("outcome"),
                    record.get("http_status"),
                    record.get("error_code"),
                ],
            )
            await db.commit()

    async def get_logs(self, since_iso: str, limit: int) -> list[dict]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM proxy_requests WHERE ts >= ? ORDER BY ts DESC LIMIT ?",
                [since_iso, limit],
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def purge_old(self, retention_days: int) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        async with aiosqlite.connect(self._path) as db:
            await db.execute("DELETE FROM proxy_requests WHERE ts < ?", [cutoff])
            await db.commit()

    async def save_tokens_cache(self, tokens: list[dict]) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute("DELETE FROM app_tokens")
            now = datetime.now(timezone.utc).isoformat()
            for t in tokens:
                await db.execute(
                    "INSERT OR REPLACE INTO app_tokens"
                    " (token_id, token_value, label, is_active, synced_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    [
                        t.get("token_id", t.get("id", "")),
                        t.get("token_value", ""),
                        t.get("label", ""),
                        int(bool(t.get("is_active", True))),
                        now,
                    ],
                )
            await db.commit()

    async def load_tokens_cache(self) -> list[dict]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM app_tokens WHERE is_active = 1"
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]
