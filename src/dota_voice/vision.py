from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import cv2
import mss
import numpy as np

logger = logging.getLogger("dota_voice.vision")


@dataclass
class MatchResult:
    center_x: int
    center_y: int
    score: float


def get_screen_resolution() -> tuple[int, int]:
    with mss.mss() as sct:
        monitor = sct.monitors[1]
        return monitor["width"], monitor["height"]


def capture_screen(region: tuple[int, int, int, int] | None = None) -> np.ndarray:
    with mss.mss() as sct:
        monitor = sct.monitors[1]
        if region is not None:
            left, top, width, height = region
            monitor = {"left": left, "top": top, "width": width, "height": height}
        shot = sct.grab(monitor)
        img = np.array(shot)
        return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)


def find_template(
    template_path: Path,
    threshold: float = 0.86,
    region: tuple[int, int, int, int] | None = None,
) -> MatchResult | None:
    if not template_path.exists():
        logger.error("Template file not found on disk: %s", template_path)
        return None

    template = cv2.imread(str(template_path), cv2.IMREAD_COLOR)
    if template is None:
        logger.error("Failed to read template file: %s", template_path)
        return None

    screenshot = capture_screen(region)
    result = cv2.matchTemplate(screenshot, template, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)

    if max_val < threshold:
        logger.debug(
            "Template %s not found (score=%.3f < threshold=%.3f)",
            template_path.name, max_val, threshold,
        )
        return None

    th, tw = template.shape[:2]
    offset_x, offset_y = (region[0], region[1]) if region else (0, 0)
    center_x = offset_x + max_loc[0] + tw // 2
    center_y = offset_y + max_loc[1] + th // 2
    logger.debug("Template %s found: score=%.3f, center=(%d, %d)", template_path.name, max_val, center_x, center_y)
    return MatchResult(center_x=center_x, center_y=center_y, score=max_val)
