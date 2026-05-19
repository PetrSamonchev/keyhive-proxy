import asyncio
import logging
from typing import TYPE_CHECKING

import httpx

from keyhive_proxy import auth
from keyhive_proxy.http_client import khg_headers

if TYPE_CHECKING:
    from keyhive_proxy.key_manager import KeyManager
    from keyhive_proxy.log_store import LogStore

logger = logging.getLogger(__name__)


class StatusReporter:
    def __init__(
        self,
        khg_base_url: str,
        log_store: "LogStore",
        key_manager: "KeyManager",
    ):
        self._base_url = khg_base_url.rstrip("/")
        self._log_store = log_store
        self._key_manager = key_manager
        self._queue: asyncio.Queue = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None

    async def start(self) -> None:
        self._worker_task = asyncio.create_task(self._worker())

    async def stop(self) -> None:
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass

    async def report(self, record: dict) -> None:
        await self._queue.put(record)

    async def _worker(self) -> None:
        while True:
            try:
                record = await self._queue.get()
            except asyncio.CancelledError:
                break
            try:
                await self._send(record)
            finally:
                self._queue.task_done()

    async def _send(self, record: dict) -> None:
        token = auth.get_session_token()
        proxy_id = auth.get_proxy_id()
        if not token:
            await self._log_store.insert({**record, "proxy_id": proxy_id, "outcome": "report_failed"})
            return
        body = {**record, "proxy_id": proxy_id}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{self._base_url}/api/v1/proxy/status",
                    headers=khg_headers(token, proxy_id),
                    json=body,
                )
            if resp.status_code == 401:
                auth.notify_session_expired()
                await self._log_store.insert({**record, "proxy_id": proxy_id, "outcome": "report_failed"})
            elif resp.status_code < 300:
                token_id = record.get("token_id")
                if token_id:
                    self._key_manager.handle_status_response(token_id, resp.json())
                await self._log_store.insert({**record, "proxy_id": proxy_id})
            else:
                logger.warning("status report returned HTTP %s", resp.status_code)
                await self._log_store.insert({**record, "proxy_id": proxy_id, "outcome": "report_failed"})
        except Exception as exc:
            logger.warning("status report failed: %s", exc)
            await self._log_store.insert({**record, "proxy_id": proxy_id, "outcome": "report_failed"})
