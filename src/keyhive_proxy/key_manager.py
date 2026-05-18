import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional

import httpx

from keyhive_proxy import auth

if TYPE_CHECKING:
    from keyhive_proxy.log_store import LogStore

logger = logging.getLogger(__name__)

_FETCH_BACKOFF = [1.0, 2.0, 4.0, 30.0]


@dataclass
class Slot:
    slot_id: str
    api_key: str
    endpoint: str
    model: str
    provider: str
    priority: int = 0


@dataclass
class Bundle:
    bundle_id: str
    expires_at: datetime
    slots: list[Slot]
    _index: int = field(default=0, init=False, repr=False)


class KeyManager:
    def __init__(self, khg_base_url: str, log_store: "LogStore"):
        self._base_url = khg_base_url.rstrip("/")
        self._log_store = log_store
        self._bundles: dict[str, Bundle] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._tokens: dict[str, dict] = {}
        self._token_by_value: dict[str, str] = {}
        self._sync_task: asyncio.Task | None = None
        self.last_sync_at: Optional[datetime] = None

    def _get_token(self) -> str:
        return auth.get_session_token() or ""

    async def start(self) -> None:
        await self._sync_tokens()
        self._sync_task = asyncio.create_task(self._sync_loop())

    async def stop(self) -> None:
        if self._sync_task:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass

    async def _sync_loop(self) -> None:
        while True:
            await asyncio.sleep(300)
            try:
                await self._sync_tokens()
            except Exception as exc:
                logger.warning("periodic token sync failed: %s", exc)

    async def _sync_tokens(self) -> None:
        tokens: list[dict] = []
        try:
            token = self._get_token()
            if not token:
                raise RuntimeError("no session token")
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    f"{self._base_url}/api/v1/proxy/app-tokens",
                    headers={"Authorization": f"Bearer {token}"},
                )
                if resp.status_code == 401:
                    auth.notify_session_expired()
                    tokens = await self._log_store.load_tokens_cache()
                else:
                    resp.raise_for_status()
                    tokens = resp.json().get("tokens", [])
                    await self._log_store.save_tokens_cache(tokens)
        except Exception as exc:
            logger.warning("failed to fetch app tokens from KHG (%s); using cache", exc)
            tokens = await self._log_store.load_tokens_cache()

        new_tokens: dict[str, dict] = {}
        new_by_value: dict[str, str] = {}
        for t in tokens:
            if not t.get("is_active", True):
                continue
            tid = t.get("token_id") or t.get("id", "")
            if not tid:
                continue
            new_tokens[tid] = t
            tv = t.get("token_value", "")
            if tv:
                new_by_value[tv] = tid

        self._tokens = new_tokens
        self._token_by_value = new_by_value
        logger.info("synced %d app tokens", len(new_tokens))

    async def sync_all(self) -> None:
        """Public sync: refresh tokens and clear cached bundles."""
        await self._sync_tokens()
        self._bundles.clear()
        self.last_sync_at = datetime.now(timezone.utc)
        logger.info("sync_all complete at %s", self.last_sync_at.isoformat())

    def _get_lock(self, token_id: str) -> asyncio.Lock:
        if token_id not in self._locks:
            self._locks[token_id] = asyncio.Lock()
        return self._locks[token_id]

    async def validate_token(self, value: str) -> str | None:
        tid = self._token_by_value.get(value)
        if tid:
            return tid
        await self._sync_tokens()
        return self._token_by_value.get(value)

    async def get_slot(self, token_id: str) -> Slot | None:
        async with self._get_lock(token_id):
            bundle = self._bundles.get(token_id)
            if bundle is None or self._bundle_expired(bundle) or bundle._index >= len(bundle.slots):
                bundle = await self._fetch_bundle(token_id)
                if bundle is None:
                    return None
                self._bundles[token_id] = bundle
            return bundle.slots[bundle._index] if bundle.slots else None

    async def rotate_slot(self, token_id: str) -> Slot | None:
        async with self._get_lock(token_id):
            bundle = self._bundles.get(token_id)
            if bundle is None:
                return None
            bundle._index += 1
            if bundle._index >= len(bundle.slots):
                new_bundle = await self._fetch_bundle(token_id)
                if new_bundle and new_bundle.slots:
                    self._bundles[token_id] = new_bundle
                    return new_bundle.slots[0]
                return None
            return bundle.slots[bundle._index]

    def handle_status_response(self, token_id: str, response_json: dict) -> None:
        if "expires_at" in response_json:
            bundle = self._bundles.get(token_id)
            if bundle:
                bundle.expires_at = datetime.fromtimestamp(0, tz=timezone.utc)

    def get_active_slot_count(self) -> int:
        return sum(
            len(b.slots)
            for b in self._bundles.values()
            if not self._bundle_expired(b)
        )

    def _bundle_expired(self, bundle: Bundle) -> bool:
        return datetime.now(timezone.utc) >= bundle.expires_at

    async def _fetch_bundle(self, token_id: str) -> Bundle | None:
        for delay in _FETCH_BACKOFF:
            try:
                token = self._get_token()
                if not token:
                    logger.warning("no session token — cannot fetch bundle")
                    return None
                async with httpx.AsyncClient(timeout=20.0) as client:
                    resp = await client.post(
                        f"{self._base_url}/api/v1/proxy/bundle",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"token_id": token_id},
                    )
                    if resp.status_code == 401:
                        auth.notify_session_expired()
                        return None
                    resp.raise_for_status()
                    data = resp.json()

                slots = [
                    Slot(
                        slot_id=s["slot_id"],
                        api_key=s["api_key"],
                        endpoint=s["endpoint"],
                        model=s["model"],
                        provider=s["provider"],
                        priority=s.get("priority", 0),
                    )
                    for s in data.get("slots", [])
                ]
                slots.sort(key=lambda s: s.priority)

                exp_str = data.get("expires_at")
                if exp_str:
                    expires_at = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
                else:
                    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)

                return Bundle(bundle_id=data["bundle_id"], expires_at=expires_at, slots=slots)

            except Exception as exc:
                logger.warning("bundle fetch attempt failed: %s", exc)
                await asyncio.sleep(delay)

        logger.error("bundle fetch exhausted all retries for token_id=%s", token_id)
        return None
