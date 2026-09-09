from __future__ import annotations

import logging
import threading

from .actions import ActionExecutor
from .commands import CommandMatcher
from .config import CommandsConfig, Config
from .logging_setup import setup_logging
from .notify import Notifier
from .speech import SpeechListener
from .tray import TrayApp
from .vision import get_screen_resolution

logger = logging.getLogger("dota_voice.app")


class Application:
    def __init__(self):
        self.config = Config.load()
        self.commands_config = CommandsConfig.load()
        setup_logging(self.config)

        self.notifier = Notifier(self.config)
        self.matcher = CommandMatcher(self.config, self.commands_config)
        self.executor = ActionExecutor(self.config, self.notifier)
        self.listener = SpeechListener(self.config, on_text=self._on_text_recognized)
        self.tray = TrayApp(self.config, self.listener)

        self._busy_lock = threading.Lock()
        self._check_resolution()

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
        matched = self.matcher.match(text)
        if matched is None:
            logger.debug("Phrase did not match any command: '%s'", text)
            return

        command, params = matched

        if not self._busy_lock.acquire(blocking=False):
            logger.warning("Another command is already running, ignoring new command '%s'.", command.get("id"))
            self.notifier.speak("Дождитесь завершения текущей команды")
            return

        def _run():
            try:
                self.executor.run_steps(command.get("steps", []), params)
            finally:
                self._busy_lock.release()

        threading.Thread(target=_run, daemon=True).start()

    def run(self) -> None:
        logger.info("Starting Dota2 Voice Command Assistant")
        self.listener.start()
        try:
            self.tray.run()
        finally:
            self.listener.stop()
            logger.info("Application stopped.")
