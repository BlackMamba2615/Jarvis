"""Config-driven, allowlisted skill engine for Jarvis.

Only action identifiers from skills.json can be selected from user speech.
Speech is never passed to a shell or command interpreter.
"""

from __future__ import annotations

import ctypes
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class SkillConfigError(RuntimeError):
    """Raised when skills.json is missing or unsafe."""


@dataclass(frozen=True)
class PlannedAction:
    action_id: str
    message: str


class SkillSystem:
    ALLOWED_ACTION_TYPES = {"open_path", "status", "lock_pc", "wechat_login_qr"}

    def __init__(
        self,
        config_path: str | os.PathLike[str],
        config: dict,
        normalize_term: Callable[[str], str] | None = None,
    ) -> None:
        self.config_path = Path(config_path)
        self.config = config
        self._normalize_term = normalize_term or (lambda value: value.lower().strip())
        self.settings = config["settings"]
        self.actions = config["actions"]
        self.scenes = config["scenes"]
        self._validate()

    @classmethod
    def load(
        cls,
        config_path: str | os.PathLike[str],
        normalize_term: Callable[[str], str] | None = None,
    ) -> "SkillSystem":
        path = Path(config_path)
        try:
            with path.open("r", encoding="utf-8-sig") as config_file:
                config = json.load(config_file)
        except FileNotFoundError as error:
            raise SkillConfigError(f"Skill config not found: {path}") from error
        except json.JSONDecodeError as error:
            raise SkillConfigError(
                f"Invalid JSON in {path} at line {error.lineno}, column {error.colno}"
            ) from error
        return cls(path, config, normalize_term)

    def _validate_string_list(self, value: object, field_name: str) -> list[str]:
        if not isinstance(value, list) or not value:
            raise SkillConfigError(f"{field_name} must be a non-empty list")
        if not all(isinstance(item, str) and item.strip() for item in value):
            raise SkillConfigError(f"{field_name} may contain only non-empty strings")
        return value

    def _validate(self) -> None:
        if not isinstance(self.config, dict):
            raise SkillConfigError("skills.json must contain a JSON object")
        if not isinstance(self.settings, dict):
            raise SkillConfigError("settings must be an object")
        if not isinstance(self.actions, dict) or not self.actions:
            raise SkillConfigError("actions must be a non-empty object")
        if not isinstance(self.scenes, dict):
            raise SkillConfigError("scenes must be an object")

        self._validate_string_list(self.settings.get("wake_words"), "settings.wake_words")
        sleep_phrases = self._validate_string_list(
            self.settings.get("sleep_phrases"), "settings.sleep_phrases"
        )
        exit_phrases = self._validate_string_list(
            self.settings.get("exit_phrases"), "settings.exit_phrases"
        )
        if set(self._terms(sleep_phrases)) & set(self._terms(exit_phrases)):
            raise SkillConfigError("Sleep and exit phrases must be distinct")
        self._validate_string_list(self.settings.get("open_intents"), "settings.open_intents")
        punctuation = self.settings.get("punctuation", [])
        if not isinstance(punctuation, list) or not all(isinstance(item, str) for item in punctuation):
            raise SkillConfigError("settings.punctuation must be a list of strings")

        for action_id, action in self.actions.items():
            if not isinstance(action_id, str) or not action_id.strip():
                raise SkillConfigError("Every action id must be a non-empty string")
            if not isinstance(action, dict):
                raise SkillConfigError(f"actions.{action_id} must be an object")
            action_type = action.get("type")
            if action_type not in self.ALLOWED_ACTION_TYPES:
                raise SkillConfigError(
                    f"actions.{action_id}.type must be one of: "
                    + ", ".join(sorted(self.ALLOWED_ACTION_TYPES))
                )
            message = action.get("message")
            if not isinstance(message, str) or not message.strip():
                raise SkillConfigError(f"actions.{action_id}.message must be a string")

            if action_type == "open_path":
                target = action.get("target")
                if not isinstance(target, str) or not os.path.isabs(target):
                    raise SkillConfigError(
                        f"actions.{action_id}.target must be an absolute path"
                    )
                self._validate_string_list(action.get("aliases"), f"actions.{action_id}.aliases")
                if action.get("intent", "open") != "open":
                    raise SkillConfigError(f"actions.{action_id}.intent must be 'open'")
            else:
                self._validate_string_list(action.get("triggers"), f"actions.{action_id}.triggers")
                keyword_groups = action.get("keyword_groups")
                if keyword_groups is not None:
                    if action_type != "wechat_login_qr" or not isinstance(keyword_groups, list) or not keyword_groups:
                        raise SkillConfigError(
                            f"actions.{action_id}.keyword_groups is only allowed for WeChat login QR"
                        )
                    for index, group in enumerate(keyword_groups):
                        self._validate_string_list(
                            group, f"actions.{action_id}.keyword_groups[{index}]"
                        )

        for scene_id, scene in self.scenes.items():
            if not isinstance(scene, dict):
                raise SkillConfigError(f"scenes.{scene_id} must be an object")
            self._validate_string_list(scene.get("keywords"), f"scenes.{scene_id}.keywords")
            scene_actions = self._validate_string_list(scene.get("actions"), f"scenes.{scene_id}.actions")
            unknown = [action_id for action_id in scene_actions if action_id not in self.actions]
            if unknown:
                raise SkillConfigError(
                    f"scenes.{scene_id} references unknown actions: {', '.join(unknown)}"
                )

    def setting_list(self, name: str) -> list[str]:
        value = self.settings.get(name, [])
        return list(value) if isinstance(value, list) else []

    def _terms(self, values: list[str]) -> list[str]:
        return [self._normalize_term(value) for value in values]

    @staticmethod
    def _contains_any(text: str, terms: list[str]) -> bool:
        return any(term in text for term in terms)

    def _matches_keyword_groups(self, command: str, groups: list[list[str]]) -> bool:
        """Require one configured keyword from every group; never infer new actions."""
        return bool(groups) and all(
            self._contains_any(command, self._terms(group)) for group in groups
        )

    def plan(self, normalized_command: str) -> tuple[list[PlannedAction], str | None]:
        command = normalized_command.strip()
        if not command:
            return [], None

        for scene in self.scenes.values():
            keywords = self._terms(scene["keywords"])
            if self._contains_any(command, keywords):
                planned = [
                    PlannedAction(action_id, self.actions[action_id]["message"])
                    for action_id in scene["actions"]
                ]
                return planned, scene.get("message")

        selected: list[tuple[str, str]] = []
        open_requested = self._contains_any(
            command,
            self._terms(self.settings["open_intents"]),
        )

        if open_requested:
            for action_id, action in self.actions.items():
                if action["type"] != "open_path":
                    continue
                matches = [
                    alias for alias in self._terms(action["aliases"])
                    if alias in command
                ]
                if matches:
                    selected.append((action_id, max(matches, key=len)))

            # Prefer a specific phrase such as "Jarvis文件夹" over its generic
            # suffix "文件夹", while preserving genuinely separate commands.
            selected = [
                item for item in selected
                if not any(
                    item[0] != other[0]
                    and item[1] != other[1]
                    and item[1] in other[1]
                    for other in selected
                )
            ]

        for action_id, action in self.actions.items():
            if action["type"] == "open_path":
                continue
            if action["type"] == "wechat_login_qr" and self._contains_any(
                command, ["不要", "别", "不用", "取消", "停止"]
            ):
                continue
            triggers = self._terms(action["triggers"])
            matched_triggers = [trigger for trigger in triggers if trigger in command]
            if matched_triggers or self._matches_keyword_groups(
                command, action.get("keyword_groups", [])
            ):
                selected.append((action_id, max(matched_triggers, key=len) if matched_triggers else action_id))

        seen: set[str] = set()
        planned: list[PlannedAction] = []
        for action_id, _ in selected:
            if action_id not in seen:
                seen.add(action_id)
                planned.append(PlannedAction(action_id, self.actions[action_id]["message"]))
        return planned, None

    def execute_action(self, action_id: str, dry_run: bool = False) -> str:
        if action_id not in self.actions:
            raise PermissionError(f"Action is not in the Jarvis whitelist: {action_id}")

        action = self.actions[action_id]
        action_type = action["type"]
        if dry_run:
            return action["message"]

        if action_type == "open_path":
            target = action["target"]
            if not os.path.exists(target):
                raise FileNotFoundError(f"Configured path does not exist: {target}")
            os.startfile(target)
        elif action_type == "status":
            pass
        elif action_type == "lock_pc":
            if ctypes.windll.user32.LockWorkStation() == 0:
                raise OSError("Windows could not lock the workstation")
        elif action_type == "wechat_login_qr":
            # QR delivery requires the authenticated mobile API and must not
            # silently run in the desktop voice controller or a scene.
            raise PermissionError("WeChat login QR requires the mobile API")
        else:  # Guarded by validation; retained as defense in depth.
            raise PermissionError(f"Unsupported action type: {action_type}")
        return action["message"]

    def run(
        self,
        original_text: str,
        normalized_command: str,
        dry_run: bool = False,
        action_executor: Callable[[str, bool], str] | None = None,
    ) -> dict:
        planned, scene_message = self.plan(normalized_command)
        if not planned:
            return {
                "success": False,
                "recognized": False,
                "command": original_text,
                "normalized_command": normalized_command,
                "actions": [],
                "message": "我听懂了，但还不会执行这个指令",
            }

        completed: list[str] = []
        messages: list[str] = []
        errors: list[str] = []
        for item in planned:
            try:
                if action_executor is None:
                    message = self.execute_action(item.action_id, dry_run=dry_run)
                else:
                    message = action_executor(item.action_id, dry_run)
                messages.append(message)
                completed.append(item.action_id)
            except Exception as error:
                errors.append(f"{item.action_id}: {error}")

        success = not errors
        message = scene_message if success and scene_message else "，".join(messages)
        if errors:
            failure_text = "；".join(errors)
            message = f"{message}；部分动作失败：{failure_text}" if message else f"执行失败：{failure_text}"

        return {
            "success": success,
            "recognized": True,
            "command": original_text,
            "normalized_command": normalized_command,
            "actions": completed,
            "message": message,
            **({"errors": errors} if errors else {}),
        }
