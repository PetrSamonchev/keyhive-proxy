import json
import os
import platform
import socket
import uuid
from pathlib import Path

_CONFIG_FILENAME = "config.json"
_DB_FILENAME = "logs.db"

_DEFAULTS: dict = {
    "listen_port": 8080,
    "log_retention_days": 30,
    "autostart": False,
    "khg_base_url": "https://keyhivegarden.com",
    "last_email": "",
}


def get_data_dir() -> Path:
    system = platform.system()
    if system == "Windows":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif system == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    data_dir = base / "keyhive-proxy"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def get_db_path() -> Path:
    system = platform.system()
    if system == "Linux":
        xdg_data = os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
        db_dir = Path(xdg_data) / "keyhive-proxy"
    else:
        db_dir = get_data_dir()
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / _DB_FILENAME


def load_config() -> dict:
    config_path = get_data_dir() / _CONFIG_FILENAME
    if not config_path.exists():
        cfg = dict(_DEFAULTS)
    else:
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            cfg = {**_DEFAULTS, **data}
        except Exception:
            cfg = dict(_DEFAULTS)

    changed = False

    # Generate proxy_id once — never overwrite an existing value.
    if not cfg.get("proxy_id"):
        cfg["proxy_id"] = str(uuid.uuid4())
        changed = True

    # Default proxy_name to hostname if missing.
    if not cfg.get("proxy_name"):
        cfg["proxy_name"] = socket.gethostname()
        changed = True

    if changed:
        save_config(cfg)

    return cfg


def save_config(config: dict) -> None:
    config_path = get_data_dir() / _CONFIG_FILENAME
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
