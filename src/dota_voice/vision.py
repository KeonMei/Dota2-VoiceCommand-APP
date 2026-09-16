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
    width: int = 0
    height: int = 0


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
        width=int(tw),
        height=int(th),
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


@dataclass
class CheckboxPatches:
    plain: np.ndarray
    ticked: np.ndarray
    plain_offset: tuple[int, int]
    ticked_offset: tuple[int, int]


def checkbox_patches(plain_path: Path, ticked_path: Path) -> CheckboxPatches | None:
    """Finds where an unticked/ticked template pair actually differs (the
    checkbox) and cuts that spot out of both. The pair is aligned first, since
    two hand-dragged crops are rarely pixel-identical. Offsets are the patch's
    top-left inside each template. None if the pair can't be compared."""
    plain = _load_template(plain_path)
    ticked = _load_template(ticked_path)
    if plain is None or ticked is None:
        return None

    # Align on the right part of the pair (the label) - the checkbox sits on
    # the left and differs by design, so it would only skew the alignment.
    margin = 4
    x_from = ticked.shape[1] * 35 // 100
    core = ticked[margin:-margin, x_from:-margin]
    if core.shape[0] < 4 or core.shape[1] < 4 or core.shape[0] > plain.shape[0] or core.shape[1] > plain.shape[1]:
        return None
    _, _, _, loc = cv2.minMaxLoc(cv2.matchTemplate(plain, core, cv2.TM_CCOEFF_NORMED))
    # ticked[y, x] lines up with plain[y + dy, x + dx]
    dx, dy = loc[0] - x_from, loc[1] - margin

    x0, y0 = max(0, dx), max(0, dy)
    x1 = min(plain.shape[1], ticked.shape[1] + dx)
    y1 = min(plain.shape[0], ticked.shape[0] + dy)
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    plain_gray = cv2.cvtColor(plain[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY).astype(np.int16)
    ticked_gray = cv2.cvtColor(ticked[y0 - dy:y1 - dy, x0 - dx:x1 - dx], cv2.COLOR_BGR2GRAY).astype(np.int16)
    # The tick is a solid light fill. Label pixels that don't line up exactly
    # (sub-pixel text rendering) only leave thin slivers - opening drops them.
    mask = ((ticked_gray - plain_gray) > 60).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))

    count, _, stats, _ = cv2.connectedComponentsWithStats(mask)
    if count < 2:
        logger.warning("No tick found between %s and %s", plain_path.name, ticked_path.name)
        return None
    largest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    bx, by, bw, bh = (int(v) for v in stats[largest, :4])
    pad = 5  # take in the checkbox border around the fill
    bx0 = max(0, bx - pad) + x0
    by0 = max(0, by - pad) + y0
    bx1 = min(x1 - x0, bx + bw + pad) + x0
    by1 = min(y1 - y0, by + bh + pad) + y0
    return CheckboxPatches(
        plain=plain[by0:by1, bx0:bx1],
        ticked=ticked[by0 - dy:by1 - dy, bx0 - dx:bx1 - dx],
        plain_offset=(bx0, by0),
        ticked_offset=(bx0 - dx, by0 - dy),
    )


def checkbox_is_ticked(screenshot: np.ndarray, top_left: tuple[int, int], patches: CheckboxPatches) -> bool | None:
    """Compares the screenshot around the expected checkbox spot with the
    unticked and ticked patches; whichever is closer wins. None if the spot
    falls outside the screenshot."""
    slack = 4
    h, w = patches.plain.shape[:2]
    x0, y0 = top_left[0] - slack, top_left[1] - slack
    x1, y1 = top_left[0] + w + slack, top_left[1] + h + slack
    if x0 < 0 or y0 < 0 or x1 > screenshot.shape[1] or y1 > screenshot.shape[0]:
        return None
    area = screenshot[y0:y1, x0:x1]
    plain_diff = cv2.minMaxLoc(cv2.matchTemplate(area, patches.plain, cv2.TM_SQDIFF))[0]
    ticked_diff = cv2.minMaxLoc(cv2.matchTemplate(area, patches.ticked, cv2.TM_SQDIFF))[0]
    return ticked_diff < plain_diff
