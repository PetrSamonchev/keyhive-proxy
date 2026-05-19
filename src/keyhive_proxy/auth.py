"""Session management and KHG authentication."""
import logging
import sys
from typing import Optional

import httpx
import keyring

from keyhive_proxy.http_client import khg_headers

logger = logging.getLogger(__name__)

_SERVICE = "keyhive-proxy"

# Module-level session state — all components read from here.
_current_session_token: Optional[str] = None
_current_email: Optional[str] = None
_proxy_id: str = ""

# Optional callback invoked when a 401 is detected mid-operation.
_session_expired_callback = None


class AuthError(Exception):
    pass


class ProxyRegisterError(Exception):
    pass


# ---------------------------------------------------------------------------
# In-memory state accessors
# ---------------------------------------------------------------------------

def get_session_token() -> Optional[str]:
    return _current_session_token


def get_email() -> Optional[str]:
    return _current_email


def get_proxy_id() -> str:
    return _proxy_id


def set_session_token(email: str, token: str) -> None:
    global _current_session_token, _current_email
    _current_session_token = token
    _current_email = email


def set_proxy_id(proxy_id: str) -> None:
    global _proxy_id
    _proxy_id = proxy_id


def set_session_expired_callback(cb) -> None:
    global _session_expired_callback
    _session_expired_callback = cb


def notify_session_expired() -> None:
    """Called by components when they receive HTTP 401 from KHG."""
    clear_session()
    logger.warning("KHG session expired — proxy returning 503 until re-login")
    if _session_expired_callback:
        try:
            _session_expired_callback()
        except Exception as exc:
            logger.warning("session_expired_callback failed: %s", exc)


# ---------------------------------------------------------------------------
# Async KHG API calls
# ---------------------------------------------------------------------------

async def login_email(base_url: str, email: str, password: str) -> dict:
    """Authenticate with KHG. Returns {"session_token": ..., "email": ...}."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{base_url.rstrip('/')}/auth/login",
                json={"email": email, "password": password},
            )
        if resp.status_code == 401:
            raise AuthError("Invalid email or password")
        resp.raise_for_status()
        data = resp.json()
        token = data.get("access_token") or data.get("session_token", "")
        if not token:
            raise AuthError("Server returned no token")
        return {
            "session_token": token,
            "email": data.get("email", email),
        }
    except AuthError:
        raise
    except Exception as exc:
        raise AuthError(f"Connection failed: {exc}") from exc


async def validate_token(base_url: str, session_token: str) -> dict:
    """Validate a session token. Returns {"email": ...}. Raises AuthError on 401."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{base_url.rstrip('/')}/api/v1/proxy/app-tokens",
                headers=khg_headers(session_token, _proxy_id),
            )
        if resp.status_code == 401:
            raise AuthError("Session expired or invalid")
        resp.raise_for_status()
        return {"email": _current_email or ""}
    except AuthError:
        raise
    except Exception as exc:
        raise AuthError(f"Connection failed: {exc}") from exc


async def register_proxy(
    base_url: str,
    session_token: str,
    proxy_id: str,
    proxy_name: str,
    version: str,
) -> None:
    """Register or update this proxy instance with KHG."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{base_url.rstrip('/')}/api/v1/proxy/register",
                headers=khg_headers(session_token, proxy_id),
                json={
                    "proxy_id": proxy_id,
                    "proxy_name": proxy_name,
                    "platform": sys.platform,
                    "version": version,
                },
            )
        if not resp.is_success:
            raise ProxyRegisterError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        logger.info("proxy registered with KHG (proxy_id=%s, name=%s)", proxy_id, proxy_name)
    except ProxyRegisterError:
        raise
    except Exception as exc:
        raise ProxyRegisterError(f"Connection failed: {exc}") from exc


async def rename_proxy(
    base_url: str,
    session_token: str,
    proxy_id: str,
    proxy_name: str,
) -> None:
    """Rename this proxy instance on KHG."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.patch(
                f"{base_url.rstrip('/')}/api/v1/proxy/register",
                headers=khg_headers(session_token, proxy_id),
                json={"proxy_id": proxy_id, "proxy_name": proxy_name},
            )
        if not resp.is_success:
            raise ProxyRegisterError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        logger.info("proxy renamed on KHG (proxy_id=%s, name=%s)", proxy_id, proxy_name)
    except ProxyRegisterError:
        raise
    except Exception as exc:
        raise ProxyRegisterError(f"Connection failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Persistent session (keyring + config)
# ---------------------------------------------------------------------------

def save_session(email: str, session_token: str) -> None:
    from keyhive_proxy.config import load_config, save_config
    keyring.set_password(_SERVICE, email, session_token)
    cfg = load_config()
    cfg["last_email"] = email
    save_config(cfg)
    set_session_token(email, session_token)


def load_session() -> Optional[tuple[str, str]]:
    from keyhive_proxy.config import load_config
    cfg = load_config()
    email = cfg.get("last_email", "")
    if not email:
        return None
    token = keyring.get_password(_SERVICE, email)
    return (email, token) if token else None


def clear_session() -> None:
    global _current_session_token, _current_email
    from keyhive_proxy.config import load_config, save_config
    cfg = load_config()
    email = cfg.get("last_email", "")
    if email:
        try:
            keyring.delete_password(_SERVICE, email)
        except Exception:
            pass
    cfg["last_email"] = ""
    save_config(cfg)
    _current_session_token = None
    _current_email = None
