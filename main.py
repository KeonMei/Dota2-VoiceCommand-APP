import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from dota_voice.app import Application  # noqa: E402


def main() -> None:
    app = Application()
    app.run()


if __name__ == "__main__":
    main()
