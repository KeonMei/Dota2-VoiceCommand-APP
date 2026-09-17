import os
import sys
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

# Under pythonw.exe (the desktop shortcut) there is no console and
# sys.stdout/sys.stderr are None - any library writing to them would crash.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

import win32api  # noqa: E402
import win32con  # noqa: E402
import win32event  # noqa: E402
import winerror  # noqa: E402

APP_TITLE = "Dota2 Voice Assistant"
_MUTEX_NAME = "Local\\Dota2VoiceCommandAssistant"


def _message_box(text: str, icon: int) -> None:
    win32api.MessageBox(0, text, APP_TITLE, icon | win32con.MB_SETFOREGROUND)


def main() -> None:
    # Two instances would both listen to the mic and run every command twice.
    mutex = win32event.CreateMutex(None, False, _MUTEX_NAME)  # noqa: F841 - held for the process lifetime
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        _message_box("Ассистент уже запущен - его значок в трее.", win32con.MB_ICONINFORMATION)
        return

    try:
        from dota_voice.app import Application

        Application().run()
    except Exception:
        details = traceback.format_exc()
        crash_log = PROJECT_ROOT / "logs" / "startup_error.log"
        crash_log.parent.mkdir(parents=True, exist_ok=True)
        crash_log.write_text(details, encoding="utf-8")
        _message_box(
            f"Не удалось запустить ассистента:\n\n{details.strip().splitlines()[-1]}\n\n"
            f"Подробности: {crash_log}",
            win32con.MB_ICONERROR,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
