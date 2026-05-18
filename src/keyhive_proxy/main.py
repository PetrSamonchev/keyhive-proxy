"""keyhive-proxy — entry point and CLI."""
import asyncio
import logging
import os
import signal
import sys
import threading

import click

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


async def _run_proxy(
    config: dict,
    stop_event: asyncio.Event,
    tray_app=None,
) -> None:
    from keyhive_proxy.log_store import LogStore
    from keyhive_proxy.key_manager import KeyManager
    from keyhive_proxy.server import ProxyServer
    from keyhive_proxy.status_reporter import StatusReporter
    from keyhive_proxy.tunnel_public import PublicTunnel
    from keyhive_proxy.tunnel_khg import KHGTunnel

    log_store = LogStore()
    await log_store.init()
    await log_store.purge_old(config.get("log_retention_days", 30))

    key_manager = KeyManager(
        khg_base_url=config["khg_base_url"],
        log_store=log_store,
    )
    if tray_app is not None:
        tray_app._key_manager = key_manager

    status_reporter = StatusReporter(
        khg_base_url=config["khg_base_url"],
        log_store=log_store,
        key_manager=key_manager,
    )
    proxy_server = ProxyServer(
        key_manager=key_manager,
        status_reporter=status_reporter,
        log_store=log_store,
        port=config.get("listen_port", 8080),
    )
    tunnel_public = PublicTunnel(
        port=config.get("listen_port", 8080),
        khg_base_url=config["khg_base_url"],
    )
    tunnel_khg = KHGTunnel(
        khg_base_url=config["khg_base_url"],
        log_store=log_store,
        key_manager=key_manager,
    )

    try:
        await key_manager.start()
        await status_reporter.start()
        await proxy_server.start()
        await tunnel_public.start()
        await tunnel_khg.start()
        logger.info("keyhive-proxy running on port %d", config.get("listen_port", 8080))
        await stop_event.wait()
    finally:
        await tunnel_khg.stop()
        await tunnel_public.stop()
        await proxy_server.stop()
        await status_reporter.stop()
        await key_manager.stop()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.group()
def cli() -> None:
    """keyhive-proxy — local AI privacy proxy."""


@cli.command()
@click.option("--no-tray", is_flag=True, help="Run headless (no tray icon)")
def start(no_tray: bool) -> None:
    """Start the proxy (with tray icon by default)."""
    from keyhive_proxy.config import load_config
    from keyhive_proxy import auth

    config = load_config()
    base_url = config.get("khg_base_url", "https://keyhivegarden.com")

    # Restore saved session and validate it against the server
    saved = auth.load_session()
    if saved:
        email, token = saved
        async def _validate():
            return await auth.validate_token(base_url, token)
        try:
            asyncio.run(_validate())
            auth.set_session_token(email, token)
        except Exception:
            auth.clear_session()

    if not auth.get_session_token():
        if no_tray:
            click.echo(
                "ERROR: Not signed in.\n"
                "Run keyhive-proxy without --no-tray to sign in via the tray icon.",
                err=True,
            )
            sys.exit(1)

        from keyhive_proxy.login_window import LoginWindow

        def on_auth(em: str, tok: str) -> None:
            auth.set_session_token(em, tok)

        LoginWindow(config, on_authenticated=on_auth).show()
        config = load_config()  # reload in case base_url changed

        if not auth.get_session_token():
            sys.exit(0)

    stop_event = asyncio.Event()

    if no_tray:
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop_event.set())
        asyncio.run(_run_proxy(config, stop_event))
        return

    # Tray mode: async loop in background thread, tray in main thread.
    loop = asyncio.new_event_loop()

    from keyhive_proxy.tray.app import TrayApp

    def _stop() -> None:
        loop.call_soon_threadsafe(stop_event.set)

    def _restart() -> None:
        loop.call_soon_threadsafe(stop_event.set)
        os.execv(sys.executable, [sys.executable] + sys.argv)

    # tray is referenced by _async_thread_main closure; defined before thread starts
    tray = TrayApp(config=config, on_stop=_stop, on_restart=_restart, async_loop=loop)

    def _async_thread_main() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_run_proxy(config, stop_event, tray_app=tray))

    async_thread = threading.Thread(target=_async_thread_main, daemon=True)
    async_thread.start()

    tray.run()  # blocks until tray quits

    loop.call_soon_threadsafe(stop_event.set)
    async_thread.join(timeout=10)


@cli.command()
def stop() -> None:
    """Send stop signal to running instance."""
    click.echo("Send SIGTERM to the running keyhive-proxy process.")


@cli.command()
def status() -> None:
    """Show whether proxy is running and its public URL."""
    from keyhive_proxy.config import load_config
    import httpx

    config = load_config()
    port = config.get("listen_port", 8080)
    try:
        resp = httpx.get(f"http://localhost:{port}/health", timeout=3.0)
        data = resp.json()
        click.echo(
            f"Running  |  status={data.get('status')}  "
            f"slots_active={data.get('slots_active', 0)}"
        )
    except Exception:
        click.echo(f"Stopped (no response on port {port})")


@cli.command("config")
@click.argument("action")
@click.option("--port", default=None, type=int, help="Listen port")
@click.option("--url", default=None, help="KHG base URL")
def config_cmd(action: str, port: int | None, url: str | None) -> None:
    """Configure keyhive-proxy settings."""
    from keyhive_proxy.config import load_config, save_config

    if action != "set":
        click.echo(f"Unknown action '{action}'. Available: set", err=True)
        return

    cfg = load_config()
    if port is not None:
        cfg["listen_port"] = port
    if url is not None:
        cfg["khg_base_url"] = url
    save_config(cfg)
    click.echo("Config saved.")


@cli.command("logs")
@click.option("--limit", default=50, show_default=True, help="Number of records")
def logs_cmd(limit: int) -> None:
    """Tail local request logs."""
    from keyhive_proxy.log_store import LogStore

    async def _show() -> None:
        store = LogStore()
        rows = await store.get_logs("1970-01-01T00:00:00", limit)
        if not rows:
            click.echo("No logs found.")
            return
        for row in rows:
            click.echo(
                f"{str(row.get('ts', ''))[:19]}  "
                f"{str(row.get('provider', '-')):12}  "
                f"{str(row.get('model', '-')):32}  "
                f"{str(row.get('outcome', '-')):15}  "
                f"{row.get('latency_ms', '-')}ms"
            )

    asyncio.run(_show())


if __name__ == "__main__":
    cli()
