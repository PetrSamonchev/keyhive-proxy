import asyncio
import json
import logging
from typing import TYPE_CHECKING

import httpx
import websockets
import websockets.exceptions

from keyhive_proxy import auth

if TYPE_CHECKING:
    from keyhive_proxy.key_manager import KeyManager
    from keyhive_proxy.log_store import LogStore

logger = logging.getLogger(__name__)

_BACKOFF = [3, 5, 10, 30, 60, 120, 300]
_PING_INTERVAL = 30


def _next_backoff(attempt: int) -> int:
    return _BACKOFF[min(attempt, len(_BACKOFF) - 1)]


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
        self._http_base = khg_base_url.rstrip("/")
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

    async def _preflight_check(self) -> dict:
        """Check host reachability, auth validity, and endpoint existence via HTTP."""
        token = auth.get_session_token()
        if not token:
            return {
                "host_reachable": False,
                "auth_valid": False,
                "endpoint_exists": False,
                "http_status": None,
                "error": "no session token",
                "response_body": None,
            }

        base = self._http_base
        auth_valid = False
        host_reachable = False

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{base}/api/v1/proxy/app-tokens",
                    headers={"Authorization": f"Bearer {token}"},
                )
                host_reachable = True
                auth_valid = resp.status_code == 200
        except Exception as exc:
            return {
                "host_reachable": False,
                "auth_valid": False,
                "endpoint_exists": False,
                "http_status": None,
                "error": str(exc),
                "response_body": None,
            }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{base}/ws/proxy-tunnel",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Connection": "Upgrade",
                        "Upgrade": "websocket",
                    },
                )
                body = resp.text[:500] if resp.text else None
                return {
                    "host_reachable": True,
                    "auth_valid": auth_valid,
                    "endpoint_exists": resp.status_code not in (404, 501),
                    "http_status": resp.status_code,
                    "response_body": body,
                    "error": None,
                }
        except Exception as exc:
            return {
                "host_reachable": host_reachable,
                "auth_valid": auth_valid,
                "endpoint_exists": False,
                "http_status": None,
                "error": str(exc),
                "response_body": None,
            }

    def _log_preflight(self, result: dict, attempt: int) -> None:
        if not result["host_reachable"]:
            logger.error(
                "TUNNEL [attempt %d] Host unreachable: %s\n"
                "  → Check network connection and khg_base_url in settings",
                attempt, result["error"],
            )
            return

        if not result["auth_valid"]:
            logger.error(
                "TUNNEL [attempt %d] Authentication failed\n"
                "  → Session token may have expired\n"
                "  → Sign out and sign in again via Settings",
                attempt,
            )
            return

        status = result["http_status"]
        body   = result["response_body"] or ""
        wait   = _next_backoff(attempt)

        if status == 404:
            logger.error(
                "TUNNEL [attempt %d] WebSocket endpoint not found (HTTP 404)\n"
                "  → /ws/proxy-tunnel is not deployed on this KHG server\n"
                "  → Server version may be outdated — contact KHG support\n"
                "  URL: %s/ws/proxy-tunnel",
                attempt, self._http_base,
            )
        elif status == 502:
            logger.error(
                "TUNNEL [attempt %d] Bad Gateway (HTTP 502)\n"
                "  → KHG server or its WebSocket handler returned 502\n"
                "  → Possible causes:\n"
                "     1. /ws/proxy-tunnel endpoint not yet deployed\n"
                "     2. Upstream service crash behind load balancer\n"
                "     3. WebSocket not enabled on this deployment\n"
                "  Response body: %s\n"
                "  → Proxy continues working without tunnel\n"
                "  → Will retry in %ds",
                attempt, body, wait,
            )
        elif status == 503:
            logger.warning(
                "TUNNEL [attempt %d] Service unavailable (HTTP 503)\n"
                "  → KHG server is temporarily overloaded or restarting\n"
                "  → Will retry in %ds",
                attempt, wait,
            )
        elif status == 401:
            logger.error(
                "TUNNEL [attempt %d] Unauthorized (HTTP 401)\n"
                "  → Session token rejected by server\n"
                "  → Try signing out and signing in again",
                attempt,
            )
        elif status and status not in (200, 101, 426):
            logger.warning(
                "TUNNEL [attempt %d] Unexpected HTTP %d\n"
                "  Response body: %s",
                attempt, status, body,
            )
        else:
            logger.info(
                "TUNNEL [attempt %d] Pre-flight OK (HTTP %d) — attempting WebSocket",
                attempt, status,
            )

    async def _connect_once(self, attempt: int) -> bool:
        """Try one WebSocket connection. Returns True if cleanly disconnected."""
        url = self._ws_url()
        if not url:
            logger.error("TUNNEL [attempt %d] No session token — cannot connect", attempt)
            return False

        logger.info("TUNNEL [attempt %d] Connecting to %s", attempt, url)
        try:
            async with websockets.connect(
                url,
                open_timeout=15,
                ping_interval=_PING_INTERVAL,
                ping_timeout=10,
                close_timeout=5,
            ) as ws:
                self._state = "connected"
                logger.info("TUNNEL [attempt %d] Connected successfully", attempt)
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    try:
                        await self._handle(ws, msg)
                    except Exception as exc:
                        logger.warning("tunnel message handler error: %s", exc)
                return True

        except websockets.exceptions.InvalidStatus as exc:
            status = exc.response.status_code if exc.response else "unknown"
            body = ""
            try:
                body = exc.response.body.decode()[:300] if (exc.response and exc.response.body) else ""
            except Exception:
                pass
            logger.error(
                "TUNNEL [attempt %d] Connection rejected: HTTP %s\n"
                "  URL:  %s\n"
                "  Body: %s",
                attempt, status, url, body,
            )
            return False

        except websockets.exceptions.ConnectionClosedError as exc:
            logger.warning(
                "TUNNEL [attempt %d] Connection closed unexpectedly\n"
                "  Code:   %s\n"
                "  Reason: %s",
                attempt, exc.code, exc.reason,
            )
            return False

        except (OSError, asyncio.TimeoutError) as exc:
            logger.error(
                "TUNNEL [attempt %d] Network error: %s", attempt, str(exc)
            )
            return False

        except Exception as exc:
            logger.exception(
                "TUNNEL [attempt %d] Unexpected error: %s", attempt, str(exc)
            )
            return False

        finally:
            self._state = "disconnected"

    async def _run_with_reconnect(self) -> None:
        attempt = 0
        while True:
            self._state = "reconnecting"

            try:
                result = await self._preflight_check()
            except asyncio.CancelledError:
                self._state = "disconnected"
                return
            except Exception as exc:
                result = {
                    "host_reachable": False,
                    "auth_valid": False,
                    "endpoint_exists": False,
                    "http_status": None,
                    "error": str(exc),
                    "response_body": None,
                }

            self._log_preflight(result, attempt + 1)

            if result["host_reachable"] and result["auth_valid"]:
                try:
                    success = await self._connect_once(attempt + 1)
                except asyncio.CancelledError:
                    self._state = "disconnected"
                    return
                if success:
                    attempt = 0
                    continue

            attempt += 1
            wait = _next_backoff(attempt)

            if attempt == 7:
                logger.error(
                    "TUNNEL 7 consecutive failures — switching to long retry (300s)\n"
                    "  Proxy HTTP server continues working normally\n"
                    "  KHG dashboard will show this proxy as offline\n"
                    "  Log pull from dashboard will be unavailable\n"
                    "  To diagnose: keyhive-proxy tunnel-check"
                )

            logger.info("TUNNEL retrying in %ds (attempt %d)", wait, attempt + 1)
            try:
                await asyncio.sleep(wait)
            except asyncio.CancelledError:
                self._state = "disconnected"
                return

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


async def run_preflight(base_url: str) -> dict:
    """Standalone preflight check for the tunnel-check CLI command."""
    obj = object.__new__(KHGTunnel)
    obj._http_base = base_url.rstrip("/")
    return await obj._preflight_check()
