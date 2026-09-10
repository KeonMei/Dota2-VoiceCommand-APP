from __future__ import annotations

import logging
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Callable

import pyautogui

from . import process_utils, vision
from .config import Config
from .notify import Notifier

logger = logging.getLogger("dota_voice.actions")

pyautogui.FAILSAFE = True


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
        for step in steps:
            if self._stop_event.is_set():
                logger.info("Step sequence stopped by voice command.")
                return False

            step_type = step.get("type")
            handler = self._handlers.get(step_type)
            if handler is None:
                logger.error("Unknown step type: %s", step_type)
                continue

            optional = bool(step.get("optional", False))
            try:
                handler(step, context)
            except ActionError as exc:
                logger.error("Step '%s' failed: %s", step_type, exc)
                if optional:
                    logger.warning("Step is marked optional, continuing.")
                    continue
                self.notifier.beep(ok=False)
                self.notifier.speak(f"Не удалось выполнить шаг: {exc}")
                return False

            if self._stop_event.is_set():
                logger.info("Step sequence stopped by voice command.")
                return False
        return True

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

    def _h_launch_uri(self, step: dict, context: dict) -> None:
        dota_app_id = self.config.get("dota2", "app_id", default=570)
        uri = self._fmt(step["uri"], {**context, "dota_app_id": dota_app_id})
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

        retries = self._retries(step)
        for attempt in range(1, retries + 1):
            if self._stop_event.is_set():
                return
            match = vision.find_template(template_path, threshold=self.match_threshold)
            if match is not None:
                pyautogui.moveTo(match.center_x, match.center_y, duration=0.15)
                pyautogui.click()
                return
            logger.debug("Attempt %d/%d: template '%s' not found", attempt, retries, template_name)
            self._interruptible_sleep(self.retry_delay)

        if self._stop_event.is_set():
            return

        fallback = step.get("fallback_point")
        if fallback:
            logger.warning("Template '%s' not found, clicking the fallback coordinates %s", template_name, fallback)
            pyautogui.moveTo(fallback[0], fallback[1], duration=0.15)
            pyautogui.click()
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

        for role_id in roles:
            if self._stop_event.is_set():
                return
            if role_id == target_role:
                continue
            selected_path = self.templates_dir / f"role_{role_id}_selected.png"
            match = vision.find_template(selected_path, threshold=self.match_threshold)
            if match is not None:
                logger.debug("Role '%s' is currently selected, clicking it off", role_id)
                pyautogui.moveTo(match.center_x, match.center_y, duration=0.15)
                pyautogui.click()
                self._interruptible_sleep(click_delay)

        if self._stop_event.is_set():
            return

        target_selected_path = self.templates_dir / f"role_{target_role}_selected.png"
        if vision.find_template(target_selected_path, threshold=self.match_threshold) is not None:
            return

        target_base_path = self.templates_dir / f"role_{target_role}.png"
        retries = self._retries(step)
        for attempt in range(1, retries + 1):
            if self._stop_event.is_set():
                return
            match = vision.find_template(target_base_path, threshold=self.match_threshold)
            if match is not None:
                pyautogui.moveTo(match.center_x, match.center_y, duration=0.15)
                pyautogui.click()
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
        pyautogui.moveTo(x, y, duration=0.15)
        pyautogui.click()

    def _h_key_press(self, step: dict, context: dict) -> None:
        keys = step["keys"]
        if isinstance(keys, str):
            keys = [keys]
        pyautogui.hotkey(*keys)
