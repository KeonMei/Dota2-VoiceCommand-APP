from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


class Config:
    def __init__(self, data: dict):
        self._data = data

    @classmethod
    def load(cls, path: str | Path = PROJECT_ROOT / "config" / "config.yaml") -> "Config":
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        return cls(_expand(raw))

    def get(self, *keys: str, default: Any = None) -> Any:
        node = self._data
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    def resolve_path(self, relative_or_abs: str) -> Path:
        p = Path(relative_or_abs)
        return p if p.is_absolute() else (PROJECT_ROOT / p)

    @property
    def raw(self) -> dict:
        return self._data


class CommandsConfig:
    def __init__(self, data: dict):
        self._data = data

    @classmethod
    def load(cls, path: str | Path = PROJECT_ROOT / "config" / "commands.yaml") -> "CommandsConfig":
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        return cls(raw or {})

    @property
    def commands(self) -> list[dict]:
        return self._data.get("commands", [])
