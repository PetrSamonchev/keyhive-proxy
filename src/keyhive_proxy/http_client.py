"""Shared HTTP helper for all outbound KHG API calls."""


def khg_headers(session_token: str, proxy_id: str) -> dict:
    """Build standard headers for every request to KHG."""
    return {
        "Authorization": f"Bearer {session_token}",
        "X-Proxy-ID": proxy_id,
        "Content-Type": "application/json",
    }
