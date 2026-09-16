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

    def run_steps(self, steps: list[dict], context: dict[str, Any]) -> bool:
        self._stop_event.clear()
        try:
            completed = self._run_sequence(steps, context)
        except ActionError as exc:
            self.notifier.beep(ok=False)
            self.notifier.speak(f"Не удалось выполнить шаг: {exc}")
            return False
        if not completed:
            logger.info("Step sequence stopped by voice command.")
        return completed

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

        retries = self._retries(step)
        for attempt in range(1, retries + 1):
            if self._stop_event.is_set():
                return
            match = vision.find_template(template_path, threshold=self.match_threshold, region=region)
            if match is not None:
                self._move_and_click(match.center_x, match.center_y)
                return
            logger.debug("Attempt %d/%d: template '%s' not found", attempt, retries, template_name)
            self._interruptible_sleep(self.retry_delay)

        if self._stop_event.is_set():
            return

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
        for attempt in range(1, retries + 1):
            if self._stop_event.is_set():
                return
            match = vision.find_template(target_base_path, threshold=self.match_threshold, region=region)
            if match is not None:
                self._move_and_click(match.center_x, match.center_y)
                return
            logger.debug("Attempt %d/%d: role icon '%s' not found", attempt, retries, target_role)
            self._interruptible_sleep(self.retry_delay)

        if self._stop_event.is_set():
            return

        raise ActionError(
            f"Не найдена иконка роли '{target_role}' на экране за {retries} попыток "
            f"(проверьте калибровку шаблонов role_{target_role}.png / role_{target_role}_selected.png)"
        )

    def _h_click_point(self, step: dict, context: dict) -> None:
        x, y = step["point"]
        self._move_and_click(x, y)

    def _h_key_press(self, step: dict, context: dict) -> None:
        keys = step["keys"]
        if isinstance(keys, str):
            keys = [keys]
        pyautogui.hotkey(*keys)
