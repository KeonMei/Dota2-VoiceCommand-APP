from __future__ import annotations

import ctypes
import logging
import math
import threading
import tkinter as tk
from pathlib import Path
from typing import Callable

import win32event
from PIL import ImageTk

from .speech import SpeechListener

logger = logging.getLogger("dota_voice.window")

APP_TITLE = "Dota2 Voice Assistant"
# A second launch (e.g. the desktop shortcut clicked again) signals this event
# instead of starting another instance; the running window then shows itself.
SHOW_WINDOW_EVENT = "Local\\Dota2VoiceAssistantShowWindow"

BG = "#0f1115"
TEXT = "#e8eaed"
MUTED = "#7d838d"
ACCENT = "#d8412f"
ON = "#3ddc84"
ON_DIM = "#1d5c3c"
OFF = "#4a4f58"
OFF_DIM = "#23272e"
FONT = "Segoe UI"

_BUTTON_SIZE = 170
_POLL_MS = 100


def signal_show_window() -> None:
    handle = win32event.CreateEvent(None, False, False, SHOW_WINDOW_EVENT)
    win32event.SetEvent(handle)


class MainWindow:
    """A small always-available control window: app name, one round button
    that toggles listening and shows whether it's on. Closing the window only
    hides it - the assistant keeps running in the tray."""

    def __init__(
        self,
        listener: SpeechListener,
        on_state_changed: Callable[[], None],
        hotkey: str,
        icon_image,
        icon_file: Path | None = None,
    ):
        self.listener = listener
        self._on_state_changed = on_state_changed
        self._show_requested = threading.Event()
        self._quit_requested = threading.Event()
        self._show_event = win32event.CreateEvent(None, False, False, SHOW_WINDOW_EVENT)
        self._shown_state: bool | None = None
        self._hover = False
        self._phase = 0.0

        try:
            # Own taskbar entry/icon instead of being grouped under python.exe.
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Dota2VoiceAssistant")
        except Exception:
            pass

        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self._set_icon(icon_image, icon_file)
        self.root.protocol("WM_DELETE_WINDOW", self.hide)

        self._build(hotkey)
        self._center(340, 450)
        self._dark_title_bar()
        self._tick()

    # --- layout -------------------------------------------------------------

    def _build(self, hotkey: str) -> None:
        tk.Label(self.root, text="D O T A   2", bg=BG, fg=ACCENT, font=(FONT, 10, "bold")).pack(pady=(30, 0))
        tk.Label(self.root, text="Voice Assistant", bg=BG, fg=TEXT, font=(FONT, 20, "bold")).pack()

        pad = 24
        size = _BUTTON_SIZE + pad * 2
        self.canvas = tk.Canvas(self.root, width=size, height=size, bg=BG, highlightthickness=0, cursor="hand2")
        self.canvas.pack(pady=(26, 8))
        center = size / 2
        r = _BUTTON_SIZE / 2
        self._glow = self.canvas.create_oval(center - r - 14, center - r - 14, center + r + 14, center + r + 14, width=0)
        self._ring = self.canvas.create_oval(center - r, center - r, center + r, center + r, width=4)
        self._disc = self.canvas.create_oval(center - r + 10, center - r + 10, center + r - 10, center + r - 10, width=0)
        self._mic = self._draw_mic(center, center)

        self.canvas.bind("<Button-1>", lambda _e: self.toggle())
        self.canvas.bind("<Enter>", lambda _e: self._set_hover(True))
        self.canvas.bind("<Leave>", lambda _e: self._set_hover(False))

        self.status = tk.Label(self.root, bg=BG, font=(FONT, 12, "bold"))
        self.status.pack()
        self.caption = tk.Label(self.root, bg=BG, fg=MUTED, font=(FONT, 9))
        self.caption.pack(pady=(2, 0))

        tk.Label(
            self.root,
            text=f"{hotkey.title()} — вкл/выкл   ·   закрытое окно остаётся в трее",
            bg=BG, fg=MUTED, font=(FONT, 8),
        ).pack(side="bottom", pady=(0, 14))

    def _draw_mic(self, cx: float, cy: float) -> list[int]:
        c = self.canvas
        w, h = 13, 22  # half-width of the capsule, half-height of its straight part
        items = [
            c.create_oval(cx - w, cy - 34, cx + w, cy - 34 + 2 * w, width=0),
            c.create_rectangle(cx - w, cy - 34 + w, cx + w, cy - 34 + w + h, width=0),
            c.create_oval(cx - w, cy - 34 + h, cx + w, cy - 34 + h + 2 * w, width=0),
            c.create_arc(cx - 24, cy - 22, cx + 24, cy + 24, start=180, extent=180, style="arc", width=4),
            c.create_line(cx, cy + 24, cx, cy + 36, width=4),
            c.create_line(cx - 14, cy + 36, cx + 14, cy + 36, width=4, capstyle="round"),
        ]
        return items

    def _center(self, width: int, height: int) -> None:
        x = (self.root.winfo_screenwidth() - width) // 2
        y = (self.root.winfo_screenheight() - height) // 3
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _set_icon(self, icon_image, icon_file: Path | None) -> None:
        try:
            if icon_file is not None and icon_file.exists():
                self.root.iconbitmap(default=str(icon_file))
            else:
                self._icon_photo = ImageTk.PhotoImage(icon_image)
                self.root.iconphoto(True, self._icon_photo)
        except Exception:
            logger.debug("Could not set the window icon", exc_info=True)

    def _dark_title_bar(self) -> None:
        try:
            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            value = ctypes.c_int(1)
            # DWMWA_USE_IMMERSIVE_DARK_MODE (Windows 10 20H1+ / 11)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))
        except Exception:
            pass

    # --- state --------------------------------------------------------------

    def toggle(self) -> None:
        self.listener.toggle_enabled()
        self._on_state_changed()
        self._render()

    def _set_hover(self, hover: bool) -> None:
        self._hover = hover
        self._render()

    def _render(self) -> None:
        enabled = self.listener.is_enabled
        c = self.canvas
        pulse = (math.sin(self._phase) + 1) / 2 if enabled else 0.0

        ring = ON if enabled else (MUTED if self._hover else OFF)
        disc = _mix(ON_DIM, "#256f49", 0.6) if enabled and self._hover else (ON_DIM if enabled else (OFF if self._hover else OFF_DIM))
        c.itemconfigure(self._ring, outline=ring)
        c.itemconfigure(self._disc, fill=disc)
        c.itemconfigure(self._glow, fill=_mix(BG, ON_DIM, 0.35 + 0.45 * pulse) if enabled else BG)
        mic_color = TEXT if enabled else "#9aa0a8"
        for item in self._mic:
            c.itemconfigure(item, **{"outline" if c.type(item) == "arc" else "fill": mic_color})

        if self._shown_state != enabled:
            self._shown_state = enabled
            self.status.configure(text="СЛУШАЮ" if enabled else "НА ПАУЗЕ", fg=ON if enabled else MUTED)
            self.caption.configure(
                text="Нажмите, чтобы поставить на паузу" if enabled else "Нажмите, чтобы включить"
            )

    def _tick(self) -> None:
        if self._quit_requested.is_set():
            self.root.destroy()
            return
        if self._show_requested.is_set() or win32event.WaitForSingleObject(self._show_event, 0) == win32event.WAIT_OBJECT_0:
            self._show_requested.clear()
            self._show_now()

        if self.listener.is_enabled:
            self._phase += 0.12
            self._render()
        elif self._shown_state is not False:
            self._render()
        self.root.after(_POLL_MS, self._tick)

    # --- visibility / lifecycle (show/quit are safe to call from any thread) --

    def hide(self) -> None:
        self.root.withdraw()

    def request_show(self) -> None:
        self._show_requested.set()

    def request_quit(self) -> None:
        self._quit_requested.set()

    def _show_now(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(200, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()

    def run(self) -> None:
        self.root.mainloop()


def _mix(a: str, b: str, t: float) -> str:
    t = max(0.0, min(1.0, t))
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(ca, cb))
