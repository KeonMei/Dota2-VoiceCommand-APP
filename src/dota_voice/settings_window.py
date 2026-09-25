from __future__ import annotations

import logging
import threading
import tkinter as tk
import tkinter.font as tkfont
from typing import TYPE_CHECKING, Any, Callable

from PIL import ImageTk

from . import ui_art
from .config import Config
from .notify import Notifier
from .speech import SpeechListener, input_devices
from .window import ACCENT, FONT, MUTED, SUBTLE, TEXT, key_labels

if TYPE_CHECKING:
    from .tray import TrayApp
    from .window import MainWindow

logger = logging.getLogger("dota_voice.settings")

WIDTH, HEIGHT = 470, 614
CARDS = [(18, 80, 452, 225), (18, 236, 452, 455), (18, 466, 452, 574)]
MIC_FIELD = (35, 142, 436, 169)
VOICE_FIELD = (143, 312, 436, 339)
LEVEL_BAR = (35, 183, 436, 191)
TOGGLE = (398, 282, 436, 302)
VOLUME_SLIDER = (143, 350, 398, 370)
TEMPO_SLIDER = (143, 379, 398, 399)
PREVIEW_BUTTON = (36, 410, 176, 442)
KEY_FIELD_RIGHT, KEY_FIELD_Y = 434, (507, 543)

SYSTEM_DEFAULT_MIC = "Системный по умолчанию"
SAPI_VOICE = "sapi"
PIPER_VOICE_NAMES = {"denis": "Денис", "dmitri": "Дмитрий", "ruslan": "Руслан", "irina": "Ирина"}
PREVIEW_PHRASE = "Так будут звучать мои ответы."
SAPI_BASE_RATE = 175

# The values config.yaml ships with.
DEFAULTS = {
    "input_device": None,
    "tts_enabled": True,
    "voice": "models/piper/ru_RU-denis-medium.onnx",
    "tts_volume": 0.15,
    "piper_length_scale": 0.9,
    "hotkey": "ctrl+alt+l",
}

_MODIFIER_KEYSYMS = {
    "Control_L": "ctrl", "Control_R": "ctrl",
    "Alt_L": "alt", "Alt_R": "alt",
    "Shift_L": "shift", "Shift_R": "shift",
    "Win_L": "windows", "Win_R": "windows",
}
_MODIFIER_ORDER = ["windows", "ctrl", "alt", "shift"]
_SAVE_DELAY_MS = 400
_TICK_MS = 50


def _key_name(keycode: int) -> str | None:
    """Windows virtual-key code -> `keyboard` library key name. Uses the
    physical key, so a Russian layout still records "l", not "д"."""
    if 0x41 <= keycode <= 0x5A or 0x30 <= keycode <= 0x39:
        return chr(keycode).lower()
    if 0x70 <= keycode <= 0x7B:
        return f"f{keycode - 0x6F}"
    return {0x20: "space"}.get(keycode)


def _ellipsize(text: str, font: tkfont.Font, max_width: float) -> str:
    if font.measure(text) <= max_width:
        return text
    while text and font.measure(text + "…") > max_width:
        text = text[:-1]
    return text.rstrip() + "…"


def voice_options(config: Config) -> list[tuple[str, str]]:
    """(label, value) pairs: every Piper model in models/piper, then the Windows voice."""
    options = []
    for model in sorted(config.resolve_path("models/piper").glob("*.onnx")):
        parts = model.stem.split("-")
        name = parts[1] if len(parts) > 1 else model.stem
        label = PIPER_VOICE_NAMES.get(name, name.capitalize())
        options.append((f"{label} (Piper)", f"models/piper/{model.name}"))
    options.append(("Системный голос Windows", SAPI_VOICE))
    return options


