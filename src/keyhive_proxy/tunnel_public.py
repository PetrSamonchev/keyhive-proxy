import asyncio
import logging
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
_BACKOFF = [2, 4, 8, 16, 30, 60]


def _find_cloudflared() -> Path | None:
    # Bundled by PyInstaller
    if hasattr(sys, "_MEIPASS"):
        name = "cloudflared.exe" if sys.platform == "win32" else "cloudflared"
        p = Path(sys._MEIPASS) / "assets" / name
        if p.exists():
            return p

    # Alongside this source file (development)
    name = "cloudflared.exe" if sys.platform == "win32" else "cloudflared"
    p = Path(__file__).parent / "tray" / "assets" / name
    if p.exists():
        return p

    return None


class PublicTunnel:
    def __init__(self, port: int, khg_base_url: str, khg_api_key: str):
        self._port = port
        self._base_url = khg_base_url.rstrip("/")
        self._api_key = khg_api_key
        self._public_url: str | None = None
        self._monitor_task: asyncio.Task | None = None
        self._proc: subprocess.Popen | None = None

    @property
    def public_url(self) -> str | None:
        return self._public_url

    async def start(self) -> None:
        self._monitor_task = asyncio.create_task(self._run_with_restart())

    async def stop(self) -> None:
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        self._kill_proc()

    def _kill_proc(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None

    async def _run_with_restart(self) -> None:
        backoff_idx = 0
        while True:
            try:
                await self._launch()
                backoff_idx = 0
            except asyncio.CancelledError:
                self._kill_proc()
                return
            except Exception as exc:
                logger.warning("cloudflared exited unexpectedly: %s", exc)

            self._public_url = None
            delay = _BACKOFF[min(backoff_idx, len(_BACKOFF) - 1)]
            backoff_idx += 1
            logger.info("restarting cloudflared in %ds", delay)
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return

    async def _launch(self) -> None:
        cf = _find_cloudflared()
        if cf is None:
            logger.warning("cloudflared binary not found — public URL unavailable")
            await asyncio.sleep(3600)
            return

        cmd = [str(cf), "tunnel", "--url", f"http://localhost:{self._port}", "--no-autoupdate"]
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        loop = asyncio.get_event_loop()
        while True:
            line = await loop.run_in_executor(None, self._proc.stdout.readline)
            if not line:
                break

            m = _URL_RE.search(line)
            if m and self._public_url is None:
                self._public_url = m.group(0)
                logger.info("cloudflare tunnel URL: %s", self._public_url)
                asyncio.create_task(self._register_url(self._public_url))

            if self._proc.poll() is not None:
                break

    async def _register_url(self, url: str) -> None:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{self._base_url}/api/v1/proxy/register-url",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={"public_url": f"{url}/v1"},
                )
                if resp.status_code < 300:
                    logger.info("registered public URL with KHG")
                else:
                    logger.warning("register-url returned HTTP %s", resp.status_code)
        except Exception as exc:
            logger.warning("failed to register public URL: %s", exc)
