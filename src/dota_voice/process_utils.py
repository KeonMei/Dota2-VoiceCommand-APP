from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from pathlib import Path

import psutil
import win32api
import win32con
import win32gui
import win32process

logger = logging.getLogger("dota_voice.process")


class ProcessError(RuntimeError):
    pass


def is_process_running(process_name: str) -> bool:
    return bool(_process_ids(process_name))


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


def wait_for_process(process_name: str, timeout: float, stop_event: threading.Event | None = None) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if stop_event is not None and stop_event.is_set():
            return False
        if is_process_running(process_name):
            return True
        time.sleep(0.5)
    return False


def wait_for_window(title_substr: str, timeout: float, stop_event: threading.Event | None = None) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if stop_event is not None and stop_event.is_set():
            return False
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


def _process_ids(process_name: str) -> set[int]:
    process_name = process_name.lower()
    pids = set()
    for proc in psutil.process_iter(["name"]):
        try:
            if (proc.info["name"] or "").lower() == process_name:
                pids.add(proc.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return pids


def close_process(process_name: str, timeout: float, stop_event: threading.Event | None = None) -> bool:
    """Asks the program to quit the way its window's close button does
    (WM_CLOSE to its own top-level windows - matched by process, so a browser
    tab titled "Dota 2" is left alone) and waits for it to exit. Never kills
    it: a program that shows a quit confirmation stays open. Returns True
    once no such process is left."""
    pids = _process_ids(process_name)
    if not pids:
        return True

    def _post_close(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and win32process.GetWindowThreadProcessId(hwnd)[1] in pids:
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)

    win32gui.EnumWindows(_post_close, None)
    logger.info("Asked %s to close.", process_name)

    deadline = time.time() + timeout
    while time.time() < deadline:
        if stop_event is not None and stop_event.is_set():
            return False
        if not is_process_running(process_name):
            return True
        time.sleep(0.5)
    return False


def launch_uri(uri: str) -> None:
    logger.info("Opening URI: %s", uri)
    os.startfile(uri)  # noqa: S606


def restore_and_focus_window(hwnd: int) -> bool:
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        else:
            win32gui.ShowWindow(hwnd, win32con.SW_SHOW)

        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            # Windows blocks SetForegroundWindow from a background process unless
            # it recently had input focus. Sending a no-op key event resets that
            # internal timer - a well-known, harmless workaround.
            win32api.keybd_event(0, 0, 0, 0)
            win32gui.SetForegroundWindow(hwnd)
        return True
    except Exception:
        logger.warning("Failed to bring window %s to the foreground", hwnd)
        return False
