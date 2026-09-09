from __future__ import annotations

import json
import logging
import queue
import threading
from pathlib import Path
from typing import Callable

import sounddevice as sd
import vosk

from .config import Config

logger = logging.getLogger("dota_voice.speech")

vosk.SetLogLevel(-1)


class SpeechListener:
    def __init__(self, config: Config, on_text: Callable[[str], None]):
        self._on_text = on_text
        self._sample_rate = int(config.get("speech", "sample_rate", default=16000))
        self._device = config.get("speech", "input_device", default=None)
        model_path = config.resolve_path(config.get("speech", "model_path", default="models/vosk-model-small-ru-0.22"))

        if not Path(model_path).exists():
            raise FileNotFoundError(
                f"Vosk model not found at: {model_path}. "
                "Download the offline Russian model from https://alphacephei.com/vosk/models "
                "and unpack it there (see README)."
            )

        self._model = vosk.Model(str(model_path))
        self._audio_queue: queue.Queue[bytes] = queue.Queue()
        self._stream: sd.RawInputStream | None = None
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._enabled = threading.Event()
        self._enabled.set()

    def start(self) -> None:
        if self._running.is_set():
            return
        self._running.set()
        self._stream = sd.RawInputStream(
            samplerate=self._sample_rate,
            blocksize=8000,
            device=self._device,
            dtype="int16",
            channels=1,
            callback=self._audio_callback,
        )
        self._stream.start()
        self._thread = threading.Thread(target=self._recognize_loop, daemon=True)
        self._thread.start()
        logger.info("Microphone listening started.")

    def stop(self) -> None:
        self._running.clear()
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        logger.info("Microphone listening stopped.")

    def set_enabled(self, enabled: bool) -> None:
        if enabled:
            self._enabled.set()
            logger.info("Command recognition enabled.")
        else:
            self._enabled.clear()
            logger.info("Command recognition paused.")

    def toggle_enabled(self) -> bool:
        new_state = not self._enabled.is_set()
        self.set_enabled(new_state)
        return new_state

    @property
    def is_enabled(self) -> bool:
        return self._enabled.is_set()

    def _audio_callback(self, indata, frames, time_info, status) -> None:
        if status:
            logger.debug("Audio status: %s", status)
        self._audio_queue.put(bytes(indata))

    def _recognize_loop(self) -> None:
        recognizer = vosk.KaldiRecognizer(self._model, self._sample_rate)
        while self._running.is_set():
            try:
                data = self._audio_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if not self._enabled.is_set():
                continue

            if recognizer.AcceptWaveform(data):
                result = json.loads(recognizer.Result())
                text = (result.get("text") or "").strip()
                if text:
                    logger.debug("Recognized: %s", text)
                    try:
                        self._on_text(text)
                    except Exception:
                        logger.exception("Error in recognized-text handler")
