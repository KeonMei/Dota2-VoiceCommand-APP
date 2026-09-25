from __future__ import annotations

import json
import logging
import math
import queue
import threading
from pathlib import Path
from typing import Callable

import itertools

import numpy as np
import sounddevice as sd
import vosk

from .config import CommandsConfig, Config

logger = logging.getLogger("dota_voice.speech")

vosk.SetLogLevel(-1)

_UNKNOWN = "[unk]"


def _loudness(data: bytes) -> float:
    """0..1 on a dB scale: the room's noise floor (~-55 dBFS) maps to 0,
    loud close speech (~-20 dBFS) to 1."""
    samples = np.frombuffer(data, dtype=np.int16).astype(np.float32)
    rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0
    db = 20 * math.log10(max(rms, 1.0) / 32768)
    return min(1.0, max(0.0, (db + 55) / 35))


def _mme_hostapi() -> int | None:
    return next((i for i, api in enumerate(sd.query_hostapis()) if api["name"] == "MME"), None)


def input_devices() -> list[str]:
    """Microphone names as Windows lists them (MME host API - the one that
    accepts the 16 kHz stream Vosk needs), without the system "sound mapper"
    pseudo-device."""
    mme = _mme_hostapi()
    names = []
    for device in sd.query_devices():
        if device["max_input_channels"] > 0 and device["hostapi"] == mme and not device["name"].endswith(" - Input"):
            names.append(device["name"])
    return names


def _device_index(device: str | int | None) -> int | None:
    """config.yaml stores the microphone by name (indexes shift when devices
    are plugged in); an int is still accepted as an index."""
    if device is None or isinstance(device, int):
        return device
    mme = _mme_hostapi()
    for index, info in enumerate(sd.query_devices()):
        if info["name"] == device and info["hostapi"] == mme and info["max_input_channels"] > 0:
            return index
    logger.warning("Microphone '%s' not found - using the system default.", device)
    return None


def _in_vocabulary(model: vosk.Model, word: str) -> bool:
    return model.vosk_model_find_word(word) >= 0


def _vocabulary_form(model: vosk.Model, word: str) -> str | None:
    """The model's spelling of a word - it spells "ё" out, while phrases in
    the configs are usually typed with "е"."""
    if _in_vocabulary(model, word):
        return word
    spots = [i for i, ch in enumerate(word) if ch == "е"][:4]
    for count in range(1, len(spots) + 1):
        for chosen in itertools.combinations(spots, count):
            variant = "".join("ё" if i in chosen else ch for i, ch in enumerate(word))
            if _in_vocabulary(model, variant):
                return variant
    return None


def command_words(config: Config, commands_config: CommandsConfig) -> set[str]:
    from .commands import normalize

    words: set[str] = set()
    roles = config.get("roles", default={}) or {}
    for command in commands_config.commands:
        for phrase in command.get("phrases", []):
            if "{role}" in phrase:
                for role in roles.values():
                    for synonym in role.get("synonyms", []):
                        words.update(normalize(phrase.replace("{role}", synonym)).split())
            else:
                words.update(normalize(phrase).split())
    words.update(normalize(" ".join(config.get("speech", "extra_words", default=[]) or [])).split())
    return words


def build_grammar(config: Config, commands_config: CommandsConfig, model: vosk.Model) -> str:
    """Vosk grammar limited to the words the commands use, plus "[unk]" for
    everything else. The recognizer then can't turn "турбо" into a similar
    everyday word, and unrelated speech comes out as [unk]."""
    grammar: set[str] = set()
    skipped = []
    for word in sorted(command_words(config, commands_config)):
        form = _vocabulary_form(model, word)
        if form is None:
            skipped.append(word)
        else:
            grammar.add(form)
    if skipped:
        logger.info(
            "Not in the speech model's vocabulary (matched only via similar words): %s", ", ".join(skipped)
        )
    return json.dumps(sorted(grammar) + [_UNKNOWN], ensure_ascii=False)


class SpeechListener:
    def __init__(self, config: Config, on_text: Callable[[str], None], commands_config: CommandsConfig | None = None):
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
        self._grammar: str | None = None
        if commands_config is not None and config.get("speech", "restrict_vocabulary", default=True):
            self._grammar = build_grammar(config, commands_config, self._model)
            logger.info("Recognition limited to %d command words.", len(json.loads(self._grammar)) - 1)
        self._audio_queue: queue.Queue[bytes] = queue.Queue()
        self._stream: sd.RawInputStream | None = None
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self._enabled = threading.Event()
        self._enabled.set()
        self.level = 0.0

    def start(self) -> None:
        if self._running.is_set():
            return
        self._running.set()
        self._open_stream()
        self._thread = threading.Thread(target=self._recognize_loop, daemon=True)
        self._thread.start()
        logger.info("Microphone listening started.")

    def stop(self) -> None:
        self._running.clear()
        self._close_stream()
        logger.info("Microphone listening stopped.")

    @property
    def input_device(self) -> str | int | None:
        return self._device

    def set_input_device(self, device: str | int | None) -> None:
        """Switches the microphone on the fly. Raises if the device can't be
        opened - the previous device is restored in that case."""
        previous = self._device
        self._device = device
        if not self._running.is_set():
            return
        self._close_stream()
        try:
            self._open_stream()
        except Exception:
            self._device = previous
            self._open_stream()
            raise
        logger.info("Microphone switched to: %s", device or "system default")

    def _open_stream(self) -> None:
        self._stream = sd.RawInputStream(
            samplerate=self._sample_rate,
            # 0.1 s blocks keep the window's level meter responsive; Vosk
            # decodes the same stream regardless of the chunking.
            blocksize=self._sample_rate // 10,
            device=_device_index(self._device),
            dtype="int16",
            channels=1,
            callback=self._audio_callback,
        )
        self._stream.start()

    def _close_stream(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        self.level = 0.0

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
        data = bytes(indata)
        self.level = _loudness(data)
        self._audio_queue.put(data)

    def _recognize_loop(self) -> None:
        if self._grammar is not None:
            recognizer = vosk.KaldiRecognizer(self._model, self._sample_rate, self._grammar)
        else:
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
                text = " ".join(w for w in (result.get("text") or "").split() if w != _UNKNOWN)
                if text:
                    logger.debug("Recognized: %s", text)
                    try:
                        self._on_text(text)
                    except Exception:
                        logger.exception("Error in recognized-text handler")
