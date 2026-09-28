from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

from .config import CommandsConfig, Config
from .logging_setup import setup_logging
from .tray import TrayApp, make_icon_image
from .window import MainWindow

if TYPE_CHECKING:
    from .settings_window import SettingsWindow

logger = logging.getLogger("dota_voice.app")

# Vosk sometimes finalizes a command at a short pause ("начни рейтинговую
# игру" ... "на керри"); an unmatched piece is kept this long to be retried
# glued to the next one.
_JOIN_WINDOW_SEC = 3.0


class Application:
    """The window comes up first; speech recognition, command matching and
    UI automation (the slow imports and the Vosk model) load in the
    background while it shows "Загрузка…"."""

    def __init__(self):
        self._started = time.perf_counter()
        self.config = Config.load()
        self.commands_config = CommandsConfig.load()
        setup_logging(self.config)

        self.notifier = None
        self.matcher = None
        self.executor = None
        self.listener = None
        self.tray: TrayApp | None = None
        self._startup_error: BaseException | None = None
        self.window = MainWindow(
            None,
            on_state_changed=lambda: self.tray.refresh() if self.tray else None,
            hotkey=str(self.config.get("hotkeys", "toggle_listening", default="ctrl+alt+l")),
            icon_image=make_icon_image(True),
            icon_file=self.config.resolve_path("assets/app.ico"),
            on_open_settings=self._open_settings,
            wake_word=self.active_wake_word(),
        )
        self._settings: SettingsWindow | None = None

        self._busy_lock = threading.Lock()
        self._pending_text = ""
        self._pending_since = 0.0
        self._armed_until = 0.0

    def _load(self) -> None:
        """Runs on a background thread; the window stays responsive meanwhile."""
        try:
            # The Vosk model loads in native code without holding the GIL, so
            # it overlaps with the slow imports below.
            speech: dict = {}

            def _load_speech() -> None:
                try:
                    from .speech import SpeechListener

                    speech["listener"] = SpeechListener(
                        self.config, on_text=self._on_text_recognized, commands_config=self.commands_config
                    )
                except Exception as exc:
                    speech["error"] = exc

            speech_thread = threading.Thread(target=_load_speech, name="speech-model", daemon=True)
            speech_thread.start()

            from .actions import ActionExecutor
            from .commands import CommandMatcher
            from .notify import Notifier

            self.notifier = Notifier(self.config)
            self.matcher = CommandMatcher(self.config, self.commands_config)
            self.executor = ActionExecutor(self.config, self.notifier)
            speech_thread.join()
            if "error" in speech:
                raise speech["error"]
            listener = speech["listener"]
            listener.start()
            self.tray = TrayApp(
                self.config,
                listener,
                on_show_window=lambda: self.window.request_show(),
                on_exit=lambda: self.window.request_quit(),
            )
            self.tray.start()
            self.listener = self.window.listener = listener
            logger.info("Ready in %.1fs.", time.perf_counter() - self._started)
            # Loading the voice model is CPU-heavy - only now, so it doesn't slow the start.
            self.notifier.warm_up()
            self._check_resolution()
        except Exception as exc:
            logger.exception("Startup failed")
            self._startup_error = exc
            self.window.request_quit()

    def _open_settings(self) -> None:
        if self.listener is None:
            return
        if self._settings is not None and self._settings.alive:
            self._settings.focus()
            return
        from .settings_window import SettingsWindow

        self._settings = SettingsWindow(self.window, self.config, self.listener, self.notifier, self.tray)

    def _check_resolution(self) -> None:
        from .vision import get_screen_resolution

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

    def active_wake_word(self) -> str | None:
        if not self.config.get("speech", "wake_word_required", default=False):
            return None
        return str(self.config.get("speech", "wake_word", default="оракул"))

    def _strip_wake_word(self, text: str, now: float) -> str:
        """What follows the wake word, or the whole phrase while the wake
        window is open; "" means the phrase is ignored."""
        from .commands import normalize

        wake = normalize(str(self.config.get("speech", "wake_word", default="оракул")))
        words = normalize(text).split()
        if wake in words:
            rest = words[len(words) - words[::-1].index(wake):]
            self._pending_text = ""
            self._set_armed_until(now + float(self.config.get("speech", "wake_window_sec", default=6)))
            if not rest:
                logger.info("Wake word heard, waiting for a command.")
                self.notifier.chime()
            return " ".join(rest)
        if now <= self._armed_until:
            return text
        logger.debug("No wake word, ignoring: '%s'", text)
        return ""

    def _set_armed_until(self, deadline: float) -> None:
        self._armed_until = deadline
        self.window.armed_until = deadline

    def _on_text_recognized(self, text: str) -> None:
        now = time.monotonic()
        if self.config.get("speech", "wake_word_required", default=False):
            text = self._strip_wake_word(text, now)
            if not text:
                return
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
        self._set_armed_until(0.0)

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
        # Paint the window before the loader's imports start competing for the GIL.
        self.window.root.update()
        threading.Thread(target=self._load, name="startup", daemon=True).start()
        try:
            self.window.run()
        finally:
            if self.listener is not None:
                self.listener.stop()
            if self.tray is not None:
                self.tray.stop()
            logger.info("Application stopped.")
        if self._startup_error is not None:
            raise self._startup_error