class SettingsWindow:
    """Microphone, voice replies and the listening hotkey. Every change is
    applied to the running assistant at once and written to config.yaml."""

    def __init__(self, main: MainWindow, config: Config, listener: SpeechListener, notifier: Notifier, tray: TrayApp):
        self.main = main
        self.config = config
        self.listener = listener
        self.notifier = notifier
        self.tray = tray
        self._pending_saves: set[tuple[str, str]] = set()
        self._save_job: str | None = None
        self._preview_thread: threading.Thread | None = None
        self._level = 0.0
        self._shown_level_px = -1
        self._recording = False
        self._held_modifiers: set[str] = set()
        self.alive = True

        top = self.top = tk.Toplevel(main.root)
        top.title("Настройки")
        top.configure(bg=ui_art.color(ui_art.BG))
        top.resizable(False, False)
        top.transient(main.root)
        top.protocol("WM_DELETE_WINDOW", self.close)
        # Tk fires only the most specific binding, so Escape never reaches _on_key_press.
        top.bind("<Escape>", lambda _e: self._stop_recording() if self._recording else self.close())
        top.bind("<KeyPress>", self._on_key_press)
        top.bind("<KeyRelease>", self._on_key_release)

        self._build()
        x = main.root.winfo_rootx() + (main.root.winfo_width() - WIDTH) // 2
        y = main.root.winfo_rooty() + max(0, (main.root.winfo_height() - HEIGHT) // 2)
        top.geometry(f"{WIDTH}x{HEIGHT}+{max(0, x)}+{max(0, y)}")
        main.decorate(top)
        top.focus_force()
        self._tick()

    # --- layout -------------------------------------------------------------

    def _build(self) -> None:
        c = self.canvas = tk.Canvas(self.top, width=WIDTH, height=HEIGHT, highlightthickness=0, bd=0)
        c.pack()
        boxes = [(*card, 12) for card in CARDS] + [(*MIC_FIELD, 6, *ui_art.FIELD), (*VOICE_FIELD, 6, *ui_art.FIELD)]
        self.background = ui_art.render_background((WIDTH, HEIGHT), boxes, art="background_settings.webp")
        self._images: dict[str, ImageTk.PhotoImage] = {"background": ImageTk.PhotoImage(self.background)}
        c.create_image(0, 0, image=self._images["background"], anchor="nw")

        self.font = tkfont.Font(family=FONT, size=11)
        self.small_font = tkfont.Font(family=FONT, size=9)

        c.create_text(31, 35, anchor="w", text="Настройки", fill=TEXT, font=(FONT, 22, "bold"))
        c.create_text(31, 62, anchor="w", text="Изменения применяются сразу", fill="#8b919b", font=(FONT, 11))

        self._section("icon_section_mic.png", "Микрофон", CARDS[0])
        self._section("icon_section_voice.png", "Голосовые ответы", CARDS[1])
        self._section("icon_section_hotkey.png", "Горячая клавиша", CARDS[2])

        # Microphone
        c.create_text(35, 130, anchor="w", text="Устройство ввода", fill=SUBTLE, font=self.font)
        mics = [(SYSTEM_DEFAULT_MIC, None)] + [(name, name) for name in input_devices()]
        current_mic = self.listener.input_device
        if isinstance(current_mic, str) and current_mic not in [v for _, v in mics]:
            mics.append((f"{current_mic} (не подключён)", current_mic))
        self.mic_dropdown = _Dropdown(self, MIC_FIELD, mics, current_mic, self._set_microphone)
        self._level_item = c.create_image(LEVEL_BAR[0], LEVEL_BAR[1], anchor="nw")
        self._mic_hint = c.create_text(35, 206, anchor="w", fill=MUTED, font=self.small_font)
        self._set_mic_hint()

        # Voice replies
        c.create_text(35, 292, anchor="w", text="Озвучивать ответы", fill=TEXT, font=self.font)
        self.tts_toggle = _Toggle(self, TOGGLE, bool(self.config.get("feedback", "tts_enabled", default=True)), self._set_tts_enabled)
        c.create_text(35, 325.5, anchor="w", text="Голос", fill=TEXT, font=self.font)
        self.voice_dropdown = _Dropdown(self, VOICE_FIELD, voice_options(self.config), self._current_voice(), self._set_voice)
        c.create_text(35, 360, anchor="w", text="Громкость", fill=TEXT, font=self.font)
        self.volume_slider = _Slider(
            self, VOLUME_SLIDER, 0.0, 1.0, 0.01,
            float(self.config.get("feedback", "tts_volume", default=0.55)),
            lambda v: f"{round(v * 100)}%", self._set_volume,
        )
        c.create_text(35, 389, anchor="w", text="Темп речи", fill=TEXT, font=self.font)
        self.tempo_slider = _Slider(
            self, TEMPO_SLIDER, 0.6, 1.6, 0.05, self._current_speed(),
            lambda v: f"{v:.2f}".rstrip("0").rstrip(".") + "×", self._set_speed,
        )
        self.preview_button = _Button(self, PREVIEW_BUTTON, "Прослушать", self._preview)

        # Hotkey
        c.create_text(35, 526, anchor="w", text="Вкл / выкл прослушивание", fill=TEXT, font=self.font)
        self._key_field = c.create_image(0, KEY_FIELD_Y[0], anchor="nw", tags=("key_field",))
        self._key_texts: list[int] = []
        self._key_hint = c.create_text(35, 554, anchor="w", fill=MUTED, font=self.small_font)
        c.tag_bind("key_field", "<Button-1>", lambda _e: self._start_recording())
        c.tag_bind("key_field", "<Enter>", lambda _e: c.configure(cursor="hand2"))
        c.tag_bind("key_field", "<Leave>", lambda _e: c.configure(cursor=""))
        self._render_hotkey()

        reset = c.create_text(WIDTH / 2, 591, text="Сбросить по умолчанию", fill=SUBTLE, font=(FONT, 10), tags=("reset",))
        c.tag_bind("reset", "<Enter>", lambda _e: (c.itemconfigure(reset, fill=TEXT), c.configure(cursor="hand2")))
        c.tag_bind("reset", "<Leave>", lambda _e: (c.itemconfigure(reset, fill=SUBTLE), c.configure(cursor="")))
        c.tag_bind("reset", "<Button-1>", lambda _e: self._reset())
        c.bind("<Button-1>", self._on_canvas_click, add="+")

    def _section(self, icon: str, title: str, card: tuple[int, int, int, int]) -> None:
        y = card[1] + 24
        self._images[icon] = ImageTk.PhotoImage(ui_art.fitted_icon(icon, 23))
        self.canvas.create_image(card[0] + 29, y, image=self._images[icon])
        self.canvas.create_text(card[0] + 53, y, anchor="w", text=title, fill=TEXT, font=(FONT, 15, "bold"))

    def crop(self, box: tuple[int, int, int, int]):
        return self.background.crop(box)

    # --- current values -----------------------------------------------------

    def _current_voice(self) -> str:
        if str(self.config.get("feedback", "tts_engine", default="sapi")).lower() != "piper":
            return SAPI_VOICE
        model = self.config.get("feedback", "piper_model", default="")
        for _, value in voice_options(self.config):
            if value != SAPI_VOICE and self.config.resolve_path(value).resolve() == self.config.resolve_path(model).resolve():
                return value
        return model

    def _current_speed(self) -> float:
        return 1 / float(self.config.get("feedback", "piper_length_scale", default=1.0))

    # --- applying changes ---------------------------------------------------

    def _store(self, section: str, key: str, value: Any) -> None:
        self.config.set(section, key, value)
        self._pending_saves.add((section, key))
        if self._save_job is not None:
            self.top.after_cancel(self._save_job)
        self._save_job = self.top.after(_SAVE_DELAY_MS, self._flush_saves)

    def _flush_saves(self) -> None:
        self._save_job = None
        for section, key in sorted(self._pending_saves):
            try:
                self.config.save(section, key)
            except OSError:
                logger.exception("Could not write %s.%s to config.yaml", section, key)
        self._pending_saves.clear()

    def _set_microphone(self, device: str | None) -> bool:
        try:
            self.listener.set_input_device(device)
        except Exception:
            logger.exception("Could not open microphone '%s'", device)
            self._set_mic_hint("Не удалось открыть это устройство", error=True)
            return False
        self._store("speech", "input_device", device)
        self._set_mic_hint()
        return True

    def _set_mic_hint(self, text: str = "Скажите что-нибудь — полоса должна двигаться", error: bool = False) -> None:
        self.canvas.itemconfigure(self._mic_hint, text=text, fill=ACCENT if error else MUTED)

    def _set_tts_enabled(self, enabled: bool) -> None:
        self.notifier.configure(enabled=enabled)
        self._store("feedback", "tts_enabled", enabled)

    def _set_voice(self, value: str) -> bool:
        if value == SAPI_VOICE:
            self.notifier.configure(engine="sapi")
            self._store("feedback", "tts_engine", "sapi")
        else:
            self.notifier.configure(engine="piper", piper_model=self.config.resolve_path(value))
            self._store("feedback", "tts_engine", "piper")
            self._store("feedback", "piper_model", value)
        return True

    def _set_volume(self, volume: float) -> None:
        self.notifier.configure(volume=volume)
        self._store("feedback", "tts_volume", round(volume, 2))

    def _set_speed(self, speed: float) -> None:
        # One tempo for both engines: Piper stretches phrase length, SAPI
        # counts words per minute.
        self._set_tempo_values(round(1 / speed, 2), round(SAPI_BASE_RATE * speed))

    def _set_tempo_values(self, length_scale: float, rate: int) -> None:
        self.notifier.configure(length_scale=length_scale, rate=rate)
        self._store("feedback", "piper_length_scale", length_scale)
        self._store("feedback", "tts_rate", rate)

    def _preview(self) -> None:
        if self._preview_thread is not None and self._preview_thread.is_alive():
            return
        self._preview_thread = threading.Thread(target=self.notifier.speak, args=(PREVIEW_PHRASE, True), daemon=True)
        self._preview_thread.start()

    def _set_hotkey(self, hotkey: str) -> None:
        self.tray.set_hotkey(hotkey)
        self.main.set_hotkey(hotkey)
        self._store("hotkeys", "toggle_listening", hotkey)

    def _reset(self) -> None:
        self._stop_recording()
        if self.mic_dropdown.value != DEFAULTS["input_device"]:
            self.mic_dropdown.choose(DEFAULTS["input_device"])
        self.tts_toggle.set(DEFAULTS["tts_enabled"], notify=True)
        voices = [v for _, v in voice_options(self.config)]
        self.voice_dropdown.choose(DEFAULTS["voice"] if DEFAULTS["voice"] in voices else voices[0])
        self.volume_slider.set(DEFAULTS["tts_volume"], notify=True)
        # Exact shipped values - the slider's 0.05 steps can't express 1 / 0.9.
        self.tempo_slider.set(1 / DEFAULTS["piper_length_scale"])
        self._set_tempo_values(DEFAULTS["piper_length_scale"], SAPI_BASE_RATE)
        self._set_hotkey(DEFAULTS["hotkey"])
        self._render_hotkey()

    # --- hotkey recording ---------------------------------------------------

    def _hotkey(self) -> str:
        return str(self.config.get("hotkeys", "toggle_listening", default="ctrl+alt+l"))

    def _start_recording(self) -> None:
        if self._recording:
            return
        self._recording = True
        self._held_modifiers.clear()
        # The old combo must not toggle listening while the new one is typed.
        self.tray.set_hotkey(None)
        self.top.focus_force()
        self._render_hotkey()

    def _stop_recording(self, new_hotkey: str | None = None) -> None:
        if not self._recording:
            return
        self._recording = False
        if new_hotkey:
            self._set_hotkey(new_hotkey)
        else:
            self.tray.set_hotkey(self._hotkey())
        self._render_hotkey()

    def _on_key_press(self, event) -> None:
        if not self._recording:
            return
        modifier = _MODIFIER_KEYSYMS.get(event.keysym)
        if modifier:
            self._held_modifiers.add(modifier)
            return
        key = _key_name(event.keycode)
        if key is None:
            self._set_key_hint("Эта клавиша не подходит — выберите букву, цифру или F1–F12", error=True)
            return
        if not self._held_modifiers:
            self._set_key_hint("Добавьте Ctrl, Alt, Shift или Win", error=True)
            return
        if len(self._held_modifiers) > 2:
            self._set_key_hint("Не больше двух клавиш-модификаторов", error=True)
            return
        modifiers = [m for m in _MODIFIER_ORDER if m in self._held_modifiers]
        self._stop_recording("+".join(modifiers + [key]))

    def _on_key_release(self, event) -> None:
        modifier = _MODIFIER_KEYSYMS.get(event.keysym)
        if modifier:
            self._held_modifiers.discard(modifier)

    def _on_canvas_click(self, _event) -> None:
        if self._recording and "key_field" not in self.canvas.gettags("current"):
            self._stop_recording()

    def _set_key_hint(self, text: str, error: bool = False) -> None:
        self.canvas.itemconfigure(self._key_hint, text=text, fill=ACCENT if error else MUTED)

    def _render_hotkey(self) -> None:
        c = self.canvas
        for item in self._key_texts:
            c.delete(item)
        self._key_texts = []
        y0, y1 = KEY_FIELD_Y
        mid = (y0 + y1) / 2
        key_font = tkfont.Font(family=FONT, size=10)

        if self._recording:
            width = 206
            x0 = KEY_FIELD_RIGHT - width
            self._key_texts.append(c.create_text(x0 + width / 2, mid, text="Нажмите сочетание…", fill=ACCENT, font=key_font))
            caps = []
            self._set_key_hint("Esc — отмена")
        else:
            labels = ["Win" if label == "Windows" else label for label in key_labels(self._hotkey())]
            widths = [max(34, key_font.measure(label) + 18) for label in labels]
            plus_gap = 22
            group = sum(widths) + plus_gap * (len(widths) - 1)
            width = max(206, group + 20)
            x0 = KEY_FIELD_RIGHT - width
            x = (width - group) / 2
            caps = []
            for i, (label, w) in enumerate(zip(labels, widths)):
                caps.append((round(x), round(x + w)))
                self._key_texts.append(c.create_text(x0 + x + w / 2, mid, text=label, fill=TEXT, font=key_font))
                if i < len(labels) - 1:
                    self._key_texts.append(c.create_text(x0 + x + w + plus_gap / 2, mid, text="+", fill=SUBTLE, font=key_font))
                x += w + plus_gap
            self._set_key_hint("Нажмите на поле, затем новое сочетание")

        box = (x0, y0, KEY_FIELD_RIGHT, y1)
        self._images["key_field"] = ImageTk.PhotoImage(ui_art.over(self.crop(box), ui_art.keycaps((width, y1 - y0), caps, self._recording)))
        c.itemconfigure(self._key_field, image=self._images["key_field"])
        c.coords(self._key_field, x0, y0)
        for item in self._key_texts:
            c.addtag_withtag("key_field", item)

    # --- lifecycle ----------------------------------------------------------

    def _tick(self) -> None:
        if not self.alive:
            return
        target = self.listener.level
        self._level = target if target > self._level else self._level * 0.8 + target * 0.2
        x0, y0, x1, y1 = LEVEL_BAR
        px = round(self._level * (x1 - x0))
        if px != self._shown_level_px:
            self._shown_level_px = px
            layer = ui_art.level_bar(px / (x1 - x0), (x1 - x0, y1 - y0))
            self._images["level"] = ImageTk.PhotoImage(ui_art.over(self.crop(LEVEL_BAR), layer))
            self.canvas.itemconfigure(self._level_item, image=self._images["level"])
        speaking = self._preview_thread is not None and self._preview_thread.is_alive()
        self.preview_button.set_label("Звучит…" if speaking else "Прослушать")
        self.top.after(_TICK_MS, self._tick)

    def focus(self) -> None:
        self.top.deiconify()
        self.top.lift()
        self.top.focus_force()

    def close(self) -> None:
        self._stop_recording()
        if self._save_job is not None:
            self.top.after_cancel(self._save_job)
        self._flush_saves()
        self.alive = False
        self.top.destroy()


class _Dropdown:
    def __init__(self, owner: SettingsWindow, box, options: list[tuple[str, Any]], value: Any, on_change: Callable[[Any], bool]):
        self.owner, self.box, self.options, self.value, self.on_change = owner, box, options, value, on_change
        c = owner.canvas
        x0, y0, x1, y1 = box
        mid = (y0 + y1) / 2
        tag = f"dropdown{id(self)}"
        self.text = c.create_text(x0 + 12, mid, anchor="w", fill=TEXT, font=owner.font, tags=(tag,))
        owner._images[f"chevron{id(self)}"] = ImageTk.PhotoImage(ui_art.fitted_icon("icon_chevron_down.png", 12))
        c.create_image(x1 - 16, mid, image=owner._images[f"chevron{id(self)}"], tags=(tag,))
        hit = c.create_rectangle(x0, y0, x1, y1, outline="", fill="", tags=(tag,))
        c.tag_raise(hit)
        c.tag_bind(tag, "<Button-1>", lambda _e: self._open())
        c.tag_bind(tag, "<Enter>", lambda _e: c.configure(cursor="hand2"))
        c.tag_bind(tag, "<Leave>", lambda _e: c.configure(cursor=""))
        self._render()

    def _label(self) -> str:
        return next((label for label, v in self.options if v == self.value), str(self.value))

    def _render(self) -> None:
        x0, _, x1, _ = self.box
        self.owner.canvas.itemconfigure(self.text, text=_ellipsize(self._label(), self.owner.font, x1 - x0 - 44))

    def _open(self) -> None:
        menu = tk.Menu(
            self.owner.top, tearoff=0, bg="#1a1d23", fg=TEXT, activebackground="#2b3039",
            activeforeground=TEXT, selectcolor=ui_art.color(ui_art.GREEN), font=(FONT, 10), bd=0,
        )
        current = next((i for i, (_, v) in enumerate(self.options) if v == self.value), None)
        self._selected = tk.StringVar(master=self.owner.top, value="" if current is None else str(current))
        for i, (label, value) in enumerate(self.options):
            menu.add_radiobutton(label=label, variable=self._selected, value=str(i), command=lambda v=value: self.choose(v))
        x0, _, _, y1 = self.box
        c = self.owner.canvas
        menu.tk_popup(c.winfo_rootx() + x0, c.winfo_rooty() + y1 + 2)

    def choose(self, value: Any) -> None:
        if value == self.value:
            return
        if self.on_change(value):
            self.value = value
            self._render()


class _Toggle:
    def __init__(self, owner: SettingsWindow, box, on: bool, on_change: Callable[[bool], None]):
        self.owner, self.box, self.on, self.on_change = owner, box, on, on_change
        c = owner.canvas
        tag = f"toggle{id(self)}"
        self.item = c.create_image(box[0], box[1], anchor="nw", tags=(tag,))
        c.tag_bind(tag, "<Button-1>", lambda _e: self.set(not self.on, notify=True))
        c.tag_bind(tag, "<Enter>", lambda _e: c.configure(cursor="hand2"))
        c.tag_bind(tag, "<Leave>", lambda _e: c.configure(cursor=""))
        self._render()

    def set(self, on: bool, notify: bool = False) -> None:
        self.on = on
        self._render()
        if notify:
            self.on_change(on)

    def _render(self) -> None:
        x0, y0, x1, y1 = self.box
        img = ui_art.over(self.owner.crop(self.box), ui_art.toggle_switch(self.on, (x1 - x0, y1 - y0)))
        self.owner._images[f"toggle{id(self)}"] = ImageTk.PhotoImage(img)
        self.owner.canvas.itemconfigure(self.item, image=self.owner._images[f"toggle{id(self)}"])


class _Slider:
    def __init__(self, owner: SettingsWindow, box, lo: float, hi: float, step: float, value: float,
                 fmt: Callable[[float], str], on_change: Callable[[float], None]):
        self.owner, self.box, self.lo, self.hi, self.step, self.fmt, self.on_change = owner, box, lo, hi, step, fmt, on_change
        self.value = self._snap(value)
        c = owner.canvas
        tag = f"slider{id(self)}"
        self.item = c.create_image(box[0], box[1], anchor="nw", tags=(tag,))
        self.label = c.create_text(433, (box[1] + box[3]) / 2, anchor="e", fill=TEXT, font=owner.font)
        c.tag_bind(tag, "<Button-1>", self._drag)
        c.tag_bind(tag, "<B1-Motion>", self._drag)
        c.tag_bind(tag, "<Enter>", lambda _e: c.configure(cursor="hand2"))
        c.tag_bind(tag, "<Leave>", lambda _e: c.configure(cursor=""))
        self._render()

    def _snap(self, value: float) -> float:
        value = min(self.hi, max(self.lo, value))
        return round(round((value - self.lo) / self.step) * self.step + self.lo, 4)

    def _drag(self, event) -> None:
        x0, _, x1, _ = self.box
        fraction = (event.x - x0 - 8) / (x1 - x0 - 16)
        self.set(self.lo + fraction * (self.hi - self.lo), notify=True)

    def set(self, value: float, notify: bool = False) -> None:
        value = self._snap(value)
        changed = value != self.value
        self.value = value
        self._render()
        if notify and changed:
            self.on_change(value)

    def _render(self) -> None:
        x0, y0, x1, y1 = self.box
        fraction = (self.value - self.lo) / (self.hi - self.lo)
        img = ui_art.over(self.owner.crop(self.box), ui_art.slider(fraction, (x1 - x0, y1 - y0)))
        self.owner._images[f"slider{id(self)}"] = ImageTk.PhotoImage(img)
        self.owner.canvas.itemconfigure(self.item, image=self.owner._images[f"slider{id(self)}"])
        self.owner.canvas.itemconfigure(self.label, text=self.fmt(self.value))


class _Button:
    def __init__(self, owner: SettingsWindow, box, label: str, on_click: Callable[[], None]):
        self.owner, self.box = owner, box
        c = owner.canvas
        x0, y0, x1, y1 = box
        size = (x1 - x0, y1 - y0)
        self._states = {
            hover: ImageTk.PhotoImage(ui_art.over(owner.crop(box), ui_art.outline_button(hover, size)))
            for hover in (False, True)
        }
        tag = f"button{id(self)}"
        self.item = c.create_image(x0, y0, anchor="nw", image=self._states[False], tags=(tag,))
        mid = (y0 + y1) / 2
        owner._images["play"] = ImageTk.PhotoImage(ui_art.tinted(ui_art.fitted_icon("icon_play.png", 13), (236, 238, 241)))
        c.create_image(x0 + 21, mid, image=owner._images["play"], tags=(tag,))
        self.text = c.create_text(x0 + 39, mid, anchor="w", text=label, fill=TEXT, font=owner.font, tags=(tag,))
        self._label = label
        c.tag_bind(tag, "<Button-1>", lambda _e: on_click())
        c.tag_bind(tag, "<Enter>", lambda _e: (c.itemconfigure(self.item, image=self._states[True]), c.configure(cursor="hand2")))
        c.tag_bind(tag, "<Leave>", lambda _e: (c.itemconfigure(self.item, image=self._states[False]), c.configure(cursor="")))

    def set_label(self, label: str) -> None:
        if label != self._label:
            self._label = label
            self.owner.canvas.itemconfigure(self.text, text=label)
