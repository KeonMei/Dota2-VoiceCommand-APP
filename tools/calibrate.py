"""UI template calibration tool for the "ranked game" command.

Usage:
    python tools/calibrate.py --check-resolution
    python tools/calibrate.py play_button
    python tools/calibrate.py ranked_roles_tab
    python tools/calibrate.py role_carry
    python tools/calibrate.py role_mid
    python tools/calibrate.py role_offlane
    python tools/calibrate.py role_support
    python tools/calibrate.py role_hard_support
    python tools/calibrate.py find_match_button

Checking the resolution:
    Before calibrating, it's worth confirming which screen resolution the
    templates are about to be saved for:
        python tools/calibrate.py --check-resolution
    This prints the current resolution alongside config.yaml ->
    vision.calibrated_resolution, and if they differ (or nothing is set yet)
    it updates config.yaml to the current value. The same check also runs
    automatically every time the main application starts (main.py) - if the
    screen doesn't match the calibration, a warning is logged/printed.

Steps:
    1. Open Dota 2 and get to the screen where the target element is visible
       (e.g. the main menu with the "Play" button).
    2. Run the script with the template name as an argument.
    3. You have 5 seconds to switch to the Dota 2 window (no need for
       Alt+Tab beforehand - the script takes a fullscreen screenshot itself).
    4. A fullscreen window with the screenshot appears. Drag a rectangle
       (press and hold the left mouse button, then drag) around the target
       button/icon - the tighter the crop, with as little background as
       possible, the more reliable the matching.
    5. Release the mouse button - the crop is saved to templates/<name>.png
       and the window closes automatically.

Templates are tied to the current screen resolution and Dota 2 UI scale.
Recalibrate whenever either of those changes.
"""
from __future__ import annotations

import re
import sys
import time
import tkinter as tk
from pathlib import Path

from PIL import Image, ImageGrab, ImageTk

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = PROJECT_ROOT / "templates"
CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"

sys.path.insert(0, str(PROJECT_ROOT / "src"))
from dota_voice.config import Config  # noqa: E402

COUNTDOWN_SECONDS = 5


def _get_configured_resolution() -> tuple[int, int]:
    config = Config.load(CONFIG_PATH)
    width, height = config.get("vision", "calibrated_resolution", default=[0, 0])
    return int(width), int(height)


def _write_configured_resolution(width: int, height: int) -> None:
    text = CONFIG_PATH.read_text(encoding="utf-8")
    new_text, count = re.subn(
        r"calibrated_resolution:\s*\[\s*\d+\s*,\s*\d+\s*\]",
        f"calibrated_resolution: [{width}, {height}]",
        text,
    )
    if count == 0:
        print("Could not find the 'calibrated_resolution' line in config.yaml - update it manually.")
        return
    CONFIG_PATH.write_text(new_text, encoding="utf-8")


def check_resolution(auto_update: bool) -> tuple[int, int]:
    current = ImageGrab.grab().size
    configured = _get_configured_resolution()

    print(f"Current screen resolution: {current[0]}x{current[1]}")
    print(f"config.yaml has:           {configured[0]}x{configured[1]}")

    if configured == (0, 0):
        print("No resolution set in config.yaml yet.")
        if auto_update:
            _write_configured_resolution(*current)
            print(f"config.yaml updated: calibrated_resolution: [{current[0]}, {current[1]}]")
    elif configured != current:
        print(
            "WARNING: the resolution differs from the one the templates were calibrated for.\n"
            "If you recently changed your screen resolution or the Dota 2 UI scale,\n"
            "recalibrate all 8 templates (see README, section 6)."
        )
        if auto_update:
            _write_configured_resolution(*current)
            print(f"config.yaml updated to the current value: [{current[0]}, {current[1]}]")
    else:
        print("Resolution matches the calibration - all good.")

    return current


class RegionSelector:
    def __init__(self, screenshot: Image.Image, output_path: Path):
        self.screenshot = screenshot
        self.output_path = output_path
        self.start_x = self.start_y = 0
        self.rect_id = None

        self.root = tk.Tk()
        self.root.attributes("-fullscreen", True)
        self.root.attributes("-topmost", True)
        self.root.title("Drag a rectangle over the template (left mouse button, Esc to cancel)")

        self.tk_image = ImageTk.PhotoImage(screenshot)
        self.canvas = tk.Canvas(self.root, cursor="cross", width=screenshot.width, height=screenshot.height)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_image(0, 0, image=self.tk_image, anchor="nw")

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.root.bind("<Escape>", lambda e: self.root.destroy())

    def _on_press(self, event):
        self.start_x, self.start_y = event.x, event.y
        if self.rect_id:
            self.canvas.delete(self.rect_id)
        self.rect_id = self.canvas.create_rectangle(
            self.start_x, self.start_y, self.start_x, self.start_y, outline="#00ff00", width=2
        )

    def _on_drag(self, event):
        self.canvas.coords(self.rect_id, self.start_x, self.start_y, event.x, event.y)

    def _on_release(self, event):
        x0, y0 = min(self.start_x, event.x), min(self.start_y, event.y)
        x1, y1 = max(self.start_x, event.x), max(self.start_y, event.y)

        if x1 - x0 < 5 or y1 - y0 < 5:
            print("The selected area is too small, try again (press and drag the left mouse button).")
            return

        cropped = self.screenshot.crop((x0, y0, x1, y1))
        TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
        cropped.save(self.output_path)
        print(f"Saved: {self.output_path} ({cropped.width}x{cropped.height}px)")
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)

    if sys.argv[1] in ("--check-resolution", "-r"):
        check_resolution(auto_update=True)
        return

    template_name = sys.argv[1]
    output_path = TEMPLATES_DIR / f"{template_name}.png"

    check_resolution(auto_update=False)
    print(f"Template: {template_name}")
    print(f"Switch to the Dota 2 window. The screenshot will be taken in {COUNTDOWN_SECONDS} seconds...")
    for remaining in range(COUNTDOWN_SECONDS, 0, -1):
        print(f"  {remaining}...")
        time.sleep(1)

    screenshot = ImageGrab.grab()
    print("Screenshot taken. Drag a rectangle over the element in the window that just opened.")

    selector = RegionSelector(screenshot, output_path)
    selector.run()


if __name__ == "__main__":
    main()
