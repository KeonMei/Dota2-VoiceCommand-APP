from __future__ import annotations

import logging
import os
import subprocess
import threading
from pathlib import Path

import keyboard
import pystray
from PIL import Image, ImageDraw

from .config import Config
from .speech import SpeechListener

logger = logging.getLogger("dota_voice.tray")


def _make_icon_image(active: bool) -> Image.Image:
    color = (0, 200, 0) if active else (150, 150, 150)
    img = Image.new("RGB", (64, 64), (30, 30, 30))
    draw = ImageDraw.Draw(img)
    draw.ellipse((12, 12, 52, 52), fill=color)
    return img


class TrayApp:
    def __init__(self, config: Config, listener: SpeechListener):
        self.config = config
        self.listener = listener
        self.logs_dir = config.resolve_path(config.get("feedback", "log_file", default="logs/app.log")).parent
        self._icon = pystray.Icon(
            "dota_voice",
            _make_icon_image(listener.is_enabled),
            "Dota2 Voice Command",
            menu=self._build_menu(),
        )

    def _build_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem(
                lambda item: "Прослушивание: ВКЛ" if self.listener.is_enabled else "Прослушивание: ВЫКЛ",
                self._on_toggle,
            ),
            pystray.MenuItem("Открыть папку логов", self._on_open_logs),
            pystray.MenuItem("Выход", self._on_exit),
        )

    def _on_toggle(self, icon: pystray.Icon, item) -> None:
        enabled = self.listener.toggle_enabled()
        icon.icon = _make_icon_image(enabled)
        icon.update_menu()

    def _on_open_logs(self, icon: pystray.Icon, item) -> None:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(str(self.logs_dir))

    def _on_exit(self, icon: pystray.Icon, item) -> None:
        logger.info("Shutting down from the tray menu.")
        self.listener.stop()
        icon.stop()

    def _register_hotkey(self) -> None:
        hotkey = self.config.get("hotkeys", "toggle_listening", default="ctrl+alt+l")

        def _toggle():
            enabled = self.listener.toggle_enabled()
            self._icon.icon = _make_icon_image(enabled)
            self._icon.update_menu()

        try:
            keyboard.add_hotkey(hotkey, _toggle)
            logger.info("Registered the listening toggle hotkey: %s", hotkey)
        except Exception:
            logger.exception("Failed to register the global hotkey '%s'", hotkey)

    def run(self) -> None:
        self._register_hotkey()
        self._icon.run()
