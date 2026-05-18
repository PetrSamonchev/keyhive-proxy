import asyncio
import json
import logging
from typing import TYPE_CHECKING

import websockets
import websockets.exceptions

from keyhive_proxy import auth

if TYPE_CHECKING:
    from keyhive_proxy.key_manager import KeyManager
    from keyhive_proxy.log_store import LogStore

logger = logging.getLogger(__name__)

_BACKOFF = [1, 2, 4, 8, 16, 30, 60]
_PING_INTERVAL = 30


class KHGTunnel:
    def __init__(
        self,
        khg_base_url: str,
        log_store: "LogStore",
        key_manager: "KeyManager",
    ):
        self._ws_base = (
            khg_base_url.rstrip("/")
            .replace("https://", "wss://")
            .replace("http://", "ws://")
        )
        self._log_store = log_store
        self._key_manager = key_manager
        self._task: asyncio.Task | None = None
        self._state = "disconnected"

    @property
    def connection_state(self) -> str:
        return self._state

    def _ws_url(self) -> str | None:
        token = auth.get_session_token()
        if not token:
            return None
        return f"{self._ws_base}/ws/proxy-tunnel?token={token}"

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run_with_reconnect())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._state = "disconnected"

    async def _run_with_reconnect(self) -> None:
        backoff_idx = 0
        while True:
            self._state = "reconnecting"
            url = self._ws_url()
            if not url:
                self._state = "disconnected"
                await asyncio.sleep(10)
                continue
            try:
                await self._connect(url)
                backoff_idx = 0
            except asyncio.CancelledError:
                self._state = "disconnected"
                return
            except Exception as exc:
                logger.warning("KHG tunnel disconnected: %s", exc)

            self._state = "disconnected"
            delay = _BACKOFF[min(backoff_idx, len(_BACKOFF) - 1)]
            backoff_idx += 1
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                self._state = "disconnected"
                return

    async def _connect(self, url: str) -> None:
        async with websockets.connect(
            url,
            ping_interval=_PING_INTERVAL,
            ping_timeout=10,
        ) as ws:
            self._state = "connected"
            logger.info("KHG tunnel connected")
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                try:
                    await self._handle(ws, msg)
                except Exception as exc:
                    logger.warning("tunnel message handler error: %s", exc)

    async def _handle(self, ws, msg: dict) -> None:
        mtype = msg.get("type")

        if mtype == "ping":
            await ws.send(json.dumps({"type": "pong"}))

        elif mtype == "pull_logs":
            req_id = msg.get("req_id", "")
            since = msg.get("since", "1970-01-01T00:00:00")
            limit = int(msg.get("limit", 200))
            records = await self._log_store.get_logs(since, limit)
            await ws.send(json.dumps({"type": "log_batch", "req_id": req_id, "records": records}))

        elif mtype == "token_sync":
            await self._key_manager._sync_tokens()
            await ws.send(json.dumps({"type": "token_sync_ack"}))
