from __future__ import annotations

import logging
import threading

import pyttsx3
import winsound

from .config import Config

logger = logging.getLogger("dota_voice.notify")


def find_voice_id(engine: pyttsx3.Engine, name_substr: str) -> str | None:
    name_substr = name_substr.lower()
    for voice in engine.getProperty("voices"):
        haystack = f"{voice.id} {voice.name}".lower()
        if name_substr in haystack:
            return voice.id
    return None


class Notifier:
    def __init__(self, config: Config):
        self._enabled = bool(config.get("feedback", "tts_enabled", default=True))
        self._rate = int(config.get("feedback", "tts_rate", default=175))
        self._volume = float(config.get("feedback", "tts_volume", default=1.0))
        self._voice_substr = config.get("feedback", "tts_voice", default=None)
        self._beep_enabled = bool(config.get("feedback", "beep_on_command_recognized", default=True))
        self._lock = threading.Lock()
        self._resolved_voice_id: str | None = None
        self._voice_resolved = False

    def _resolve_voice(self, engine: pyttsx3.Engine) -> str | None:
        if self._voice_resolved:
            return self._resolved_voice_id
        self._voice_resolved = True
        if not self._voice_substr:
            return None
        voice_id = find_voice_id(engine, str(self._voice_substr))
        if voice_id is None:
            logger.warning(
                "No installed TTS voice matched '%s' - using the system default. "
                "Run 'python tools/list_voices.py' to see available voices.",
                self._voice_substr,
            )
        self._resolved_voice_id = voice_id
        return voice_id

    def _speak_sync(self, text: str) -> None:
        try:
            engine = pyttsx3.init()
            engine.setProperty("rate", self._rate)
            engine.setProperty("volume", self._volume)
            voice_id = self._resolve_voice(engine)
            if voice_id:
                engine.setProperty("voice", voice_id)
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
