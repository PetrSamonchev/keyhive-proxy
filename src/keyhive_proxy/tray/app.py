import logging
import sys
import threading
import tkinter as tk
from tkinter import ttk
from typing import Callable

logger = logging.getLogger(__name__)

try:
    import pystray
    from PIL import Image, ImageDraw
    _PYSTRAY = True
except ImportError:
    _PYSTRAY = False
    logger.warning("pystray/Pillow not available — tray icon disabled")


_STATUS_COLORS = {
    "running":  "#22c55e",
    "degraded": "#eab308",
    "error":    "#ef4444",
    "starting": "#94a3b8",
}


def _draw_icon(color: str, size: int = 64) -> "Image.Image":
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    margin = size // 8
    draw.ellipse([margin, margin, size - margin, size - margin], fill=color)
    return img


class TrayApp:
    def __init__(
        self,
        config: dict,
        on_stop: Callable[[], None],
        on_restart: Callable[[], None],
    ):
        self._config = config
        self._on_stop = on_stop
        self._on_restart = on_restart
        self._status = "starting"
        self._slots_active = 0
        self._slots_total = 0
        self._requests_today = 0
        self._public_url: str | None = None
        self._icon: "pystray.Icon | None" = None

    def update_status(
        self,
        status: str,
        slots_active: int = 0,
        slots_total: int = 0,
        requests_today: int = 0,
        public_url: str | None = None,
    ) -> None:
        self._status = status
        self._slots_active = slots_active
        self._slots_total = slots_total
        self._requests_today = requests_today
        self._public_url = public_url
        if self._icon:
            color = _STATUS_COLORS.get(status, "#94a3b8")
            self._icon.icon = _draw_icon(color)
            self._icon.menu = self._build_menu()

    def _base_url(self) -> str:
        port = self._config.get("listen_port", 8080)
        return self._public_url or f"http://localhost:{port}"

    def _build_menu(self) -> "pystray.Menu | None":
        if not _PYSTRAY:
            return None

        label = {
            "running":  "Running",
            "degraded": "Degraded",
            "error":    "Error",
            "starting": "Starting...",
        }.get(self._status, self._status.title())

        base = self._base_url()

        return pystray.Menu(
            pystray.MenuItem(f"keyhive-proxy  [{label}]", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                f"Slots: {self._slots_active}/{self._slots_total} active", None, enabled=False
            ),
            pystray.MenuItem(
                f"Requests today: {self._requests_today}", None, enabled=False
            ),
            pystray.MenuItem(
                f"Public URL: {self._public_url or 'unavailable'}",
                lambda *_: self._copy(f"{base}/v1"),
                enabled=self._public_url is not None,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Copy Base URL", lambda *_: self._copy(f"{base}/v1")),
            pystray.MenuItem("Open Logs folder", self._open_logs_folder),
            pystray.MenuItem("Settings...", self._open_settings),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Restart", lambda *_: self._on_restart()),
            pystray.MenuItem("Stop", lambda *_: self._on_stop()),
            pystray.MenuItem("Quit", self._quit),
        )

    def _copy(self, text: str) -> None:
        try:
            root = tk.Tk()
            root.withdraw()
            root.clipboard_clear()
            root.clipboard_append(text)
            root.update()
            root.after(500, root.destroy)
            root.mainloop()
        except Exception as exc:
            logger.warning("clipboard copy failed: %s", exc)

    def _open_logs_folder(self, *_) -> None:
        import subprocess
        from keyhive_proxy.config import get_data_dir
        folder = str(get_data_dir())
        if sys.platform == "win32":
            subprocess.Popen(["explorer", folder])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", folder])
        else:
            subprocess.Popen(["xdg-open", folder])

    def _open_settings(self, *_) -> None:
        t = threading.Thread(
            target=lambda: SettingsWindow(self._config).show(), daemon=True
        )
        t.start()

    def _quit(self, *_) -> None:
        if self._icon:
            self._icon.stop()
        self._on_stop()

    def run(self) -> None:
        if not _PYSTRAY:
            logger.info("running without tray icon (pystray not available) — press Ctrl+C to stop")
            try:
                threading.Event().wait()
            except KeyboardInterrupt:
                self._on_stop()
            return
        icon_img = _draw_icon(_STATUS_COLORS["starting"])
        self._icon = pystray.Icon(
            "keyhive-proxy",
            icon_img,
            "keyhive-proxy",
            menu=self._build_menu(),
        )
        try:
            self._icon.run()
        except Exception as exc:
            logger.error("tray icon failed: %s — proxy keeps running, use CLI to manage", exc)
            try:
                threading.Event().wait()
            except KeyboardInterrupt:
                self._on_stop()


class SettingsWindow:
    def __init__(self, config: dict):
        self._config = config

    def show(self) -> None:
        from keyhive_proxy.config import save_config

        root = tk.Tk()
        root.title("keyhive-proxy Settings")
        root.resizable(False, False)

        frame = ttk.Frame(root, padding=16)
        frame.grid(sticky="nsew")

        fields = [
            ("KHG API Key", "khg_api_key", "entry_secret"),
            ("KHG Server URL", "khg_base_url", "entry"),
            ("Listen Port", "listen_port", "spinbox_port"),
            ("Log retention (days)", "log_retention_days", "spinbox_days"),
        ]
        widgets: dict[str, tk.Variable] = {}
        for row, (label, key, kind) in enumerate(fields):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=3)
            if kind == "entry_secret":
                var = tk.StringVar(value=str(self._config.get(key, "")))
                ttk.Entry(frame, textvariable=var, show="*", width=42).grid(
                    row=row, column=1, padx=6
                )
            elif kind == "entry":
                var = tk.StringVar(value=str(self._config.get(key, "")))
                ttk.Entry(frame, textvariable=var, width=42).grid(row=row, column=1, padx=6)
            elif kind == "spinbox_port":
                var = tk.IntVar(value=int(self._config.get(key, 8080)))
                ttk.Spinbox(frame, from_=1024, to=65535, textvariable=var, width=8).grid(
                    row=row, column=1, sticky="w", padx=6
                )
            elif kind == "spinbox_days":
                var = tk.IntVar(value=int(self._config.get(key, 30)))
                ttk.Spinbox(frame, from_=1, to=365, textvariable=var, width=8).grid(
                    row=row, column=1, sticky="w", padx=6
                )
            widgets[key] = var

        autostart_var = tk.BooleanVar(value=bool(self._config.get("autostart", False)))
        ttk.Checkbutton(frame, text="Start on login", variable=autostart_var).grid(
            row=len(fields), column=0, columnspan=2, sticky="w", pady=4
        )

        def save() -> None:
            self._config["khg_api_key"] = widgets["khg_api_key"].get().strip()
            self._config["khg_base_url"] = widgets["khg_base_url"].get().strip()
            self._config["listen_port"] = int(widgets["listen_port"].get())
            self._config["log_retention_days"] = int(widgets["log_retention_days"].get())
            self._config["autostart"] = autostart_var.get()
            save_config(self._config)
            _configure_autostart(autostart_var.get())
            root.destroy()

        btn = ttk.Frame(frame)
        btn.grid(row=len(fields) + 1, column=0, columnspan=2, pady=8)
        ttk.Button(btn, text="Save", command=save).pack(side="left", padx=4)
        ttk.Button(btn, text="Cancel", command=root.destroy).pack(side="left")

        root.mainloop()


def _configure_autostart(enabled: bool) -> None:
    try:
        if sys.platform == "win32":
            import winreg
            reg_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, reg_path, 0, winreg.KEY_SET_VALUE
            ) as key:
                if enabled:
                    winreg.SetValueEx(key, "keyhive-proxy", 0, winreg.REG_SZ, sys.executable)
                else:
                    try:
                        winreg.DeleteValue(key, "keyhive-proxy")
                    except FileNotFoundError:
                        pass

        elif sys.platform == "darwin":
            from pathlib import Path
            plist = Path.home() / "Library" / "LaunchAgents" / "com.keyhivegarden.proxy.plist"
            if enabled:
                plist.write_text(
                    f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.keyhivegarden.proxy</string>
  <key>ProgramArguments</key>
  <array>
    <string>{sys.executable}</string>
    <string>start</string>
  </array>
  <key>RunAtLoad</key><true/>
</dict>
</plist>"""
                )
            else:
                plist.unlink(missing_ok=True)
    except Exception as exc:
        logger.warning("autostart configuration failed: %s", exc)
