"""Strict non-secret, machine-local overrides; no env-file access."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import time

BASE_DIR = Path(__file__).resolve().parent
LOCAL_NAME = "jarvis.local.json"
TARGET_NAMES = {
    "open_wechat": {"weixin.exe", "wechat.exe"},
    "open_calculator": {"calc.exe"},
    "open_notepad": {"notepad.exe"},
    "open_explorer": {"explorer.exe"},
    "open_jarvis": None,
}


class SettingsError(ValueError):
    pass


def is_redirect(path: Path) -> bool:
    """Reject symlinks and Windows junctions, including dangling links."""
    try:
        info = path.lstat()
        return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)
    except FileNotFoundError:
        return False


def validate_settings(value: dict) -> dict:
    if not isinstance(value, dict) or type(value.get("version")) is not int or value.get("version") != 1:
        raise SettingsError("本机配置须为 version=1 的对象")
    if set(value) - {"version", "project_root", "local_model", "skill_targets"}:
        raise SettingsError("本机配置含不允许的字段；不得保存密钥或服务地址")
    for name in ("project_root",):
        if name in value:
            path = _absolute(value[name])
            if path.exists() and not path.is_dir():
                raise SettingsError("项目根目录必须是目录")
    if "local_model" in value and (
        not isinstance(value["local_model"], str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", value["local_model"])
    ):
        raise SettingsError("本地模型名称无效")
    targets = value.get("skill_targets", {})
    if not isinstance(targets, dict) or set(targets) - set(TARGET_NAMES):
        raise SettingsError("仅允许覆盖已有白名单应用的路径")
    for action, target in targets.items():
        path = _absolute(target)
        names = TARGET_NAMES[action]
        if names is not None and path.name.casefold() not in names:
            raise SettingsError("应用文件名不符合对应白名单技能")
        if names is not None and path.exists() and not path.is_file():
            raise SettingsError("应用目标必须是程序文件")
        if names is None and path.exists() and not path.is_dir():
            raise SettingsError("Jarvis 文件夹目标必须是目录")
    return value


def _absolute(value: str) -> Path:
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise SettingsError("路径必须是非空绝对路径")
    path = Path(value)
    if not path.is_absolute() or value.startswith(("\\\\", "//")):
        raise SettingsError("只允许本机绝对路径，不允许网络共享")
    # Check each existing ancestor without first resolving away junctions.
    if any(is_redirect(p) for p in (path, *path.parents)):
        raise SettingsError("本机配置路径不允许符号链接或目录联接")
    return path


def load_local_settings(root: Path = BASE_DIR) -> dict:
    path = Path(root) / LOCAL_NAME
    if is_redirect(path):
        raise SettingsError("本机配置不可是链接")
    if not path.exists():
        return {"version": 1}
    if path.stat().st_size > 16_384:
        raise SettingsError("本机配置超过大小限制")
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise SettingsError("无法读取本机配置；未访问任何密钥文件") from error
    return validate_settings(value)


def save_local_settings(root: Path, value: dict) -> None:
    """Only a wizard's explicit Save action calls this; never touches env files."""
    validate_settings(value)
    root = _absolute(str(root))
    target = root / LOCAL_NAME
    if is_redirect(target):
        raise SettingsError("本机配置不可是链接")
    if target.exists():
        backup_root = root / "backups"
        _absolute(str(backup_root))
        backup_root.mkdir(exist_ok=True)
        backup = backup_root / ("setup-" + str(time.time_ns()) + ".json")
        shutil.copy2(target, backup)
    fd, name = tempfile.mkstemp(prefix=".jarvis-settings-", dir=root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def apply_skill_targets(config: dict, root: Path) -> dict:
    for action, target in load_local_settings(root).get("skill_targets", {}).items():
        definition = config.get("actions", {}).get(action)
        if not isinstance(definition, dict) or definition.get("type") != "open_path":
            raise SettingsError("本机路径不能创建新技能或更改技能类型")
        definition["target"] = target
    return config
