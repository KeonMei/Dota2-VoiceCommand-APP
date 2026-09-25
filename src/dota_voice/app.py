from __future__ import annotations

import logging
import threading
import time

from .actions import ActionExecutor
from .commands import CommandMatcher
from .config import CommandsConfig, Config
from .logging_setup import setup_logging
from .notify import Notifier
from .settings_window import SettingsWindow
from .speech import SpeechListener
from .tray import TrayApp, make_icon_image
from .vision import get_screen_resolution
from .window import MainWindow

logger = logging.getLogger("dota_voice.app")

# Vosk sometimes finalizes a command at a short pause ("начни рейтинговую
# игру" ... "на керри"); an unmatched piece is kept this long to be retried
# glued to the next one.
_JOIN_WINDOW_SEC = 3.0


class Application:
    def __init__(self):
        self.config = Config.load()
        self.commands_config = CommandsConfig.load()
        setup_logging(self.config)

        self.notifier = Notifier(self.config)
        self.notifier.warm_up()
        self.matcher = CommandMatcher(self.config, self.commands_config)
        self.executor = ActionExecutor(self.config, self.notifier)
        self.listener = SpeechListener(
            self.config, on_text=self._on_text_recognized, commands_config=self.commands_config
        )
        self.tray = TrayApp(
            self.config,
            self.listener,
            on_show_window=lambda: self.window.request_show(),
            on_exit=lambda: self.window.request_quit(),
        )
        icon_file = self.config.resolve_path("assets/app.ico")
        self.window = MainWindow(
            self.listener,
            on_state_changed=self.tray.refresh,
            hotkey=str(self.config.get("hotkeys", "toggle_listening", default="ctrl+alt+l")),
            icon_image=make_icon_image(True),
            icon_file=icon_file,
            on_open_settings=self._open_settings,
        )
        self._settings: SettingsWindow | None = None

        self._busy_lock = threading.Lock()
        self._pending_text = ""
        self._pending_since = 0.0
        self._check_resolution()

    def _open_settings(self) -> None:
        if self._settings is not None and self._settings.alive:
            self._settings.focus()
            return
        self._settings = SettingsWindow(self.window, self.config, self.listener, self.notifier, self.tray)

    def _check_resolution(self) -> None:
        configured = tuple(self.config.get("vision", "calibrated_resolution", default=[0, 0]))
        try:
            current = get_screen_resolution()
        except Exception:
            logger.exception("Failed to determine the current screen resolution")
            return

        if configured == (0, 0):
            logger.info("No calibration screen resolution set in config.yaml (current: %sx%s).", *current)
            return

        if tuple(configured) != current:
            logger.warning(
                "Current screen resolution %sx%s differs from the one the "
                "templates were calibrated for (%sx%s). Clicks on the Dota 2 UI "
                "may fail to find elements. Recalibrate: python tools/calibrate.py --check-resolution",
                current[0], current[1], configured[0], configured[1],
            )
        else:
            logger.info("Screen resolution %sx%s matches the template calibration.", *current)

    def _on_text_recognized(self, text: str) -> None:
        now = time.monotonic()
        pending = self._pending_text if now - self._pending_since <= _JOIN_WINDOW_SEC else ""
        spoken = text
        matched = self.matcher.match(text)
        if matched is None and pending:
            spoken = f"{pending} {text}"
            matched = self.matcher.match(spoken)
        if matched is None:
            logger.debug("Phrase did not match any command: '%s'", text)
            # Only the last piece is kept - chaining more would let ordinary
            # chatter slowly assemble into a command.
            self._pending_text = text
            self._pending_since = now
            return
        self._pending_text = ""

        command, params = matched

        if command.get("control") == "stop":
            self._handle_stop()
            return

        if not self._busy_lock.acquire(blocking=False):
            logger.warning("Another command is already running, ignoring new command '%s'.", command.get("id"))
            self.notifier.speak("Дождитесь завершения текущей команды")
            return

        self.window.show_command(spoken, "running")

        def _run():
            outcome = "failed"
            try:
                outcome = self.executor.run_steps(command.get("steps", []), params)
            except Exception:
                logger.exception("Command '%s' crashed", command.get("id"))
            finally:
                self._busy_lock.release()
                self.window.show_command(spoken, outcome)

        threading.Thread(target=_run, daemon=True).start()

    def _handle_stop(self) -> None:
        self.executor.request_stop()
        if self._busy_lock.locked():
            logger.info("Stop command received, interrupting the running sequence.")
            self.notifier.speak("Останавливаю")
        else:
            logger.debug("Stop command received, but nothing was running.")

    def run(self) -> None:
        logger.info("Starting Dota2 Voice Command Assistant")
        self.listener.start()
        self.tray.start()
        try:
            self.window.run()
        finally:
            self.listener.stop()
            self.tray.stop()
            logger.info("Application stopped.")
