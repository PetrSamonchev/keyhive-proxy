"""Login window shown when no valid session exists."""
import asyncio
import logging
import socket
import threading
import tkinter as tk
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from tkinter import ttk
from typing import Callable
from urllib.parse import parse_qs, urlparse

from keyhive_proxy import auth

logger = logging.getLogger(__name__)


class LoginWindow:
    def __init__(self, config: dict, on_authenticated: Callable[[str, str], None]):
        self._config = config
        self._on_authenticated = on_authenticated
        self._root: tk.Tk | None = None

    def show(self) -> None:
        self._root = tk.Tk()
        self._root.title("Sign in to KeyHive Garden")
        self._root.resizable(False, False)
        self._root.protocol("WM_DELETE_WINDOW", self._on_close)

        frame = ttk.Frame(self._root, padding=20)
        frame.grid(sticky="nsew")

        # KHG Server URL
        ttk.Label(frame, text="KHG Server URL").grid(row=0, column=0, sticky="w", pady=4)
        self._url_var = tk.StringVar(value=self._config.get("khg_base_url", "https://keyhivegarden.com"))
        ttk.Entry(frame, textvariable=self._url_var, width=44).grid(row=0, column=1, padx=6)

        # Email
        ttk.Label(frame, text="Email").grid(row=1, column=0, sticky="w", pady=4)
        self._email_var = tk.StringVar(value=self._config.get("last_email", ""))
        ttk.Entry(frame, textvariable=self._email_var, width=44).grid(row=1, column=1, padx=6)

        # Password
        ttk.Label(frame, text="Password").grid(row=2, column=0, sticky="w", pady=4)
        self._pass_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self._pass_var, show="*", width=44).grid(row=2, column=1, padx=6)

        # Buttons
        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=3, column=0, columnspan=2, pady=10, sticky="w")
        self._sign_in_btn = ttk.Button(btn_frame, text="Sign In", command=self._on_sign_in_click)
        self._sign_in_btn.pack(side="left", padx=(0, 8))
        ttk.Button(btn_frame, text="Sign in with Google", command=self._on_google_click).pack(side="left")

        # Status label
        self._status_var = tk.StringVar()
        self._status_label = ttk.Label(frame, textvariable=self._status_var, foreground="red")
        self._status_label.grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))

        self._root.mainloop()

    def _on_close(self) -> None:
        import sys
        self._root.destroy()
        sys.exit(0)

    def _on_sign_in_click(self) -> None:
        email = self._email_var.get().strip()
        password = self._pass_var.get()
        base_url = self._url_var.get().strip()
        if not email or not password:
            self._status_var.set("Email and password are required.")
            return
        self._sign_in_btn.config(state="disabled")
        self._status_var.set("Signing in...")
        self._status_label.config(foreground="gray")
        threading.Thread(
            target=self._do_login,
            args=(base_url, email, password),
            daemon=True,
        ).start()

    def _do_login(self, base_url: str, email: str, password: str) -> None:
        try:
            result = asyncio.run(auth.login_email(base_url, email, password))
            self._root.after(0, lambda: self._handle_success(email, result["session_token"], base_url))
        except auth.AuthError as exc:
            self._root.after(0, lambda: self._show_error(str(exc)))

    def _on_google_click(self) -> None:
        base_url = self._url_var.get().strip()
        self._sign_in_btn.config(state="disabled")
        self._status_var.set("Opening browser...")
        self._status_label.config(foreground="gray")
        threading.Thread(target=self._do_google_oauth, args=(base_url,), daemon=True).start()

    def _do_google_oauth(self, base_url: str) -> None:
        # Find a free port for the local callback server.
        with socket.socket() as s:
            s.bind(("", 0))
            port = s.getsockname()[1]

        received: dict = {}
        server_ready = threading.Event()

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                qs = parse_qs(urlparse(self.path).query)
                token = qs.get("token", [None])[0]
                if token:
                    received["token"] = token
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<h2>Signed in! You may close this tab.</h2>")

            def log_message(self, *_):
                pass

        httpd = HTTPServer(("localhost", port), _Handler)
        httpd.timeout = 2

        callback_url = f"http://localhost:{port}/callback"
        oauth_url = (
            f"{base_url.rstrip('/')}/auth/google"
            f"?redirect_uri={callback_url}&client=proxy"
        )

        self._root.after(0, lambda: webbrowser.open(oauth_url))

        deadline = 120
        elapsed = 0
        while elapsed < deadline and "token" not in received:
            httpd.handle_request()
            elapsed += 2

        httpd.server_close()

        if "token" not in received:
            self._root.after(0, lambda: self._show_error("Google sign-in timed out"))
            return

        email = auth.get_email() or ""
        self._root.after(0, lambda: self._handle_success(email, received["token"], base_url))

    def _handle_success(self, email: str, token: str, base_url: str) -> None:
        from keyhive_proxy.config import save_config
        from keyhive_proxy.remember_dialog import RememberDialog

        if base_url != self._config.get("khg_base_url"):
            self._config["khg_base_url"] = base_url
            save_config(self._config)

        RememberDialog(email, token).show()

        self._on_authenticated(email, auth.get_session_token() or token)
        self._root.destroy()

    def _show_error(self, message: str) -> None:
        self._status_var.set(message)
        self._status_label.config(foreground="red")
        self._sign_in_btn.config(state="normal")
