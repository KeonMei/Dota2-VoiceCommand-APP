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


def _load_template(template_path: Path) -> np.ndarray | None:
    if not template_path.exists():
        logger.error("Template file not found on disk: %s", template_path)
        return None
    template = cv2.imread(str(template_path), cv2.IMREAD_COLOR)
    if template is None:
        logger.error("Failed to read template file: %s", template_path)
    return template


def best_match(
    template_path: Path,
    region: tuple[int, int, int, int] | None = None,
    screenshot: np.ndarray | None = None,
) -> MatchResult | None:
    """The single best-scoring spot for the template, whatever its score -
    for comparing two near-identical templates against each other. None only
    if the template can't be loaded or doesn't fit in the screenshot."""
    template = _load_template(template_path)
    if template is None:
        return None
    if screenshot is None:
        screenshot = capture_screen(region)

    th, tw = template.shape[:2]
    if screenshot.shape[0] < th or screenshot.shape[1] < tw:
        return None
    result = cv2.matchTemplate(screenshot, template, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)

    offset_x, offset_y = (region[0], region[1]) if region else (0, 0)
    return MatchResult(
        center_x=int(offset_x + max_loc[0] + tw // 2),
        center_y=int(offset_y + max_loc[1] + th // 2),
        score=float(max_val),
    )


def find_template(
    template_path: Path,
    threshold: float = 0.86,
    region: tuple[int, int, int, int] | None = None,
) -> MatchResult | None:
    match = best_match(template_path, region)
    if match is None:
        return None

    if match.score < threshold:
        logger.debug(
            "Template %s not found (score=%.3f < threshold=%.3f)",
            template_path.name, match.score, threshold,
        )
        return None

    logger.debug(
        "Template %s found: score=%.3f, center=(%d, %d)",
        template_path.name, match.score, match.center_x, match.center_y,
    )
    return match
