"""Creates a desktop shortcut that starts the assistant without a console window.

Usage:
    python tools/create_shortcut.py

The shortcut runs main.py with pythonw.exe from the same Python installation
(or venv) this script is run with, so run it with the interpreter that has the
requirements installed. The icon is saved to assets/app.ico - the same green
dot as the tray icon. Re-running the script overwrites the shortcut, e.g.
after moving the project folder.
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import win32com.client  # noqa: E402

from dota_voice.tray import make_icon_image  # noqa: E402

SHORTCUT_NAME = "Dota2 Voice Assistant.lnk"


def _write_icon() -> Path:
    icon_path = PROJECT_ROOT / "assets" / "app.ico"
    icon_path.parent.mkdir(parents=True, exist_ok=True)
    make_icon_image(active=True).save(icon_path, sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
    return icon_path


def _pythonw() -> Path:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if pythonw.exists():
        return pythonw
    print(f"pythonw.exe not found next to {sys.executable} - the shortcut will open a console window.")
    return Path(sys.executable)


def main() -> None:
    shell = win32com.client.Dispatch("WScript.Shell")
    # Resolves OneDrive-redirected / localized desktop folders correctly.
    desktop = Path(shell.SpecialFolders("Desktop"))
    shortcut_path = desktop / SHORTCUT_NAME

    shortcut = shell.CreateShortCut(str(shortcut_path))
    shortcut.TargetPath = str(_pythonw())
    shortcut.Arguments = f'"{PROJECT_ROOT / "main.py"}"'
    shortcut.WorkingDirectory = str(PROJECT_ROOT)
    shortcut.IconLocation = f"{_write_icon()},0"
    shortcut.Description = "Голосовой ассистент для Dota 2"
    shortcut.Save()

    print(f"Shortcut created: {shortcut_path}")


if __name__ == "__main__":
    main()
