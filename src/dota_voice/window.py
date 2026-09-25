from __future__ import annotations

import ctypes
import logging
import math
import os
import subprocess
import threading
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from typing import Callable

import win32api
import win32con
import win32event
import win32gui
from PIL import ImageTk

from . import ui_art
from .speech import SpeechListener

logger = logging.getLogger("dota_voice.window")

APP_TITLE = "Dota2 Voice Assistant"
# A second launch (e.g. the desktop shortcut clicked again) signals this event
# instead of starting another instance; the running window then shows itself.
SHOW_WINDOW_EVENT = "Local\\Dota2VoiceAssistantShowWindow"

TEXT = "#eceef1"
SUBTLE = "#b4b9c1"
MUTED = "#80868f"
ACCENT = "#e5402f"
ON = "#3ddc84"
FAIL = "#f06a5a"
FONT = "Segoe UI"
ICON_FONT = "Segoe Fluent Icons"

WIDTH, HEIGHT = 470, 712
MARGIN = 18
MIC_CENTER_Y = 214
MIC_RING_RADIUS = 66
MIC_BOX = 200
WAVE_Y, WAVE_BARS, WAVE_STEP, WAVE_MAX = 382, 33, 6, 15
CARD = (MARGIN, 408, WIDTH - MARGIN, 470)
TILE_H, TILE_GAP = 40, 10
TILES_Y = 518
SEPARATOR_Y = 648

TRY_SAYING = [
    ("icon_turbo.png", "Запусти турбо"),
    ("icon_all_pick.png", "Запусти олл пик"),
    ("icon_ranked.png", "Рейтинг на мид"),
    ("icon_basic_minimum.png", "Базовый минимум"),
]
GEAR_COLOR, GEAR_HOVER_COLOR = (128, 134, 143), (236, 238, 241)

_TICK_MS = 33
_COMMAND_STATUS = {
    "running": ("Выполняется…", MUTED),
    "done": ("Команда выполнена", SUBTLE),
    "stopped": ("Остановлена", MUTED),
    "failed": ("Не удалось выполнить", FAIL),
}


def signal_show_window() -> None:
    handle = win32event.CreateEvent(None, False, False, SHOW_WINDOW_EVENT)
    win32event.SetEvent(handle)


