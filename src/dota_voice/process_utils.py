from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path

import psutil
import win32gui

logger = logging.getLogger("dota_voice.process")


class ProcessError(RuntimeError):
    pass


def is_process_running(process_name: str) -> bool:
    process_name = process_name.lower()
    for proc in psutil.process_iter(["name"]):
        try:
            if (proc.info["name"] or "").lower() == process_name:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def find_window_by_title_substr(substr: str) -> int | None:
    substr_lower = substr.lower()
    found: list[int] = []

    def _enum_handler(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if title and substr_lower in title.lower():
            found.append(hwnd)

    win32gui.EnumWindows(_enum_handler, None)
    return found[0] if found else None


def wait_for_process(process_name: str, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_process_running(process_name):
            return True
        time.sleep(0.5)
    return False


def wait_for_window(title_substr: str, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if find_window_by_title_substr(title_substr) is not None:
            return True
        time.sleep(0.5)
    return False


def launch_process(path: str, args: list[str] | None = None, process_name: str | None = None) -> None:
    if process_name and is_process_running(process_name):
        logger.info("Process %s is already running, skipping launch.", process_name)
        return

    exe_path = Path(path)
    if not exe_path.exists():
        raise ProcessError(f"Исполняемый файл не найден: {path}")

    args = args or []
    try:
        subprocess.Popen([str(exe_path), *args], cwd=str(exe_path.parent))
        logger.info("Launched process: %s %s", exe_path, args)
    except OSError as exc:
        raise ProcessError(f"Не удалось запустить {path}: {exc}") from exc


def launch_uri(uri: str) -> None:
    logger.info("Opening URI: %s", uri)
    os.startfile(uri)  # noqa: S606
