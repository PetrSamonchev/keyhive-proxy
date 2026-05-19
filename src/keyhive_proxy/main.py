"""keyhive-proxy — entry point and CLI."""
import asyncio
import logging
import os
import platform
import signal
import sys
import threading

import click

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def get_version() -> str:
    try:
        from importlib.metadata import version
        return version("keyhive-proxy")
    except Exception:
        return "0.1.0"


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
    from keyhive_proxy import auth

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

    # Register proxy with KHG before starting components.
    # Failure must not prevent proxy from starting.
    token = auth.get_session_token()
    if token:
        try:
            await auth.register_proxy(
                base_url=config["khg_base_url"],
                session_token=token,
                proxy_id=config["proxy_id"],
                proxy_name=config["proxy_name"],
                version=get_version(),
            )
        except Exception as exc:
            logger.warning("proxy registration failed (non-fatal): %s", exc)

    try:
        await key_manager.start()
        await status_reporter.start()
        await proxy_server.start()
        await tunnel_public.start()
        await tunnel_khg.start()
        logger.info("keyhive-proxy running on port %d", config.get("listen_port", 8080))
        logger.info(
            "STARTUP SUMMARY\n"
            "  proxy_id:    %s\n"
            "  proxy_name:  %s\n"
            "  platform:    %s\n"
            "  listen_port: %d\n"
            "  khg_url:     %s\n"
            "  app_tokens:  %d synced\n"
            "  public_url:  %s\n"
            "  tunnel:      connecting (see TUNNEL log lines)",
            config.get("proxy_id", "unset"),
            config.get("proxy_name", platform.node()),
            sys.platform,
            config.get("listen_port", 8080),
            config["khg_base_url"],
            key_manager.token_count(),
            tunnel_public.public_url or "unavailable",
        )
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

    # Set proxy_id module state early so all outbound KHG calls include X-Proxy-ID.
    auth.set_proxy_id(config["proxy_id"])

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
    """Configure keyhive-proxy settings (actions: set, show)."""
    from keyhive_proxy.config import load_config, save_config

    cfg = load_config()

    if action == "show":
        from keyhive_proxy import auth as _auth
        from keyhive_proxy.tunnel_public import _find_cloudflared

        saved = _auth.load_session()
        if saved:
            em, _ = saved
            session_info = f"yes ({em})"
        else:
            session_info = "no"

        cf = _find_cloudflared()
        cf_info = str(cf) if cf else "not found"

        click.echo(f"proxy_id:            {cfg.get('proxy_id', 'unset')}")
        click.echo(f"proxy_name:          {cfg.get('proxy_name', platform.node())}")
        click.echo(f"platform:            {sys.platform}")
        click.echo(f"listen_port:         {cfg.get('listen_port', 8080)}")
        click.echo(f"khg_base_url:        {cfg.get('khg_base_url', 'unset')}")
        click.echo(f"log_retention_days:  {cfg.get('log_retention_days', 30)}")
        click.echo(f"autostart:           {'true' if cfg.get('autostart') else 'false'}")
        click.echo(f"session_stored:      {session_info}")
        click.echo(f"cloudflared:         {cf_info}")
        return

    if action == "set":
        if port is not None:
            cfg["listen_port"] = port
        if url is not None:
            cfg["khg_base_url"] = url
        save_config(cfg)
        click.echo("Config saved.")
        return

    click.echo(f"Unknown action '{action}'. Available: set, show", err=True)


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


@cli.command("tunnel-check")
def tunnel_check() -> None:
    """Run pre-flight diagnostics for the KHG WebSocket tunnel."""
    from keyhive_proxy.config import load_config
    from keyhive_proxy import auth as _auth
    from keyhive_proxy.tunnel_khg import run_preflight

    cfg = load_config()
    base_url = cfg.get("khg_base_url", "https://keyhivegarden.com")

    saved = _auth.load_session()
    if saved:
        em, tok = saved
        _auth.set_session_token(em, tok)

    result = asyncio.run(run_preflight(base_url))

    ok = "✓"
    fail = "✗"

    host_sym = ok if result["host_reachable"] else fail
    auth_sym = ok if result["auth_valid"] else fail
    ep_sym   = ok if result["endpoint_exists"] else fail

    http_info = f"HTTP {result['http_status']}" if result["http_status"] else (result.get("error") or "")
    auth_info = "valid" if result["auth_valid"] else "failed"
    if result["auth_valid"] and saved:
        auth_info = f"valid ({saved[0]})"

    click.echo(f"  {host_sym} Host reachable:      {base_url}")
    click.echo(f"  {auth_sym} Authentication:      {auth_info}")
    click.echo(f"  {ep_sym} WebSocket endpoint:  {http_info}")

    if result.get("response_body"):
        click.echo(f"    Response body: {result['response_body']}")

    if not result["host_reachable"]:
        click.echo(f"\n  Error:  {result.get('error', 'unknown')}")
        click.echo("  Action: Check network connection and khg_base_url in settings")
        sys.exit(1)

    if not result["auth_valid"]:
        click.echo("\n  Action: Sign out and sign in again")
        sys.exit(1)

    status = result.get("http_status")
    if status == 404:
        click.echo("\n  Likely cause: WebSocket endpoint not found (server version outdated)")
        click.echo("  Action: Check KHG server deployment logs")
        sys.exit(1)
    elif status == 502:
        click.echo("\n  Likely cause: /ws/proxy-tunnel endpoint not deployed on server")
        click.echo("  Action: Check KHG server deployment logs")
        sys.exit(1)

    if result["host_reachable"] and result["auth_valid"]:
        click.echo("\n  All checks passed.")
        sys.exit(0)

    sys.exit(1)


if __name__ == "__main__":
    cli()