class MainWindow:
    """The control window: a big round button that toggles listening, a live
    microphone level, the last command's outcome and example phrases. Closing
    the window only hides it - the assistant keeps running in the tray."""

    def __init__(
        self,
        listener: SpeechListener,
        on_state_changed: Callable[[], None],
        hotkey: str,
        icon_image,
        icon_file: Path | None = None,
        settings_file: Path | None = None,
    ):
        self.listener = listener
        self._on_state_changed = on_state_changed
        self._settings_file = settings_file
        self._show_requested = threading.Event()
        self._quit_requested = threading.Event()
        self._show_event = win32event.CreateEvent(None, False, False, SHOW_WINDOW_EVENT)
        self._mic_hover = False
        self._gear_hover = False
        self._phase = 0.0
        self._level = 0.0
        self._shown_enabled: bool | None = None
        self._shown_mic_key: tuple | None = None
        # (spoken text, outcome) - written from command threads, read by _tick.
        self._command: tuple[str, str] | None = None
        self._shown_command: tuple[str, str] | None = None
        self._spinner_index = 0

        try:
            # Own taskbar entry/icon instead of being grouped under python.exe.
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Dota2VoiceAssistant")
        except Exception:
            pass

        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.configure(bg=ui_art.color(ui_art.BG))
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self.root.bind("<Escape>", lambda _e: self.hide())

        self._build(hotkey)
        self._center(WIDTH, HEIGHT)
        self._set_icon(icon_image, icon_file)
        self._dark_title_bar()
        self._tick()

    # --- layout -------------------------------------------------------------

    def _tile_rects(self) -> list[tuple[int, int, int, int]]:
        tile_w = (WIDTH - 2 * MARGIN - TILE_GAP) // 2
        rects = []
        for i in range(len(TRY_SAYING)):
            col, row = i % 2, i // 2
            x0 = MARGIN + col * (tile_w + TILE_GAP)
            y0 = TILES_Y + row * (TILE_H + TILE_GAP)
            rects.append((x0, y0, x0 + tile_w, y0 + TILE_H))
        return rects

    def _build(self, hotkey: str) -> None:
        c = self.canvas = tk.Canvas(self.root, width=WIDTH, height=HEIGHT, highlightthickness=0, bd=0)
        c.pack()
        cx = WIDTH / 2
        tiles = self._tile_rects()
        panels = [(*CARD, 10)] + [(*t, 8) for t in tiles]
        background = ui_art.render_background((WIDTH, HEIGHT), panels, SEPARATOR_Y)
        self._images = {"background": ImageTk.PhotoImage(background)}
        c.create_image(0, 0, image=self._images["background"], anchor="nw")

        # Header
        c.create_text(cx, 30, text="D O T A   2", fill=ACCENT, font=(FONT, 11, "bold"))
        c.create_text(cx, 62, text="Голосовой ассистент", fill=TEXT, font=(FONT, 21, "bold"))
        c.create_text(cx, 95, text="Управляй запуском и поиском матча", fill=SUBTLE, font=(FONT, 11))

        # Mic button
        half = MIC_BOX // 2
        crop = background.crop((int(cx) - half, MIC_CENTER_Y - half, int(cx) + half, MIC_CENTER_Y + half))
        self._mic_art = ui_art.MicButtonArt(crop, MIC_RING_RADIUS)
        self._images["mic"] = ImageTk.PhotoImage(self._mic_art.frame(True, False, 0.0))
        c.create_image(cx, MIC_CENTER_Y, image=self._images["mic"])

        # Status line: dot + word, centered as a group
        self._status_font = tkfont.Font(family=FONT, size=13, weight="bold")
        self._images["dot_on"] = ImageTk.PhotoImage(ui_art.dot(ui_art.GREEN, 10, glow=True))
        self._images["dot_off"] = ImageTk.PhotoImage(ui_art.dot(ui_art.GREY, 10))
        self._status_dot = c.create_image(0, 324, image=self._images["dot_on"])
        self._status_text = c.create_text(0, 324, anchor="w", font=self._status_font)
        self._caption = c.create_text(cx, 350, fill=MUTED, font=(FONT, 10))

        # Microphone level
        x0 = cx - (WAVE_BARS - 1) / 2 * WAVE_STEP
        self._wave = [
            c.create_line(x0 + i * WAVE_STEP, WAVE_Y, x0 + i * WAVE_STEP, WAVE_Y, width=3, capstyle="round")
            for i in range(WAVE_BARS)
        ]
        self._wave_profile = [ui_art.wave_profile(i, WAVE_BARS) for i in range(WAVE_BARS)]

        # Last command card
        cx0, cy0, cx1, cy1 = CARD
        c.create_text(cx0 + 16, cy0 + 16, anchor="w", text="П О С Л Е Д Н Я Я   К О М А Н Д А", fill=MUTED, font=(FONT, 7, "bold"))
        self._phrase_font = tkfont.Font(family=FONT, size=15, weight="bold")
        self._card_status_font = tkfont.Font(family=FONT, size=10)
        self._phrase = c.create_text(cx0 + 16, cy0 + 41, anchor="w", font=self._phrase_font)
        self._card_status = c.create_text(cx1 - 16, cy0 + 41, anchor="e", font=self._card_status_font)
        self._card_icon = c.create_image(0, cy0 + 41)
        for kind in ("done", "failed", "stopped"):
            self._images[f"status_{kind}"] = ImageTk.PhotoImage(ui_art.status_icon(kind))
        self._spinner = [ImageTk.PhotoImage(f) for f in ui_art.spinner_frames()]

        # Example phrases (not clickable yet)
        c.create_text(MARGIN + 2, TILES_Y - 22, anchor="w", text="Попробуйте сказать", fill=TEXT, font=(FONT, 14, "bold"))
        for (icon, label), (x0, y0, x1, y1) in zip(TRY_SAYING, tiles):
            my = (y0 + y1) / 2
            self._images[icon] = ImageTk.PhotoImage(ui_art.fitted_icon(icon, 21))
            c.create_image(x0 + 24, my, image=self._images[icon])
            c.create_text(x0 + 46, my, anchor="w", text=label, fill=TEXT, font=(FONT, 11))
            c.create_text(x1 - 16, my, text="", fill=MUTED, font=(ICON_FONT, 9))

        # "Стоп" hint: square + red word + muted rest, centered as a group
        stop_y = TILES_Y + 2 * TILE_H + TILE_GAP + 26
        word_font = tkfont.Font(family=FONT, size=10)
        rest = " — отменить текущую команду"
        word = "«Стоп»"
        total = 14 + 8 + word_font.measure(word) + word_font.measure(rest)
        left = cx - total / 2
        self._images["stop_square"] = ImageTk.PhotoImage(ui_art.rounded_square(ui_art.RED, 13, 2))
        c.create_image(left + 7, stop_y, image=self._images["stop_square"])
        c.create_text(left + 22, stop_y, anchor="w", text=word, fill=ACCENT, font=word_font)
        c.create_text(left + 22 + word_font.measure(word), stop_y, anchor="w", text=rest, fill=SUBTLE, font=word_font)

        # Footer
        foot_y = SEPARATOR_Y + 24
        self._images["dot_footer"] = ImageTk.PhotoImage(ui_art.dot(ui_art.GREEN, 8, glow=True))
        c.create_image(MARGIN + 12, foot_y, image=self._images["dot_footer"])
        c.create_text(MARGIN + 26, foot_y, anchor="w", text="Распознавание офлайн", fill=SUBTLE, font=(FONT, 10))
        self._images["gear"] = ImageTk.PhotoImage(ui_art.icon("icon_settings.png", 22, GEAR_COLOR))
        self._images["gear_hover"] = ImageTk.PhotoImage(ui_art.icon("icon_settings.png", 22, GEAR_HOVER_COLOR))
        self._gear = c.create_image(WIDTH - MARGIN - 12, foot_y, image=self._images["gear"], tags=("gear",))
        keys = " + ".join(part.strip().capitalize() for part in hotkey.split("+"))
        c.create_text(cx, foot_y + 28, text=f"{keys} — вкл / выкл     |     Esc — закрыть", fill=MUTED, font=(FONT, 9))

        c.bind("<Motion>", lambda e: self._set_hover(self._over_mic(e.x, e.y), self._gear_hover))
        c.bind("<Leave>", lambda _e: self._set_hover(False, False))
        c.bind("<Button-1>", self._on_click)
        c.tag_bind("gear", "<Enter>", lambda _e: self._set_hover(self._mic_hover, True))
        c.tag_bind("gear", "<Leave>", lambda _e: self._set_hover(self._mic_hover, False))

        self._render_command()

    def _center(self, width: int, height: int) -> None:
        x = (self.root.winfo_screenwidth() - width) // 2
        y = max(0, (self.root.winfo_screenheight() - height) // 3)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _hwnd(self) -> int:
        self.root.update_idletasks()
        return ctypes.windll.user32.GetParent(self.root.winfo_id())

    def _set_icon(self, icon_image, icon_file: Path | None) -> None:
        try:
            if icon_file is not None and icon_file.exists():
                # Tk's iconbitmap stretches the .ico's 16 px image up for the
                # taskbar; loading each size natively keeps it sharp.
                hwnd = self._hwnd()
                for which, metric in ((win32con.ICON_BIG, win32con.SM_CXICON), (win32con.ICON_SMALL, win32con.SM_CXSMICON)):
                    size = win32api.GetSystemMetrics(metric)
                    handle = win32gui.LoadImage(0, str(icon_file), win32con.IMAGE_ICON, size, size, win32con.LR_LOADFROMFILE)
                    win32gui.SendMessage(hwnd, win32con.WM_SETICON, which, handle)
            else:
                self._icon_photo = ImageTk.PhotoImage(icon_image)
                self.root.iconphoto(True, self._icon_photo)
        except Exception:
            logger.debug("Could not set the window icon", exc_info=True)

    def _dark_title_bar(self) -> None:
        try:
            hwnd = self._hwnd()
            value = ctypes.c_int(1)
            # DWMWA_USE_IMMERSIVE_DARK_MODE (Windows 10 20H1+ / 11)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))
        except Exception:
            pass

    # --- input --------------------------------------------------------------

    def _over_mic(self, x: int, y: int) -> bool:
        return math.hypot(x - WIDTH / 2, y - MIC_CENTER_Y) <= MIC_RING_RADIUS + 4

    def _set_hover(self, mic: bool, gear: bool) -> None:
        self.canvas.configure(cursor="hand2" if mic or gear else "")
        if gear != self._gear_hover:
            self._gear_hover = gear
            self.canvas.itemconfigure(self._gear, image=self._images["gear_hover" if gear else "gear"])
        if mic != self._mic_hover:
            self._mic_hover = mic
            self._render_mic()

    def _on_click(self, event) -> None:
        if self._gear_hover:
            self._open_settings()
        elif self._over_mic(event.x, event.y):
            self.toggle()

    def _open_settings(self) -> None:
        if self._settings_file is None:
            return
        try:
            os.startfile(str(self._settings_file))
        except OSError:
            # No app associated with .yaml on this machine.
            subprocess.Popen(["notepad.exe", str(self._settings_file)])

    # --- state --------------------------------------------------------------

    def toggle(self) -> None:
        self.listener.toggle_enabled()
        self._on_state_changed()
        self._render()

    def show_command(self, text: str, outcome: str) -> None:
        """Safe to call from any thread. outcome: running / done / stopped / failed."""
        self._command = (text, outcome)

    def _render_mic(self) -> None:
        enabled = self.listener.is_enabled
        pulse = (math.sin(self._phase) + 1) / 2 if enabled else 0.0
        frame = self._mic_art.frame(enabled, self._mic_hover, pulse)
        key = id(frame)
        if key != self._shown_mic_key:
            self._shown_mic_key = key
            self._images["mic"].paste(frame)

    def _render_wave(self) -> None:
        enabled = self.listener.is_enabled
        target = self.listener.level if enabled else 0.0
        # Fast attack, slow release, so speech reads as a smooth envelope.
        self._level = target if target > self._level else self._level * 0.88 + target * 0.12
        c = self.canvas
        x0 = WIDTH / 2 - (WAVE_BARS - 1) / 2 * WAVE_STEP
        for i, bar in enumerate(self._wave):
            profile = self._wave_profile[i]
            if enabled:
                wobble = 0.6 + 0.4 * math.sin(self._phase * 2.3 + i * 0.9) * math.sin(self._phase * 1.1 + i * 0.37)
                half = 1 + (1.5 + WAVE_MAX * self._level * wobble) * profile
                color = ui_art.color(_mix((23, 70, 47), ui_art.GREEN, 0.25 + 0.75 * profile))
            else:
                half, color = 1, ui_art.color((52, 56, 64))
            x = x0 + i * WAVE_STEP
            c.coords(bar, x, WAVE_Y - half, x, WAVE_Y + half)
            c.itemconfigure(bar, fill=color)

    def _render_status(self) -> None:
        enabled = self.listener.is_enabled
        if self._shown_enabled == enabled:
            return
        self._shown_enabled = enabled
        word = "Слушаю" if enabled else "На паузе"
        group = 10 + 10 + self._status_font.measure(word)
        left = WIDTH / 2 - group / 2
        c = self.canvas
        c.itemconfigure(self._status_dot, image=self._images["dot_on" if enabled else "dot_off"])
        c.coords(self._status_dot, left + 5, 324)
        c.itemconfigure(self._status_text, text=word, fill=ON if enabled else MUTED)
        c.coords(self._status_text, left + 20, 324)
        c.itemconfigure(
            self._caption,
            text="Нажмите на микрофон, чтобы поставить на паузу" if enabled else "Нажмите на микрофон, чтобы включить",
        )

    def _render_command(self) -> None:
        command = self._command
        running = command is not None and command[1] == "running"
        if command == self._shown_command and not running:
            return
        c = self.canvas
        if running:
            self._spinner_index = (self._spinner_index + 1) % (len(self._spinner) * 3)
            c.itemconfigure(self._card_icon, image=self._spinner[self._spinner_index // 3])
            if command == self._shown_command:
                return
        self._shown_command = command

        cx0, cy0, cx1, _ = CARD
        if command is None:
            c.itemconfigure(self._phrase, text="Пока нет команд", fill=MUTED)
            c.itemconfigure(self._card_status, text="")
            c.itemconfigure(self._card_icon, image="")
            return

        text, outcome = command
        status, status_color = _COMMAND_STATUS[outcome]
        c.itemconfigure(self._card_status, text=status, fill=status_color)
        status_left = cx1 - 16 - self._card_status_font.measure(status)
        icon_x = status_left - 8 - 11
        c.coords(self._card_icon, icon_x, cy0 + 41)
        if not running:
            c.itemconfigure(self._card_icon, image=self._images[f"status_{outcome}"])
        phrase = f"«{text[:1].upper()}{text[1:]}»"
        c.itemconfigure(self._phrase, text=_ellipsize(phrase, self._phrase_font, icon_x - 11 - 12 - (cx0 + 16)), fill=TEXT)

    def _render(self) -> None:
        self._render_mic()
        self._render_status()
        self._render_wave()
        self._render_command()

    def _tick(self) -> None:
        if self._quit_requested.is_set():
            self.root.destroy()
            return
        if self._show_requested.is_set() or win32event.WaitForSingleObject(self._show_event, 0) == win32event.WAIT_OBJECT_0:
            self._show_requested.clear()
            self._show_now()

        if self.root.state() != "withdrawn":
            self._phase += 0.09
            self._render()
        self.root.after(_TICK_MS, self._tick)

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


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def _ellipsize(text: str, font: tkfont.Font, max_width: float) -> str:
    if font.measure(text) <= max_width:
        return text
    while text and font.measure(text + "…»") > max_width:
        text = text[:-1]
    return text.rstrip() + "…»"
