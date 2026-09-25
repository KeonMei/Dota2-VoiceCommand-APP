from __future__ import annotations

import json
import os
import re
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


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(str(value), ensure_ascii=False)


def _write_value(path: Path, section: str, key: str, value: Any) -> None:
    """Rewrites only the `  key: ...` line of a top-level section (keeping its
    trailing comment), so the file's comments and layout stay as they are.
    A missing key is added at the end of its section."""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    key_re = re.compile(rf"^(  {re.escape(key)}:[ \t]*)(.*?)([ \t]+#.*)?(\r?\n)?$")
    new_value = _yaml_scalar(value)
    in_section, section_end, found = False, None, False
    for i, line in enumerate(lines):
        if line[:1] not in ("", " ", "\t", "#", "\n", "\r"):
            if in_section:
                break
            in_section = line.split(":", 1)[0] == section
            section_end = i + 1 if in_section else section_end
            continue
        if not in_section:
            continue
        if line.strip() and not line.startswith("#"):
            section_end = i + 1
        match = key_re.match(line)
        if match:
            lines[i] = match.group(1) + new_value + (match.group(3) or "") + (match.group(4) or "\n")
            found = True
            break
    if not found:
        if section_end is None:
            lines.append(f"\n{section}:\n")
            section_end = len(lines)
        lines.insert(section_end, f"  {key}: {new_value}\n")
    path.write_text("".join(lines), encoding="utf-8")


class Config:
    def __init__(self, data: dict, path: Path | None = None):
        self._data = data
        self._path = path

    @classmethod
    def load(cls, path: str | Path = PROJECT_ROOT / "config" / "config.yaml") -> "Config":
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        return cls(_expand(raw), Path(path))

    def set(self, section: str, key: str, value: Any) -> None:
        """Changes a value in memory only; see save()."""
        self._data.setdefault(section, {})[key] = value

    def save(self, section: str, key: str) -> None:
        """Writes the in-memory value of section.key to config.yaml."""
        _write_value(self._path, section, key, self.get(section, key))

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
