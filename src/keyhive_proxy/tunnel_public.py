import asyncio
import logging
import os
import re
import shutil
import sys
from pathlib import Path

import httpx

from keyhive_proxy import auth

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
_BACKOFF = [2, 4, 8, 16, 30, 60]


def _cloudflared_filename() -> str:
    if sys.platform == "win32":
        return "cloudflared.exe"
    return "cloudflared"


def _find_cloudflared() -> Path | None:
    name = _cloudflared_filename()
    candidates: list[Path] = []

    # 1. PyInstaller bundle (production)
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys._MEIPASS) / "assets" / name)

    # 2. Dev: relative to this file (keyhive_proxy/assets/)
    candidates.append(Path(__file__).parent / "assets" / name)

    # 3. Dev: project root assets/
    candidates.append(Path(__file__).parent.parent.parent / "assets" / name)

    # 4. System PATH
    system = shutil.which("cloudflared")
    if system:
        candidates.append(Path(system))

    # 5. Common install locations
    if sys.platform == "win32":
        candidates += [
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "cloudflared" / name,
            Path("C:/Program Files/cloudflared") / name,
        ]
    elif sys.platform == "darwin":
        candidates += [
            Path("/usr/local/bin/cloudflared"),
            Path("/opt/homebrew/bin/cloudflared"),
        ]
    else:
        candidates += [
            Path("/usr/local/bin/cloudflared"),
            Path("/usr/bin/cloudflared"),
        ]

    for p in candidates:
        try:
            if p.exists():
                logger.info("TUNNEL_PUBLIC cloudflared found: %s", p)
                return p
        except Exception:
            pass

    logger.warning(
        "TUNNEL_PUBLIC cloudflared not found\n"
        "  Searched locations:\n%s\n"
        "  Public URL will be unavailable\n"
        "  To fix: download cloudflared from\n"
        "    https://github.com/cloudflare/cloudflared/releases\n"
        "  and place it in: %s",
        "\n".join(f"    {p}" for p in candidates),
        Path(__file__).parent / "assets" / name,
    )
    return None


class PublicTunnel:
    def __init__(self, port: int, khg_base_url: str):
        self._port = port
        self._base_url = khg_base_url.rstrip("/")
        self._public_url: str | None = None
        self._monitor_task: asyncio.Task | None = None
        self._proc: asyncio.subprocess.Process | None = None

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
        await self._kill_proc()

    async def _kill_proc(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        except Exception:
            pass
        self._proc = None

    async def _run_with_restart(self) -> None:
        backoff_idx = 0
        while True:
            try:
                await self._launch()
                backoff_idx = 0
            except asyncio.CancelledError:
                await self._kill_proc()
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
            await asyncio.sleep(3600)
            return

        self._proc = await asyncio.create_subprocess_exec(
            str(cf), "tunnel", "--url", f"http://localhost:{self._port}",
            "--no-autoupdate", "--loglevel", "error",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        last_stderr: list[str] = [""]

        async def _read_stream(stream: asyncio.StreamReader, tag: str) -> None:
            async for line_bytes in stream:
                line = line_bytes.decode(errors="replace").rstrip()
                if not line:
                    continue
                logger.debug("CLOUDFLARED %s: %s", tag, line)
                m = _URL_RE.search(line)
                if m and self._public_url is None:
                    self._public_url = m.group(0)
                    logger.info("cloudflare tunnel URL: %s", self._public_url)
                    asyncio.create_task(self._register_url(self._public_url))
                if tag == "STDERR":
                    last_stderr[0] = line

        await asyncio.gather(
            _read_stream(self._proc.stdout, "STDOUT"),
            _read_stream(self._proc.stderr, "STDERR"),
        )
        code = await self._proc.wait()
        self._proc = None

        if code != 0:
            logger.error(
                "TUNNEL_PUBLIC cloudflared exited (code %d)\n"
                "  Last stderr: %s",
                code, last_stderr[0],
            )

    async def _register_url(self, url: str) -> None:
        token = auth.get_session_token()
        if not token:
            return
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{self._base_url}/api/v1/proxy/register-url",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"public_url": f"{url}/v1"},
                )
                if resp.status_code < 300:
                    logger.info("registered public URL with KHG")
                else:
                    logger.warning("register-url returned HTTP %s", resp.status_code)
        except Exception as exc:
            logger.warning("failed to register public URL: %s", exc)
