from __future__ import annotations

import io
import logging
import threading
import wave

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
        self._volume = float(config.get("feedback", "tts_volume", default=0.55))
        self._voice_substr = config.get("feedback", "tts_voice", default=None)
        self._beep_enabled = bool(config.get("feedback", "beep_on_command_recognized", default=True))
        self._engine = str(config.get("feedback", "tts_engine", default="sapi")).lower()
        piper_model = config.get("feedback", "piper_model", default=None)
        self._piper_model_path = config.resolve_path(piper_model) if piper_model else None
        self._piper_length_scale = float(config.get("feedback", "piper_length_scale", default=1.0))
        self._piper_voice = None
        self._piper_failed = False
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

    def _load_piper_voice(self):
        if self._piper_voice is not None or self._piper_failed:
            return self._piper_voice
        if self._piper_model_path is None or not self._piper_model_path.exists():
            self._piper_failed = True
            logger.warning(
                "Piper model not found (%s) - using the SAPI voice. See feedback.piper_model in config.yaml.",
                self._piper_model_path,
            )
            return None
        try:
            from piper import PiperVoice

            self._piper_voice = PiperVoice.load(self._piper_model_path)
            logger.info("Loaded Piper voice: %s", self._piper_model_path.name)
        except Exception:
            self._piper_failed = True
            logger.exception("Failed to load the Piper voice - falling back to the SAPI voice.")
        return self._piper_voice

    def _speak_piper(self, text: str) -> bool:
        voice = self._load_piper_voice()
        if voice is None:
            return False
        try:
            from piper import SynthesisConfig

            syn_config = SynthesisConfig(length_scale=self._piper_length_scale, volume=self._volume)
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as wav_file:
                voice.synthesize_wav(text, wav_file, syn_config=syn_config)
            winsound.PlaySound(buffer.getvalue(), winsound.SND_MEMORY)
            return True
        except Exception:
            logger.exception("Piper failed to speak phrase, falling back to SAPI: %s", text)
            return False

    def warm_up(self) -> None:
        # Loading the Piper model takes a moment - do it ahead of the first phrase.
        if self._enabled and self._engine == "piper":
            threading.Thread(target=self._warm_up_sync, daemon=True).start()

    def _warm_up_sync(self) -> None:
        with self._lock:
            self._load_piper_voice()

    def _speak_sync(self, text: str) -> None:
        if self._engine == "piper" and self._speak_piper(text):
            return
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
            winsound.MessageBeep(winsound.MB_OK if ok else winsound.MB_ICONHAND)
        except RuntimeError:
            pass
