from __future__ import annotations

import logging
import threading

import pyttsx3
import winsound

from .config import Config

logger = logging.getLogger("dota_voice.notify")


class Notifier:
    def __init__(self, config: Config):
        self._enabled = bool(config.get("feedback", "tts_enabled", default=True))
        self._rate = int(config.get("feedback", "tts_rate", default=175))
        self._beep_enabled = bool(config.get("feedback", "beep_on_command_recognized", default=True))
        self._lock = threading.Lock()

    def _speak_sync(self, text: str) -> None:
        try:
            engine = pyttsx3.init()
            engine.setProperty("rate", self._rate)
            engine.say(text)
            engine.runAndWait()
            engine.stop()
        except Exception:
            logger.exception("Failed to speak phrase via TTS: %s", text)

    def speak(self, text: str) -> None:
        logger.info("[TTS] %s", text)
        if not self._enabled:
            return
        with self._lock:
            self._speak_sync(text)

    def beep(self, ok: bool = True) -> None:
        if not self._beep_enabled:
            return
        try:
            if ok:
                winsound.Beep(880, 120)
            else:
                winsound.Beep(300, 250)
        except RuntimeError:
            pass
