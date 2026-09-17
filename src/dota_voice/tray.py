from __future__ import annotations

import logging
import os
import subprocess
import threading
from pathlib import Path
from typing import Callable

import keyboard
import pystray
from PIL import Image, ImageDraw

from .config import Config
from .speech import SpeechListener

logger = logging.getLogger("dota_voice.tray")


def make_icon_image(active: bool) -> Image.Image:
    color = (0, 200, 0) if active else (150, 150, 150)
    img = Image.new("RGB", (64, 64), (30, 30, 30))
    draw = ImageDraw.Draw(img)
    draw.ellipse((12, 12, 52, 52), fill=color)
    return img


class TrayApp:
    def __init__(
        self,
        config: Config,
        listener: SpeechListener,
        on_show_window: Callable[[], None] = lambda: None,
        on_exit: Callable[[], None] = lambda: None,
    ):
        self.config = config
        self.listener = listener
        self._on_show_window = on_show_window
        self._on_exit_callback = on_exit
        self.logs_dir = config.resolve_path(config.get("feedback", "log_file", default="logs/app.log")).parent
        self._icon = pystray.Icon(
            "dota_voice",
            make_icon_image(listener.is_enabled),
            "Dota2 Voice Command",
            menu=self._build_menu(),
        )

    def _build_menu(self) -> pystray.Menu:
        return pystray.Menu(
            # default=True: also triggered by clicking the tray icon itself.
            pystray.MenuItem("Открыть окно", lambda icon, item: self._on_show_window(), default=True),
            pystray.MenuItem(
                lambda item: "Прослушивание: ВКЛ" if self.listener.is_enabled else "Прослушивание: ВЫКЛ",
                self._on_toggle,
            ),
            pystray.MenuItem("Открыть папку логов", self._on_open_logs),
            pystray.MenuItem("Выход", self._on_exit),
        )

    def _on_toggle(self, icon: pystray.Icon, item) -> None:
        self.listener.toggle_enabled()
        self.refresh()

    def refresh(self) -> None:
        """Re-syncs the icon and menu with the listening state."""
        self._icon.icon = make_icon_image(self.listener.is_enabled)
        self._icon.update_menu()

    def _on_open_logs(self, icon: pystray.Icon, item) -> None:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(str(self.logs_dir))

    def _on_exit(self, icon: pystray.Icon, item) -> None:
        logger.info("Shutting down from the tray menu.")
        self.listener.stop()
        icon.stop()
        self._on_exit_callback()

    def _register_hotkey(self) -> None:
        hotkey = self.config.get("hotkeys", "toggle_listening", default="ctrl+alt+l")

        def _toggle():
            self.listener.toggle_enabled()
            self.refresh()

        try:
            keyboard.add_hotkey(hotkey, _toggle)
            logger.info("Registered the listening toggle hotkey: %s", hotkey)
        except Exception:
            logger.exception("Failed to register the global hotkey '%s'", hotkey)

    def start(self) -> None:
        """Runs the tray icon on its own thread - the main thread belongs to
        the window."""
        self._register_hotkey()
        self._icon.run_detached()

    def stop(self) -> None:
        self._icon.stop()
