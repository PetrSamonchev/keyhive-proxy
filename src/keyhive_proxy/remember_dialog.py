"""Dialog shown after successful login — asks whether to persist session."""
import tkinter as tk
from tkinter import ttk

from keyhive_proxy import auth


class RememberDialog:
    def __init__(self, email: str, token: str):
        self._email = email
        self._token = token

    def show(self) -> None:
        root = tk.Tk()
        root.title("Remember credentials?")
        root.resizable(False, False)

        frame = ttk.Frame(root, padding=20)
        frame.grid(sticky="nsew")

        ttk.Label(
            frame,
            text="Save credentials so keyhive-proxy signs\nin automatically on startup?",
            justify="left",
        ).grid(row=0, column=0, columnspan=2, pady=(0, 16))

        def remember() -> None:
            auth.save_session(self._email, self._token)
            root.destroy()

        def not_now() -> None:
            auth.set_session_token(self._email, self._token)
            root.destroy()

        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=1, column=0, columnspan=2)
        ttk.Button(btn_frame, text="Yes, remember me", command=remember).pack(side="left", padx=(0, 8))
        ttk.Button(btn_frame, text="Not now", command=not_now).pack(side="left")

        root.protocol("WM_DELETE_WINDOW", not_now)
        root.mainloop()
