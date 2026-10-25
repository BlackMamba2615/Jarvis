"""Read-only installation probe. No application imports, env reads or audio."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import sys

from jarvis_settings import LOCAL_NAME, _absolute, is_redirect, load_local_settings

DEPENDENCIES = {
    "fastapi": "fastapi", "uvicorn": "uvicorn", "python-multipart": "python_multipart",
    "python-dotenv": "dotenv", "faster-whisper": "faster_whisper",
    "opencc-python-reimplemented": "opencc", "numpy": "numpy", "pypinyin": "pypinyin",
    "pywin32": "win32crypt", "Pillow": "PIL.Image", "opencv-python-headless": "cv2",
    "requests": "requests", "httpx": "httpx",
}


def cached_asr() -> bool:
    try:
        from huggingface_hub import try_to_load_from_cache
        for name in ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt"):
            cached = try_to_load_from_cache("Systran/faster-whisper-small", name)
            if not isinstance(cached, str) or not Path(cached).is_file():
                return False
        return True
    except Exception:
        return False


def probe(root: Path) -> dict:
    checks = []

    def add(name, status, detail):
        checks.append({"id": name, "status": status, "detail": detail})

    add("windows", "ok" if sys.platform == "win32" else "error", "需要 Windows 桌面环境")
    add("python", "ok" if sys.version_info[:2] == (3, 12) and platform.architecture()[0] == "64bit" else "error",
        "需要 64 位 Python 3.12")
    for package, module in DEPENDENCIES.items():
        try:
            importlib.import_module(module)
            version = importlib.metadata.version(package)
            add("dependency:" + package, "ok", package + " " + version)
        except Exception:
            add("dependency:" + package, "error", package + " 未安装或无法加载")
    try:
        importlib.import_module("tkinter")
        add("tk", "ok", "Tk 图形组件可导入；未创建窗口")
    except Exception:
        add("tk", "error", "Python 缺少 Tcl/Tk 图形组件")
    add("asr", "ok" if cached_asr() else "warning", "只检查本机 small 缓存；不下载或运行模型")
    try:
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        try:
            speaker = win32com.client.Dispatch("SAPI.SpVoice")
            chinese = any("804" in voice.GetAttribute("Language").lower().split(";") for voice in speaker.GetVoices())
            del speaker
        finally:
            pythoncom.CoUninitialize()
        add("chinese_voice", "ok" if chinese else "warning", "中文 SAPI 语音" if chinese else "请安装 Windows 离线中文语音")
    except Exception:
        add("chinese_voice", "warning", "无法确认离线中文语音；未播放声音")
    try:
        count = ctypes.WinDLL("winmm").waveInGetNumDevs()
        add("microphone", "ok" if count else "warning", "检测到输入设备；未打开麦克风，权限与实际音量仍须实测")
    except Exception:
        add("microphone", "warning", "无法枚举输入设备；未打开麦克风")
    # Presence only: never parse/read/hash the credential files or their values.
    for name, filename, env_name in (("phone_auth", ".env", "JARVIS_API_KEY"), ("cloud_auth", "openai.env", "OPENAI_API_KEY")):
        target = root / filename
        present = env_name in os.environ or (not is_redirect(target) and target.is_file())
        add(name, "warning", "配置存在但未读取/未验证" if present else "未检测到配置；可选功能需另行配置")
    try:
        importlib.import_module("openai_codex")
        sdk = True
    except Exception:
        sdk = False
    add("codex_sdk", "ok" if sdk else "warning", "项目任务接口已安装；未登录/运行任务" if sdk else "可选的项目任务接口未安装")
    try:
        settings = load_local_settings(root)
        present = (root / LOCAL_NAME).is_file()
        add("local_settings", "ok" if present else "warning", "本机非敏感配置有效" if present else "尚未保存本机设置；启动中心会要求先保存")
    except Exception:
        settings = {}
        add("local_settings", "error", "本机非敏感配置无效；请用向导重新保存")
    project = Path(settings.get("project_root", r"D:\LYY_work"))
    add("projects", "ok" if project.is_dir() and not is_redirect(project) else "warning", "项目根目录存在" if project.is_dir() else "请在向导中选择已有项目根目录")
    try:
        from jarvis_skills import SkillSystem
        skills = SkillSystem.load(root / "skills.json")
        missing = [name for name, item in skills.actions.items() if item.get("type") == "open_path" and not Path(item["target"]).exists()]
        add("skill_paths", "warning" if missing else "ok", "请检查应用路径：" + ", ".join(missing) if missing else "已配置的白名单应用路径存在")
    except Exception:
        add("skill_paths", "error", "Skills 配置无法加载；没有执行任何电脑操作")
    add("mask", "ok" if (root / "assets" / "ironman-mask-full.png").is_file() else "warning", "面具素材存在" if (root / "assets" / "ironman-mask-full.png").is_file() else "使用内置简化面具；受限图片不随 Git 分发")
    return {"checks": checks, "base_ready": not any(item["status"] == "error" for item in checks),
            "asr_cached": any(item["id"] == "asr" and item["status"] == "ok" for item in checks)}


def download_asr() -> None:
    """Only invoked by an explicit user action; no model inference or token."""
    from huggingface_hub import snapshot_download
    snapshot_download("Systran/faster-whisper-small", token=False,
                      allow_patterns=["config.json", "model.bin", "tokenizer.json", "vocabulary.txt"])


def prepare_replies(root: Path) -> int:
    """Generate only configured fixed replies to a local WAV cache, never play."""
    from dynamic_tts import synthesize_wave
    from jarvis_skills import SkillSystem
    skills = SkillSystem.load(root / "skills.json")
    messages = {"语音回复已开启", "任务完成", "我听懂了，但还不会执行这个指令", "没有听清，请再说一次"}
    action_messages = [value["message"] for value in skills.actions.values()]
    if len(action_messages) > 10:
        raise ValueError("Too many actions to safely pre-generate combinations")
    for mask in range(1, 1 << len(action_messages)):
        messages.add("，".join(message for index, message in enumerate(action_messages) if mask & (1 << index)))
    messages.update(value["message"] for value in skills.scenes.values())
    messages.update(item for item in skills.settings.get("speech_messages", []) if isinstance(item, str))
    cache = _absolute(str(root / "speech_cache"))
    cache.mkdir(exist_ok=True)
    generated = 0
    for message in sorted(messages):
        if not message or len(message) > 200:
            continue
        target = cache / (hashlib.sha256(message.encode("utf-8")).hexdigest() + ".wav")
        if is_redirect(target):
            raise ValueError("Linked cache target")
        if target.exists():
            continue
        wave = synthesize_wave(message)
        try:
            # Exclusive creation avoids replacing pre-existing user audio.
            with wave.open("rb") as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
            generated += 1
        finally:
            wave.unlink(missing_ok=True)
    return generated


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--download-asr", action="store_true")
    parser.add_argument("--prepare-replies", action="store_true")
    args = parser.parse_args()
    if args.download_asr:
        try:
            download_asr()
            return 0
        except Exception:
            print("ASR download failed; check network and disk space.")
            return 1
    if args.prepare_replies:
        try:
            print(json.dumps({"generated": prepare_replies(Path(__file__).resolve().parent)}))
            return 0
        except Exception:
            print("Offline reply preparation failed; check the local Chinese voice and configuration.")
            return 1
    result = probe(Path(__file__).resolve().parent)
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result["base_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
