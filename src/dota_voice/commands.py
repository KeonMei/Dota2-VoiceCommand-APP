from __future__ import annotations

import logging
import re
from typing import Any

from rapidfuzz import fuzz

from .config import CommandsConfig, Config

logger = logging.getLogger("dota_voice.commands")

_PUNCT_RE = re.compile(r"[^\w\sа-яёА-ЯЁ]", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")
# token_set_ratio scores 100 whenever the spoken words are a subset of a
# phrase, so "обычная игра" alone would fully match "обычная игра турбо".
# Every meaningful phrase word must therefore also be (fuzzily) present in
# what was said; very short words ("на") are too noisy to require.
_TOKEN_MATCH_THRESHOLD = 75
_MIN_REQUIRED_TOKEN_LEN = 3


def normalize(text: str) -> str:
    text = text.lower().strip().replace("ё", "е")
    text = _PUNCT_RE.sub(" ", text)
    text = _SPACE_RE.sub(" ", text).strip()
    return text


def covers_phrase(phrase_norm: str, spoken_norm: str) -> bool:
    spoken_tokens = spoken_norm.split()
    for token in phrase_norm.split():
        if len(token) < _MIN_REQUIRED_TOKEN_LEN:
            continue
        if not any(fuzz.ratio(token, spoken) >= _TOKEN_MATCH_THRESHOLD for spoken in spoken_tokens):
            return False
    return True


def phrase_score(phrase: str, spoken_norm: str) -> int:
    phrase_norm = normalize(phrase)
    if not covers_phrase(phrase_norm, spoken_norm):
        return 0
    return int(fuzz.token_set_ratio(phrase_norm, spoken_norm))


class CommandMatcher:
    def __init__(self, config: Config, commands_config: CommandsConfig):
        self.commands = commands_config.commands
        self.roles: dict[str, dict] = config.get("roles", default={}) or {}

    def match(self, text: str) -> tuple[dict, dict[str, Any]] | None:
        norm = normalize(text)
        if not norm:
            return None

        best: tuple[int, dict, dict[str, Any]] | None = None
        for cmd in self.commands:
            threshold = int(cmd.get("match_threshold", 80))
            if cmd.get("role_param"):
                result = self._match_role_command(cmd, norm, threshold)
            else:
                result = self._match_plain_command(cmd, norm, threshold)
            if result and (best is None or result[0] > best[0]):
                best = result

        if best is None:
            logger.debug("No command matched for phrase: '%s'", text)
            return None

        score, cmd, params = best
        logger.info("Matched command '%s' (score=%d) from phrase '%s'", cmd.get("id"), score, text)
        return cmd, params

    def _match_plain_command(self, cmd: dict, norm: str, threshold: int) -> tuple[int, dict, dict] | None:
        best_score = 0
        for phrase in cmd.get("phrases", []):
            score = phrase_score(phrase, norm)
            best_score = max(best_score, score)
        if best_score >= threshold:
            return best_score, cmd, dict(cmd.get("params") or {})
        return None

    def _match_role_command(self, cmd: dict, norm: str, threshold: int) -> tuple[int, dict, dict] | None:
        best_score = -1
        best_synonym_len = -1
        best_cmd_params: tuple[dict, dict] | None = None

        for phrase in cmd.get("phrases", []):
            if "{role}" not in phrase:
                continue
            for role_id, role_data in self.roles.items():
                synonyms = role_data.get("synonyms") or [role_id]
                for synonym in synonyms:
                    score = phrase_score(phrase.replace("{role}", synonym), norm)
                    if score < threshold:
                        continue

                    synonym_len = len(normalize(synonym).split())
                    is_better = score > best_score + 1 or (
                        abs(score - best_score) <= 1 and synonym_len > best_synonym_len
                    )
                    if is_better:
                        best_score = score
                        best_synonym_len = synonym_len
                        params = {"role": role_id, "role_label": role_data.get("label", role_id)}
                        best_cmd_params = (cmd, params)

        if best_cmd_params is None:
            return None
        cmd_result, params = best_cmd_params
        return best_score, cmd_result, params
