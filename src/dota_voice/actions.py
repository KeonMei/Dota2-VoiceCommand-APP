from __future__ import annotations

import logging
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Callable

import pyautogui
import win32gui

from . import process_utils, vision
from .config import Config
from .notify import Notifier

logger = logging.getLogger("dota_voice.actions")

pyautogui.FAILSAFE = True
# pyautogui only updates the cursor position every MINIMUM_SLEEP seconds during
# a tweened move (default 0.05s = ~7 steps over 0.35s, which looks like a few
# jumps rather than a glide regardless of duration/easing). Lowering it lets a
# shorter move_duration still look smooth. PAUSE (default 0.1s) adds a fixed
# delay after every single pyautogui call, independent of the glide itself.
pyautogui.MINIMUM_SLEEP = 0.01
pyautogui.PAUSE = 0.05

_TWEENS = {
    "linear": pyautogui.linear,
    "ease_in_out": pyautogui.easeInOutQuad,
    "ease_out": pyautogui.easeOutQuad,
    "ease_in": pyautogui.easeInQuad,
}


class ActionError(RuntimeError):
    pass


class ActionExecutor:
    def __init__(self, config: Config, notifier: Notifier):
        self.config = config
        self.notifier = notifier
        self.templates_dir = config.resolve_path(
            config.get("vision", "templates_dir", default="templates")
        )
        self.match_threshold = float(config.get("vision", "match_threshold", default=0.86))
        self.default_retries = int(config.get("vision", "max_retries", default=5))
        self.retry_delay = float(config.get("vision", "retry_delay_sec", default=1.0))
        self.move_duration = float(config.get("automation", "move_duration", default=0.35))
        tween_name = str(config.get("automation", "move_tween", default="ease_in_out"))
        self.move_tween = _TWEENS.get(tween_name)
        if self.move_tween is None:
            logger.warning("Unknown automation.move_tween '%s', falling back to ease_in_out", tween_name)
            self.move_tween = pyautogui.easeInOutQuad
        self._stop_event = threading.Event()

        self._handlers: dict[str, Callable[[dict, dict], None]] = {
            "notify": self._h_notify,
            "launch_process": self._h_launch_process,
            "launch_uri": self._h_launch_uri,
            "wait_window": self._h_wait_window,
            "sleep": self._h_sleep,
            "open_url_in_chrome": self._h_open_url_in_chrome,
            "click_template": self._h_click_template,
            "click_point": self._h_click_point,
            "key_press": self._h_key_press,
            "select_exclusive_role": self._h_select_exclusive_role,
            "ensure_dota_ready": self._h_ensure_dota_ready,
            "parallel": self._h_parallel,
            "select_exclusive_mode": self._h_select_exclusive_mode,
            "open_section": self._h_open_section,
            "cancel_search": self._h_cancel_search,
            "close_process": self._h_close_process,
        }

    def request_stop(self) -> None:
        self._stop_event.set()

    def _interruptible_sleep(self, seconds: float) -> None:
        remaining = seconds
        step_size = 0.1
        while remaining > 0 and not self._stop_event.is_set():
            chunk = min(step_size, remaining)
            time.sleep(chunk)
            remaining -= chunk

    def run_steps(self, steps: list[dict], context: dict[str, Any]) -> str:
        """Returns "done", "stopped" (interrupted by a stop request) or "failed"."""
        self._stop_event.clear()
        try:
            completed = self._run_sequence(steps, context)
        except ActionError as exc:
            self.notifier.beep(ok=False)
            self.notifier.speak(f"Не удалось выполнить шаг: {exc}")
            return "failed"
        if not completed:
            logger.info("Step sequence stopped by voice command.")
            return "stopped"
        return "done"

    def _run_sequence(self, steps: list[dict], context: dict[str, Any]) -> bool:
        """Runs steps in order. Returns False if interrupted by a stop request,
        raises ActionError if a non-optional step fails."""
        for step in steps:
            if self._stop_event.is_set():
                return False

            step_type = step.get("type")
            handler = self._handlers.get(step_type)
            if handler is None:
                logger.error("Unknown step type: %s", step_type)
                continue

            try:
                handler(step, context)
            except ActionError as exc:
                logger.error("Step '%s' failed: %s", step_type, exc)
                if step.get("optional", False):
                    logger.warning("Step is marked optional, continuing.")
                    continue
                raise

            if self._stop_event.is_set():
                return False
        return True

    def _h_parallel(self, step: dict, context: dict) -> None:
        """Runs each branch (a list of steps) in its own thread; steps inside a
        branch still run in order, so dependencies (Steam -> Dota 2, Chrome ->
        Yandex Music tab) go in the same branch. A failed branch doesn't cancel
        the others - all branches are awaited, then the failures are reported
        together."""
        branches = step.get("branches") or []
        errors: list[str] = []
        errors_lock = threading.Lock()

        def _run_branch(index: int, branch_steps: list[dict]) -> None:
            try:
                self._run_sequence(branch_steps, context)
            except ActionError as exc:
                with errors_lock:
                    errors.append(str(exc))
            except Exception:
                logger.exception("Parallel branch %d crashed", index)
                with errors_lock:
                    errors.append(f"внутренняя ошибка в ветке {index}")

        threads = [
            threading.Thread(target=_run_branch, args=(i, branch), name=f"parallel-branch-{i}", daemon=True)
            for i, branch in enumerate(branches, start=1)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        if errors:
            raise ActionError("; ".join(errors))

    def _fmt(self, value: str, context: dict[str, Any]) -> str:
        try:
            return value.format(**context)
        except (KeyError, IndexError):
            return value

    def _timeout(self, step: dict, default_key: str = "generic_step_sec") -> float:
        key = step.get("timeout_key", default_key)
        return float(self.config.get("timeouts", key, default=10))

    def _seconds(self, step: dict) -> float:
        if "seconds_key" in step:
            key = step["seconds_key"]
            value = self.config.get("delays", key, default=None)
            if value is None:
                value = self.config.get("timeouts", key, default=1.0)
            return float(value)
        return float(step.get("seconds", 1.0))

    def _retries(self, step: dict) -> int:
        if "retries_key" in step:
            return int(self.config.get("vision", step["retries_key"], default=self.default_retries))
        return int(step.get("retries", self.default_retries))

    def _region_for(self, step: dict) -> tuple[int, int, int, int] | None:
        region_key = step.get("region_key")
        if not region_key:
            return None
        region = self.config.get("vision", f"region_{region_key}", default=None)
        return tuple(region) if region else None

    def _uncover_cursor(self, element_size: tuple[int, int] | None = None) -> bool:
        """The cursor left on a button highlights it, and a highlighted button
        no longer matches its template - which happens a lot, because clicking
        "Играть" leaves the cursor exactly where "Найти игру" then appears.
        Moves the cursor out of the way once, so the next look sees the plain
        button. Returns whether it moved."""
        screen_w, screen_h = vision.get_screen_resolution()
        x, y = pyautogui.position()
        park = self.config.get("automation", "cursor_park", default=None)
        if park:
            target = (int(park[0]), int(park[1]))
        else:
            # Just off the element, not across the screen. The step has to
            # clear the element itself, so it is at least half its width.
            nudge = int(self.config.get("automation", "cursor_nudge_px", default=120))
            if element_size:
                nudge = max(nudge, element_size[0] // 2 + 20)
            target = (x - nudge if x - nudge >= 1 else x + nudge, y)
            if not 1 <= target[0] <= screen_w - 2:
                step = max(nudge, (element_size[1] // 2 + 20) if element_size else nudge)
                target = (x, y - step if y - step >= 1 else y + step)
        target = (max(1, min(screen_w - 2, target[0])), max(1, min(screen_h - 2, target[1])))
        if abs(x - target[0]) < 5 and abs(y - target[1]) < 5:
            return False
        logger.debug("Nudging the cursor off the UI (it may be highlighting the element).")
        pyautogui.moveTo(target[0], target[1], duration=self.move_duration, tween=self.move_tween)
        return True

    def _move_and_click(self, x: int, y: int) -> None:
        pyautogui.moveTo(x, y, duration=self.move_duration, tween=self.move_tween)
        pyautogui.click()

    def _h_notify(self, step: dict, context: dict) -> None:
        text = self._fmt(step.get("text", ""), context)
        self.notifier.beep(ok=True)
        self.notifier.speak(text)

    def _h_launch_process(self, step: dict, context: dict) -> None:
        app_key = step["app"]
        app_cfg = self.config.get("apps", app_key)
        if not app_cfg:
            raise ActionError(f"Приложение '{app_key}' не описано в config.yaml -> apps")

        process_name = app_cfg.get("process_name")
        try:
            process_utils.launch_process(
                path=app_cfg["path"],
                args=app_cfg.get("launch_args"),
                process_name=process_name,
            )
        except process_utils.ProcessError as exc:
            raise ActionError(str(exc)) from exc

        if not process_utils.wait_for_process(
            process_name, timeout=self._timeout(step, "process_launch_sec"), stop_event=self._stop_event
        ):
            if self._stop_event.is_set():
                return
            raise ActionError(f"Процесс {process_name} не запустился за отведённое время")

        if step.get("wait_window", False):
            title = app_cfg.get("window_title_substr", "")
            if title and not process_utils.wait_for_window(
                title, timeout=self._timeout(step), stop_event=self._stop_event
            ):
                if self._stop_event.is_set():
                    return
                raise ActionError(f"Окно приложения '{app_key}' не появилось за отведённое время")

    def _dota_launch_uri(self) -> str:
        dota_app_id = self.config.get("dota2", "app_id", default=570)
        return f"steam://rungameid/{dota_app_id}"

    def _h_launch_uri(self, step: dict, context: dict) -> None:
        dota_app_id = self.config.get("dota2", "app_id", default=570)
        extra_context = {**context, "dota_app_id": dota_app_id, "dota_launch_uri": self._dota_launch_uri()}
        uri = self._fmt(step["uri"], extra_context)
        process_utils.launch_uri(uri)

        wait_title_key = step.get("wait_window_title_key")
        if wait_title_key:
            title_cfg = self.config.get(wait_title_key, "window_title_substr", default=None)
            if title_cfg and not process_utils.wait_for_window(
                title_cfg, timeout=self._timeout(step), stop_event=self._stop_event
            ):
                if self._stop_event.is_set():
                    return
                raise ActionError(f"Окно '{title_cfg}' не появилось за отведённое время")

    def _wait_for_template_visible(
        self, template_name: str, timeout: float, region: tuple[int, int, int, int] | None = None
    ) -> bool:
        template_path = self.templates_dir / f"{template_name}.png"
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._stop_event.is_set():
                return False
            if vision.find_template(template_path, threshold=self.match_threshold, region=region) is not None:
                return True
            self._interruptible_sleep(1.0)
        return False

    def _focus_dota_window(self) -> None:
        # Other apps launched in parallel (Chrome, Discord) can steal focus while
        # Dota 2 is loading - make sure key presses actually land in the game.
        title = self.config.get("dota2", "window_title_substr", default="Dota 2")
        hwnd = process_utils.find_window_by_title_substr(title)
        if hwnd is not None and win32gui.GetForegroundWindow() != hwnd:
            process_utils.restore_and_focus_window(hwnd)

    def _skip_intro_and_wait_for_menu(self, timeout: float) -> bool:
        """Waits for the Play button while also watching for any calibrated
        intro-splash frame (templates/intro_splash*.png) and pressing Escape
        the moment one is actually detected on screen - a deterministic
        reaction to a real frame match, not a blind click on a timer. Fully
        optional: with no intro_splash*.png templates calibrated, this is
        identical to plain _wait_for_template_visible("play_button", ...)."""
        intro_templates = sorted(self.templates_dir.glob("intro_splash*.png"))
        play_button_path = self.templates_dir / "play_button.png"
        region_play = self._region_for({"region_key": "play_button"})
        skip_cooldown = 1.0
        last_skip_time = 0.0

        if intro_templates:
            logger.info("Watching for %d intro splash template(s) to skip.", len(intro_templates))
        else:
            logger.debug("No intro_splash*.png templates calibrated - just waiting for the Play button.")

        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._stop_event.is_set():
                return False

            if vision.find_template(play_button_path, threshold=self.match_threshold, region=region_play) is not None:
                return True

            now = time.time()
            if intro_templates and (now - last_skip_time) > skip_cooldown:
                for template_path in intro_templates:
                    if vision.find_template(template_path, threshold=self.match_threshold) is not None:
                        logger.info("Detected intro splash '%s', pressing Escape to skip it.", template_path.stem)
                        self._focus_dota_window()
                        pyautogui.press("escape")
                        last_skip_time = now
                        break

            self._interruptible_sleep(0.5)

        return False

    def _h_ensure_dota_ready(self, step: dict, context: dict) -> None:
        """Walks the Steam -> Dota 2 launch chain, checking each stage instead of
        assuming a cold start: skips Steam if it's already running, skips
        launching Dota 2 if its process is already up, and always finishes by
        restoring/focusing the Dota 2 window (covers "already open but
        minimized"). On a fresh launch, actively waits for the main menu's
        Play button to actually render instead of guessing a fixed delay -
        the game window can exist for a long time before Panorama UI (and
        the assets it needs) has finished loading - and presses Escape to
        skip past any calibrated intro splash it detects along the way (see
        _skip_intro_and_wait_for_menu)."""
        dota_cfg = self.config.get("dota2", default={}) or {}
        process_name = dota_cfg.get("process_name", "dota2.exe")
        window_title = dota_cfg.get("window_title_substr", "Dota 2")

        was_running = process_utils.is_process_running(process_name)

        if was_running:
            logger.info("Dota 2 is already running.")
        else:
            logger.info("Dota 2 is not running - making sure Steam is up first.")
            self._h_launch_process(
                {"app": "steam", "wait_window": True, "timeout_key": "steam_ready_sec"}, context
            )
            if self._stop_event.is_set():
                return
            self._interruptible_sleep(float(self.config.get("delays", "after_steam_launch", default=3)))

            logger.info("Launching Dota 2 via Steam.")
            self._h_launch_uri(
                {
                    "uri": "{dota_launch_uri}",
                    "wait_window_title_key": "dota2",
                    "timeout_key": "dota_ready_sec",
                },
                context,
            )
            if self._stop_event.is_set():
                return

        if self._stop_event.is_set():
            return

        hwnd = process_utils.find_window_by_title_substr(window_title)
        if hwnd is None:
            raise ActionError("Окно Dota 2 не найдено, хотя процесс запущен")

        logger.info("Bringing the Dota 2 window to the foreground.")
        if not process_utils.restore_and_focus_window(hwnd):
            logger.warning("Could not confirm the Dota 2 window was focused - continuing anyway.")

        if self._stop_event.is_set():
            return

        if was_running:
            self._interruptible_sleep(float(self.config.get("delays", "between_ui_clicks", default=0.3)))
            return

        menu_timeout = float(self.config.get("timeouts", "dota_menu_ready_sec", default=60))
        logger.info("Waiting up to %.0fs for the Dota 2 main menu to finish loading...", menu_timeout)
        self._interruptible_sleep(2.0)  # let the freshly-focused window actually paint before the first screenshot
        if self._stop_event.is_set():
            return

        if not self._skip_intro_and_wait_for_menu(menu_timeout):
            if self._stop_event.is_set():
                return
            raise ActionError(
                "Dota 2 запущена, но главное меню не появилось за отведённое время "
                "(возможно, идёт долгая загрузка или обновление - попробуй ещё раз, когда меню откроется)"
            )

    def _h_close_process(self, step: dict, context: dict) -> None:
        section_key = step["process_key"]
        process_name = self.config.get(section_key, "process_name", default=None)
        if not process_name:
            raise ActionError(f"В config.yaml нет {section_key}.process_name")
        if not process_utils.is_process_running(process_name):
            logger.info("%s is not running, nothing to close.", process_name)
            return
        if not process_utils.close_process(
            process_name, timeout=self._timeout(step, "process_close_sec"), stop_event=self._stop_event
        ):
            if self._stop_event.is_set():
                return
            raise ActionError("программа не закрылась - возможно, она ждёт подтверждения выхода")

    def _h_wait_window(self, step: dict, context: dict) -> None:
        title = self._fmt(step["title"], context)
        if not process_utils.wait_for_window(title, timeout=self._timeout(step), stop_event=self._stop_event):
            if self._stop_event.is_set():
                return
            raise ActionError(f"Окно, содержащее '{title}', не появилось")

    def _h_sleep(self, step: dict, context: dict) -> None:
        self._interruptible_sleep(self._seconds(step))

    def _h_open_url_in_chrome(self, step: dict, context: dict) -> None:
        url = self._fmt(step["url"], context)
        chrome_path = self.config.get("apps", "chrome", "path")
        try:
            controller = webbrowser.get(f'"{chrome_path}" %s')
            controller.open_new_tab(url)
        except webbrowser.Error:
            logger.warning("Failed to open via the explicit Chrome path, falling back to webbrowser.open")
            webbrowser.open_new_tab(url)

    def _h_click_template(self, step: dict, context: dict) -> None:
        template_name = self._fmt(step["template"], context)
        template_path = self.templates_dir / f"{template_name}.png"
        region = self._region_for(step)

        # The cursor left on a button highlights it, so the plain crop stops
        # matching. Optional <name>_hover*.png crops of that highlighted look
        # (any number, e.g. from `calibrate.py --burst`) are checked too, which
        # saves moving the cursor at all.
        hover_paths = sorted(self.templates_dir.glob(f"{template_name}_hover*.png"))
        retries = self._retries(step)
        uncovered = False
        attempt = 0
        while attempt < retries:
            attempt += 1
            if self._stop_event.is_set():
                return
            match = vision.find_template(template_path, threshold=self.match_threshold, region=region)
            for hover_path in hover_paths:
                if match is not None:
                    break
                match = vision.find_template(hover_path, threshold=self.match_threshold, region=region)
                if match is not None:
                    logger.debug("Matched the hovered look of '%s'.", template_name)
            if match is not None:
                self._move_and_click(match.center_x, match.center_y)
                return
            logger.debug("Attempt %d/%d: template '%s' not found", attempt, retries, template_name)
            # Moving the cursor off the element is a free extra look, not one
            # of the retries.
            if not uncovered:
                uncovered = self._uncover_cursor(vision.template_size(template_path))
                if uncovered:
                    attempt -= 1
                    continue
            self._interruptible_sleep(self.retry_delay)

        if self._stop_event.is_set():
            return

        # The score tells a missing element ("not on screen at all") apart from
        # a stale template ("there, but no longer looks like the crop").
        best = vision.best_match(template_path, region)
        if best is not None:
            logger.error(
                "Template '%s' best score was %.3f (threshold %.2f) at (%d, %d)",
                template_name, best.score, self.match_threshold, best.center_x, best.center_y,
            )

        fallback = step.get("fallback_point")
        if fallback:
            logger.warning("Template '%s' not found, clicking the fallback coordinates %s", template_name, fallback)
            self._move_and_click(fallback[0], fallback[1])
            return

        raise ActionError(
            f"Элемент UI '{template_name}' не найден на экране за {retries} попыток "
            f"(проверьте калибровку шаблонов и разрешение экрана)"
        )

    def _h_select_exclusive_role(self, step: dict, context: dict) -> None:
        target_role = context.get("role")
        if not target_role:
            raise ActionError("Шаг select_exclusive_role не получил роль из распознанной команды")

        roles = self.config.get("roles", default={}) or {}
        click_delay = float(self.config.get("delays", "between_ui_clicks", default=0.6))
        region = self._region_for(step)

        for role_id in roles:
            if self._stop_event.is_set():
                return
            if role_id == target_role:
                continue
            selected_path = self.templates_dir / f"role_{role_id}_selected.png"
            match = vision.find_template(selected_path, threshold=self.match_threshold, region=region)
            if match is not None:
                logger.debug("Role '%s' is currently selected, clicking it off", role_id)
                self._move_and_click(match.center_x, match.center_y)
                self._interruptible_sleep(click_delay)

        if self._stop_event.is_set():
            return

        target_selected_path = self.templates_dir / f"role_{target_role}_selected.png"
        if vision.find_template(target_selected_path, threshold=self.match_threshold, region=region) is not None:
            return

        target_base_path = self.templates_dir / f"role_{target_role}.png"
        retries = self._retries(step)
        uncovered = False
        attempt = 0
        while attempt < retries:
            attempt += 1
            if self._stop_event.is_set():
                return
            match = vision.find_template(target_base_path, threshold=self.match_threshold, region=region)
            if match is not None:
                self._move_and_click(match.center_x, match.center_y)
                return
            logger.debug("Attempt %d/%d: role icon '%s' not found", attempt, retries, target_role)
            # Moving the cursor off the element is a free extra look, not one
            # of the retries.
            if not uncovered:
                uncovered = self._uncover_cursor(vision.template_size(target_base_path))
                if uncovered:
                    attempt -= 1
                    continue
            self._interruptible_sleep(self.retry_delay)

        if self._stop_event.is_set():
            return

        raise ActionError(
            f"Не найдена иконка роли '{target_role}' на экране за {retries} попыток "
            f"(проверьте калибровку шаблонов role_{target_role}.png / role_{target_role}_selected.png)"
        )

    # A row under the cursor is highlighted and scores lower against its
    # template. We know that row is there (we just clicked it), so a looser
    # threshold is enough to keep tracking it.
    _HOVERED_ROW_THRESHOLD = 0.7

    def _mode_states(self, modes, region) -> dict[str, tuple[bool, tuple[int, int]]]:
        """For every mode whose row is on screen: (is_ticked, where to click -
        the checkbox itself when it can be located, else the row center).
        mode_<id>.png and mode_<id>_selected.png differ only by the checkbox,
        which can be ~1% of a row with hero art behind it - so both whole-row
        scores are nearly equal. The row is located with whichever template
        scores higher, then the tick is decided by comparing just the checkbox
        area (see vision.checkbox_patches)."""
        screenshot = vision.capture_screen(region)
        origin = (region[0], region[1]) if region else (0, 0)
        cursor_x, cursor_y = pyautogui.position()
        states = {}
        for mode_id in modes:
            plain_path = self.templates_dir / f"mode_{mode_id}.png"
            ticked_path = self.templates_dir / f"mode_{mode_id}_selected.png"
            plain = vision.best_match(plain_path, region, screenshot)
            ticked = vision.best_match(ticked_path, region, screenshot)
            candidates = [(m.score, is_ticked, m) for m, is_ticked in ((plain, False), (ticked, True)) if m is not None]
            if not candidates:
                continue
            score, is_ticked, match = max(candidates, key=lambda c: c[0])
            hovered = (
                abs(cursor_x - match.center_x) <= match.width // 2
                and abs(cursor_y - match.center_y) <= match.height // 2
            )
            threshold = self._HOVERED_ROW_THRESHOLD if hovered else self.match_threshold
            if score < threshold:
                logger.debug("Mode '%s' not found (score=%.3f, hovered=%s)", mode_id, score, hovered)
                continue

            click_point = (match.center_x, match.center_y)
            patches = vision.checkbox_patches(plain_path, ticked_path)
            if patches is not None:
                offset = patches.ticked_offset if is_ticked else patches.plain_offset
                row_left = match.center_x - match.width // 2 - origin[0]
                row_top = match.center_y - match.height // 2 - origin[1]
                box_left, box_top = row_left + offset[0], row_top + offset[1]
                box_state = vision.checkbox_is_ticked(screenshot, (box_left, box_top), patches)
                if box_state is not None:
                    is_ticked = box_state
                    box_h, box_w = patches.plain.shape[:2]
                    click_point = (origin[0] + box_left + box_w // 2, origin[1] + box_top + box_h // 2)
            logger.debug("Mode '%s': row score=%.3f, hovered=%s, ticked=%s", mode_id, score, hovered, is_ticked)
            states[mode_id] = (is_ticked, click_point)
        return states

    @staticmethod
    def _pad_region(region: tuple[int, int, int, int], pad: int) -> tuple[int, int, int, int]:
        screen_w, screen_h = vision.get_screen_resolution()
        left, top, width, height = region
        new_left, new_top = max(0, left - pad), max(0, top - pad)
        right = min(screen_w, left + width + pad)
        bottom = min(screen_h, top + height + pad)
        return new_left, new_top, right - new_left, bottom - new_top

    def _h_select_exclusive_mode(self, step: dict, context: dict) -> None:
        """Normal-game mode checkboxes are toggles like the role icons: leaves
        only the target mode ticked. Each mode (config.yaml -> modes) needs
        mode_<id>.png and mode_<id>_selected.png - checkbox plus label, since
        clicking either toggles the row. Expands "Показать все режимы" first
        so a ticked hidden mode can't slip through, then re-reads the screen
        after every round of clicks until the state is confirmed."""
        target_mode = context.get("mode")
        if not target_mode:
            raise ActionError("Шаг select_exclusive_mode не получил режим из распознанной команды")

        modes = self.config.get("modes", default={}) or {}
        if target_mode not in modes:
            raise ActionError(f"Режим '{target_mode}' не описан в config.yaml -> modes")

        region = self._region_for(step)
        if region is not None:
            # A hand-drawn region is often exactly as wide as the row crops
            # themselves - a template bigger than the region never matches.
            region = self._pad_region(region, 20)
        click_delay = float(self.config.get("delays", "between_ui_clicks", default=0.3))

        # The expander's collapsed and expanded looks differ only by the arrow,
        # so its template matches both - clicking it on sight would collapse an
        # already open list. Every mode row being visible means it's open.
        if len(self._mode_states(modes, region)) < len(modes):
            expander = vision.find_template(
                self.templates_dir / "modes_show_all_collapsed.png", threshold=self.match_threshold, region=region
            )
            if expander is not None:
                logger.info("Mode list is collapsed - expanding it to check the hidden modes.")
                self._move_and_click(expander.center_x, expander.center_y)
                self._interruptible_sleep(click_delay)

        label = context.get("mode_label", target_mode)
        retries = self._retries(step)
        states: dict[str, tuple[bool, tuple[int, int]]] = {}
        for attempt in range(1, retries + 1):
            if self._stop_event.is_set():
                return

            states = self._mode_states(modes, region)
            # A hidden row may still be ticked - don't judge (or click) until
            # every mode is on screen.
            missing = [mode_id for mode_id in modes if mode_id not in states]
            if missing:
                logger.debug("Attempt %d/%d: modes not visible: %s", attempt, retries, ", ".join(missing))
                self._interruptible_sleep(self.retry_delay)
                continue

            stale = [mode_id for mode_id, (is_ticked, _) in states.items() if is_ticked and mode_id != target_mode]
            target_ticked, target_point = states[target_mode]
            if not stale and target_ticked:
                logger.info("Only the '%s' mode is selected.", target_mode)
                return

            for mode_id in stale:
                if self._stop_event.is_set():
                    return
                logger.info("Unticking mode '%s'.", mode_id)
                self._move_and_click(*states[mode_id][1])
                self._interruptible_sleep(click_delay)

            if not target_ticked and not self._stop_event.is_set():
                logger.info("Ticking mode '%s'.", target_mode)
                self._move_and_click(*target_point)
                self._interruptible_sleep(click_delay)

        if self._stop_event.is_set():
            return
        seen = ", ".join(f"{mode_id}={'on' if ticked else 'off'}" for mode_id, (ticked, _) in states.items())
        logger.error("Mode selection gave up; last seen: %s", seen or "no mode rows")
        missing = [mode_id for mode_id in modes if mode_id not in states]
        if missing:
            names = ", ".join(str(modes[mode_id].get("label", mode_id)) for mode_id in missing)
            raise ActionError(
                f"Не видно всех режимов в списке ({names}) - поиск не запущен, "
                f"чтобы не включить лишний режим. Проверьте, раскрыт ли список 'Показать все режимы'"
            )
        raise ActionError(
            f"Не удалось оставить включённым только режим '{label}' "
            f"(проверьте шаблоны mode_{target_mode}.png / mode_{target_mode}_selected.png)"
        )

    def _h_open_section(self, step: dict, context: dict) -> None:
        """Opens a play-menu section (Рейтинговая / Обычная игра) only if it
        isn't open already. The header looks different open vs. closed, so
        both are calibrated: `template` (open) and `inactive_template`
        (closed). Like the mode rows, the two can both clear the threshold,
        so the higher score decides. Clicks only a closed header."""
        active_path = self.templates_dir / f"{step['template']}.png"
        inactive_path = self.templates_dir / f"{step['inactive_template']}.png"
        region = self._region_for(step)
        click_delay = float(self.config.get("delays", "between_ui_clicks", default=0.3))
        if not inactive_path.exists():
            logger.warning(
                "%s is not calibrated - a closed '%s' section can't be opened automatically.",
                inactive_path.name, step["template"],
            )

        retries = self._retries(step)
        for attempt in range(1, retries + 1):
            if self._stop_event.is_set():
                return
            screenshot = vision.capture_screen(region)
            active = vision.best_match(active_path, region, screenshot)
            inactive = vision.best_match(inactive_path, region, screenshot) if inactive_path.exists() else None
            active_score = active.score if active else 0.0
            inactive_score = inactive.score if inactive else 0.0

            if active_score >= self.match_threshold and active_score >= inactive_score:
                logger.info("Section '%s' is already open.", step["template"])
                return
            if inactive is not None and inactive_score >= self.match_threshold:
                logger.info("Opening section '%s'.", step["template"])
                self._move_and_click(inactive.center_x, inactive.center_y)
                # Let the menu finish re-laying itself out before the next step looks at it.
                self._interruptible_sleep(click_delay + 0.5)
                return
            logger.debug(
                "Attempt %d/%d: section header '%s' not found (open=%.3f, closed=%.3f)",
                attempt, retries, step["template"], active_score, inactive_score,
            )
            self._interruptible_sleep(self.retry_delay)

        if self._stop_event.is_set():
            return
        raise ActionError(
            f"Не найден заголовок раздела '{step['template']}' ни в открытом, ни в закрытом виде "
            f"(проверьте шаблоны {step['template']}.png / {step['inactive_template']}.png)"
        )

    def _h_cancel_search(self, step: dict, context: dict) -> None:
        """While Dota is searching, a "ПОИСК ИГРЫ" bar replaces the play
        controls and the mode/role choices are locked, so a new command can't
        do anything until that search is cancelled. Only the small red cross
        inside the bar cancels it, so the bar is what's detected
        (search_in_progress.png) and the cross is looked for inside it
        (cancel_search_button.png) - a small red icon would match all over the
        screen otherwise. Does nothing when no search is running or the
        templates aren't calibrated."""
        bar_path = self.templates_dir / f"{step.get('template', 'search_in_progress')}.png"
        cross_path = self.templates_dir / f"{step.get('cancel_template', 'cancel_search_button')}.png"
        if not bar_path.exists() or not cross_path.exists():
            logger.debug(
                "%s / %s not calibrated - a running search can't be detected.", bar_path.name, cross_path.name
            )
            return

        region = self._region_for(step)
        bar = vision.find_template(bar_path, threshold=self.match_threshold, region=region)
        if bar is None:
            logger.debug("No search in progress.")
            return

        logger.info("A search is already running - cancelling it before starting a new one.")
        pad = 10
        bar_area = (
            bar.center_x - bar.width // 2 - pad,
            bar.center_y - bar.height // 2 - pad,
            bar.width + 2 * pad,
            bar.height + 2 * pad,
        )
        cross = vision.find_template(cross_path, threshold=self.match_threshold, region=bar_area)
        if cross is None:
            raise ActionError(
                "Идёт поиск игры, но кнопка отмены не найдена "
                "(проверьте шаблоны search_in_progress.png / cancel_search_button.png)"
            )

        self._move_and_click(cross.center_x, cross.center_y)
        self._interruptible_sleep(float(self.config.get("delays", "between_ui_clicks", default=0.3)) + 0.5)

        retries = self._retries(step)
        for _ in range(retries):
            if self._stop_event.is_set():
                return
            if vision.find_template(bar_path, threshold=self.match_threshold, region=region) is None:
                return
            self._interruptible_sleep(self.retry_delay)

        if self._stop_event.is_set():
            return
        raise ActionError("Не удалось отменить текущий поиск игры")

    def _h_click_point(self, step: dict, context: dict) -> None:
        x, y = step["point"]
        self._move_and_click(x, y)

    def _h_key_press(self, step: dict, context: dict) -> None:
        keys = step["keys"]
        if isinstance(keys, str):
            keys = [keys]
        pyautogui.hotkey(*keys)
