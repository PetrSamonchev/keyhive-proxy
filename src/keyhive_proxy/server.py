import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import httpx
from aiohttp import web

from keyhive_proxy import auth

if TYPE_CHECKING:
    from keyhive_proxy.key_manager import KeyManager, Slot
    from keyhive_proxy.log_store import LogStore
    from keyhive_proxy.status_reporter import StatusReporter

logger = logging.getLogger(__name__)

_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 4


class ProxyServer:
    def __init__(
        self,
        key_manager: "KeyManager",
        status_reporter: "StatusReporter",
        log_store: "LogStore",
        port: int,
    ):
        self._km = key_manager
        self._reporter = status_reporter
        self._ls = log_store
        self._port = port
        self._runner: web.AppRunner | None = None

        app = web.Application()
        app.router.add_post("/v1/chat/completions", self._handle_chat)
        app.router.add_post("/v1/messages", self._handle_messages)
        app.router.add_get("/v1/models", self._handle_models)
        app.router.add_get("/health", self._handle_health)
        self._app = app

    @property
    def port(self) -> int:
        if self._runner and self._runner.addresses:
            return self._runner.addresses[0][1]
        return self._port

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", self._port)
        await site.start()
        logger.info("proxy server listening on port %d", self.port)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _extract_token(self, request: web.Request) -> str | None:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:]
        return None

    async def _auth(self, request: web.Request):
        raw = self._extract_token(request)
        if not raw:
            return None, web.json_response(
                {"error": {"message": "Missing Bearer token", "type": "authentication_error"}},
                status=401,
            )
        token_id = await self._km.validate_token(raw)
        if not token_id:
            return None, web.json_response(
                {"error": {"message": "Invalid token", "type": "authentication_error"}},
                status=401,
            )
        return token_id, None

    def _fire_report(self, record: dict) -> None:
        asyncio.create_task(self._reporter.report(record))

    # ------------------------------------------------------------------
    # Route handlers
    # ------------------------------------------------------------------

    async def _handle_chat(self, request: web.Request) -> web.StreamResponse:
        return await self._handle_completions(request)

    async def _handle_messages(self, request: web.Request) -> web.StreamResponse:
        return await self._handle_completions(request)

    async def _handle_completions(self, request: web.Request) -> web.StreamResponse:
        if not auth.get_session_token():
            return web.json_response(
                {
                    "error": "proxy_not_configured",
                    "message": "Please sign in via keyhive-proxy tray icon → Settings",
                },
                status=503,
            )

        token_id, err = await self._auth(request)
        if err:
            return err

        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}},
                status=400,
            )

        is_streaming = bool(body.get("stream", False))

        for attempt in range(_MAX_RETRIES):
            slot = await self._km.get_slot(token_id)
            if slot is None:
                return web.json_response(
                    {"error": {"message": "No provider slots available", "type": "server_error"}},
                    status=503,
                )

            req_body = {**body, "model": slot.model}
            headers = {
                "Authorization": f"Bearer {slot.api_key}",
                "Content-Type": "application/json",
            }
            start = time.monotonic()

            try:
                if is_streaming:
                    return await self._forward_stream(
                        request, slot, req_body, headers, token_id, start
                    )

                async with httpx.AsyncClient(timeout=120.0) as client:
                    resp = await client.post(slot.endpoint, headers=headers, json=req_body)

                latency_ms = int((time.monotonic() - start) * 1000)

                if resp.status_code in _RETRY_STATUSES:
                    outcome = "rate_limited" if resp.status_code == 429 else "error"
                    self._fire_report(
                        self._make_record(slot, token_id, latency_ms, outcome, resp.status_code)
                    )
                    await self._km.rotate_slot(token_id)
                    continue

                usage = {}
                try:
                    usage = resp.json().get("usage", {})
                except Exception:
                    pass

                record = self._make_record(slot, token_id, latency_ms, "success", resp.status_code)
                record["tokens_in"] = usage.get("prompt_tokens", 0)
                record["tokens_out"] = usage.get("completion_tokens", 0)
                self._fire_report(record)

                return web.Response(
                    status=resp.status_code,
                    content_type="application/json",
                    body=resp.content,
                )

            except Exception as exc:
                latency_ms = int((time.monotonic() - start) * 1000)
                logger.warning("slot %s request failed: %s", slot.slot_id, exc)
                record = self._make_record(slot, token_id, latency_ms, "error", None)
                record["error_code"] = type(exc).__name__
                self._fire_report(record)
                await self._km.rotate_slot(token_id)

        return web.json_response(
            {"error": {"message": "All provider slots exhausted", "type": "server_error"}},
            status=503,
        )

    async def _forward_stream(
        self,
        request: web.Request,
        slot: "Slot",
        req_body: dict,
        headers: dict,
        token_id: str,
        start: float,
    ) -> web.StreamResponse:
        response = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
        )
        await response.prepare(request)

        tokens_in = tokens_out = 0
        outcome = "success"

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream("POST", slot.endpoint, headers=headers, json=req_body) as upstream:
                    if upstream.status_code not in (200, 201):
                        err_msg = json.dumps(
                            {"error": {"message": "Provider error", "type": "server_error"}}
                        )
                        await response.write(f"data: {err_msg}\n\n".encode())
                        outcome = "error"
                    else:
                        async for line in upstream.aiter_lines():
                            if not line:
                                continue
                            await response.write((line + "\n\n").encode())
                            if line.startswith("data:") and "[DONE]" not in line:
                                try:
                                    chunk = json.loads(line[5:].strip())
                                    u = chunk.get("usage") or {}
                                    if u:
                                        tokens_in = u.get("prompt_tokens", tokens_in)
                                        tokens_out = u.get("completion_tokens", tokens_out)
                                except Exception:
                                    pass
        except Exception as exc:
            logger.warning("streaming error slot=%s: %s", slot.slot_id, exc)
            outcome = "error"

        latency_ms = int((time.monotonic() - start) * 1000)
        record = self._make_record(slot, token_id, latency_ms, outcome, 200)
        record["tokens_in"] = tokens_in
        record["tokens_out"] = tokens_out
        self._fire_report(record)

        await response.write_eof()
        return response

    async def _handle_models(self, request: web.Request) -> web.Response:
        token_id, err = await self._auth(request)
        if err:
            return err

        bundle = self._km._bundles.get(token_id)
        models: list[dict] = []
        if bundle and not self._km._bundle_expired(bundle):
            seen: set[str] = set()
            for s in bundle.slots:
                if s.model not in seen:
                    seen.add(s.model)
                    models.append(
                        {"id": s.model, "object": "model", "created": 0, "owned_by": s.provider}
                    )

        return web.json_response({"object": "list", "data": models})

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response(
            {"status": "ok", "slots_active": self._km.get_active_slot_count()}
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_record(
        slot: "Slot",
        token_id: str,
        latency_ms: int,
        outcome: str,
        http_status: int | None,
    ) -> dict:
        return {
            "ts": datetime.now(timezone.utc).isoformat(),
            "slot_id": slot.slot_id,
            "token_id": token_id,
            "provider": slot.provider,
            "model": slot.model,
            "latency_ms": latency_ms,
            "outcome": outcome,
            "http_status": http_status,
        }
