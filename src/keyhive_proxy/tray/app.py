import asyncio
import logging
import sys
import threading
import tkinter as tk
from datetime import datetime, timezone
from tkinter import ttk
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from keyhive_proxy.key_manager import KeyManager

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
        key_manager: Optional["KeyManager"] = None,
        async_loop: Optional[asyncio.AbstractEventLoop] = None,
    ):
        self._config = config
        self._on_stop = on_stop
        self._on_restart = on_restart
        self._key_manager = key_manager
        self._async_loop = async_loop
        self._status = "starting"
        self._email: str = ""
        self._slots_active = 0
        self._slots_total = 0
        self._requests_today = 0
        self._public_url: Optional[str] = None
        self._icon: Optional["pystray.Icon"] = None

    def update_status(
        self,
        status: str,
        email: str = "",
        slots_active: int = 0,
        slots_total: int = 0,
        requests_today: int = 0,
        public_url: Optional[str] = None,
    ) -> None:
        self._status = status
        self._email = email
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

        from keyhive_proxy import auth as _auth

        signed_in = bool(_auth.get_session_token())

        label = {
            "running":  "Running",
            "degraded": "Degraded",
            "error":    "Error",
            "starting": "Starting...",
        }.get(self._status, self._status.title())

        if not signed_in:
            return pystray.Menu(
                pystray.MenuItem(f"keyhive-proxy  [Not connected]", None, enabled=False),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("⚠ Sign in to activate", None, enabled=False),
                pystray.MenuItem("Settings...", self._open_settings),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", self._quit),
            )

        base = self._base_url()

        return pystray.Menu(
            pystray.MenuItem(f"keyhive-proxy  [{label}]", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(self._email or "Signed in", None, enabled=False),
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
            pystray.MenuItem("⟳ Sync Keys", self._sync_keys),
            pystray.MenuItem("Copy Base URL", lambda *_: self._copy_base_url()),
            pystray.MenuItem("Open Logs folder", self._open_logs_folder),
            pystray.MenuItem("Settings...", self._open_settings),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Restart", lambda *_: self._on_restart()),
            pystray.MenuItem("Stop", lambda *_: self._on_stop()),
            pystray.MenuItem("Quit", self._quit),
        )

    def _copy_base_url(self) -> None:
        base = self._base_url()
        self._copy(f"{base}/v1")

    def _sync_keys(self, *_) -> None:
        if not self._key_manager or not self._async_loop:
            return
        asyncio.run_coroutine_threadsafe(
            self._key_manager.sync_all(), self._async_loop
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
            target=lambda: SettingsWindow(
                self._config,
                key_manager=self._key_manager,
                async_loop=self._async_loop,
            ).show(),
            daemon=True,
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
    def __init__(
        self,
        config: dict,
        key_manager: Optional["KeyManager"] = None,
        async_loop: Optional[asyncio.AbstractEventLoop] = None,
    ):
        self._config = config
        self._key_manager = key_manager
        self._async_loop = async_loop

    def show(self) -> None:
        from keyhive_proxy import auth as _auth
        from keyhive_proxy.config import save_config

        root = tk.Tk()
        root.title("keyhive-proxy Settings")
        root.resizable(False, False)

        frame = ttk.Frame(root, padding=16)
        frame.grid(sticky="nsew")

        row = 0

        # ── Account ──────────────────────────────────────────────────────
        ttk.Separator(frame, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=(0, 4)
        )
        row += 1
        ttk.Label(frame, text="Account", font=("", 9, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w"
        )
        row += 1

        email = _auth.get_email() or "Not signed in"
        ttk.Label(frame, text=f"● Connected as {email}").grid(
            row=row, column=0, columnspan=2, sticky="w", pady=2
        )
        row += 1

        account_btn_frame = ttk.Frame(frame)
        account_btn_frame.grid(row=row, column=0, columnspan=2, sticky="w", pady=2)
        row += 1

        # Sign Out button
        def sign_out() -> None:
            _auth.clear_session()
            root.destroy()
            from keyhive_proxy.login_window import LoginWindow

            def on_auth(em, tok):
                _auth.set_session_token(em, tok)

            LoginWindow(self._config, on_authenticated=on_auth).show()

        ttk.Button(account_btn_frame, text="Sign Out", command=sign_out).pack(side="left", padx=(0, 6))

        # Sync Keys button
        sync_status_var = tk.StringVar()

        def sync_keys() -> None:
            if not self._key_manager or not self._async_loop:
                sync_status_var.set("Key manager not available")
                return
            sync_btn.config(state="disabled")
            sync_status_var.set("Syncing...")

            def _do():
                future = asyncio.run_coroutine_threadsafe(
                    self._key_manager.sync_all(), self._async_loop
                )
                try:
                    future.result(timeout=30)
                    root.after(0, lambda: _done(ok=True))
                except Exception:
                    root.after(0, lambda: _done(ok=False))

            def _done(ok: bool) -> None:
                sync_btn.config(state="normal")
                if ok:
                    ts = self._key_manager.last_sync_at
                    sync_status_var.set("✓ Keys synced")
                    root.after(3000, lambda: _update_sync_time(ts))
                else:
                    sync_status_var.set("✗ Sync failed")
                    root.after(3000, lambda: sync_status_var.set(""))

            def _update_sync_time(ts) -> None:
                if ts:
                    delta = (datetime.now(timezone.utc) - ts).seconds // 60
                    sync_status_var.set(f"Last sync: {delta} min ago" if delta > 0 else "Last sync: just now")
                else:
                    sync_status_var.set("")

            threading.Thread(target=_do, daemon=True).start()

        sync_btn = ttk.Button(account_btn_frame, text="⟳ Sync Keys", command=sync_keys)
        sync_btn.pack(side="left")

        # Last sync label
        if self._key_manager and self._key_manager.last_sync_at:
            delta = (datetime.now(timezone.utc) - self._key_manager.last_sync_at).seconds // 60
            sync_status_var.set(f"Last sync: {delta} min ago" if delta > 0 else "Last sync: just now")

        ttk.Label(frame, textvariable=sync_status_var, foreground="gray").grid(
            row=row, column=0, columnspan=2, sticky="w"
        )
        row += 1

        # ── Proxy ─────────────────────────────────────────────────────────
        ttk.Separator(frame, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=(8, 4)
        )
        row += 1
        ttk.Label(frame, text="Proxy", font=("", 9, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w"
        )
        row += 1

        ttk.Label(frame, text="KHG Server URL").grid(row=row, column=0, sticky="w", pady=3)
        url_var = tk.StringVar(value=str(self._config.get("khg_base_url", "")))
        ttk.Entry(frame, textvariable=url_var, width=42).grid(row=row, column=1, padx=6)
        row += 1

        ttk.Label(frame, text="Listen Port").grid(row=row, column=0, sticky="w", pady=3)
        port_var = tk.IntVar(value=int(self._config.get("listen_port", 8080)))
        ttk.Spinbox(frame, from_=1024, to=65535, textvariable=port_var, width=8).grid(
            row=row, column=1, sticky="w", padx=6
        )
        row += 1

        ttk.Label(frame, text="Log retention (days)").grid(row=row, column=0, sticky="w", pady=3)
        retention_var = tk.IntVar(value=int(self._config.get("log_retention_days", 30)))
        ttk.Spinbox(frame, from_=1, to=365, textvariable=retention_var, width=8).grid(
            row=row, column=1, sticky="w", padx=6
        )
        row += 1

        autostart_var = tk.BooleanVar(value=bool(self._config.get("autostart", False)))
        ttk.Checkbutton(frame, text="Start on login", variable=autostart_var).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=4
        )
        row += 1

        # ── Buttons ───────────────────────────────────────────────────────
        ttk.Separator(frame, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=(4, 8)
        )
        row += 1

        def save() -> None:
            self._config["khg_base_url"] = url_var.get().strip()
            self._config["listen_port"] = int(port_var.get())
            self._config["log_retention_days"] = int(retention_var.get())
            self._config["autostart"] = autostart_var.get()
            save_config(self._config)
            _configure_autostart(autostart_var.get())
            root.destroy()

        btn = ttk.Frame(frame)
        btn.grid(row=row, column=0, columnspan=2, pady=8)
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
