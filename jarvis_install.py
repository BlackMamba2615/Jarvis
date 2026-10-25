"""Standard-library setup and launch operations. No credential-file access."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import urllib.request
import venv

from jarvis_settings import BASE_DIR, LOCAL_NAME, _absolute, is_redirect, load_local_settings


class SetupError(RuntimeError):
    pass


def _root(root: Path) -> Path:
    root = _absolute(str(root))
    for name in ("jarvis_probe.py", "requirements.txt", "desktop_controller.py", "main.py"):
        file = root / name
        if is_redirect(file) or not file.is_file():
            raise SetupError("源码目录不完整或含链接；请重新获取可信源码")
    return root


def python_path(root: Path, *, windowed: bool = False) -> Path:
    executable = root / ".voice-venv" / "Scripts" / ("pythonw.exe" if windowed else "python.exe")
    _absolute(str(executable))
    return executable


def _run(args: list[str], root: Path, *, timeout: float = 120, capture: bool = False):
    try:
        return subprocess.run(args, cwd=root, shell=False, timeout=timeout,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                              stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as error:
        raise SetupError("操作未完成；请检查依赖、网络、磁盘及源码路径") from error


def initialize_environment(root: Path = BASE_DIR) -> str:
    root = _root(root)
    if sys.platform != "win32" or sys.version_info[:2] != (3, 12) or struct.calcsize("P") != 8:
        raise SetupError("请使用 Windows 64 位 Python 3.12 启动安装向导")
    target = root / ".voice-venv"
    _absolute(str(target))
    pending = root / ".jarvis-install-pending"
    if is_redirect(pending):
        raise SetupError("安装标记不可为链接")
    if target.exists():
        # An interrupted install may be resumed only for our own new environment.
        if not pending.is_file() or is_redirect(pending):
            raise SetupError("已有虚拟环境：不会自动升级或覆盖；请先运行检测")
    else:
        pending.touch(exist_ok=True)
        try:
            venv.EnvBuilder(with_pip=True).create(target)
        except Exception as error:
            raise SetupError("虚拟环境初始化未完成；请检查 Python、磁盘和目录权限，可再次初始化") from error
    result = _run([str(python_path(root)), "-m", "pip", "--isolated", "--disable-pip-version-check",
                   "install", "--no-input", "--only-binary=:all:", "-r", str(root / "requirements.txt")],
                  root, timeout=1200)
    if result.returncode:
        raise SetupError("依赖安装失败；可再次初始化以续装。请检查 PyPI 网络、磁盘和 Python 版本")
    (root / ".jarvis-install-pending").unlink(missing_ok=True)
    return "环境已安装；尚未下载语音模型或启动麦克风/服务"


def installation_report(root: Path = BASE_DIR) -> dict:
    root = _root(root)
    executable = python_path(root)
    if not executable.is_file():
        return {"base_ready": False, "asr_cached": False, "checks": [
            {"id": "venv", "status": "error", "detail": "尚未初始化 .voice-venv，请点击初始化环境"}]}
    result = _run([str(executable), str(root / "jarvis_probe.py")], root, timeout=90, capture=True)
    try:
        value = json.loads(result.stdout.decode("utf-8"))
        if not isinstance(value.get("checks"), list) or not isinstance(value.get("base_ready"), bool):
            raise ValueError("invalid probe")
        return value
    except (ValueError, UnicodeError, AttributeError) as error:
        raise SetupError("无法取得安装报告；没有读取密钥或启动录音") from error


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def loopback_json(url: str) -> dict:
    if url not in {"http://127.0.0.1:8000/status", "http://127.0.0.1:11434/api/tags"}:
        raise SetupError("检测仅允许固定的本机回环端点")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    with opener.open(url, timeout=2) as response:
        raw = response.read(262_145)
        if len(raw) > 262_144:
            raise SetupError("本机服务响应超过限制")
        value = json.loads(raw)
    if not isinstance(value, dict):
        raise SetupError("本机服务响应无效")
    return value


def service_report(root: Path = BASE_DIR) -> dict:
    selected = load_local_settings(root).get("local_model", "qwen3.8:27b")
    result = {"phone": "unavailable", "ollama": "unavailable", "model_installed": False}
    try:
        value = loopback_json("http://127.0.0.1:8000/status")
        result["phone"] = "online" if value.get("jarvis") == "online" else "unavailable"
    except Exception:
        pass
    try:
        value = loopback_json("http://127.0.0.1:11434/api/tags")
        result["ollama"] = "reachable"
        result["model_installed"] = selected in {m.get("name") for m in value.get("models", []) if isinstance(m, dict)}
    except Exception:
        pass
    return result


def prepare_asr(root: Path = BASE_DIR) -> str:
    root = _root(root)
    result = _run([str(python_path(root)), str(root / "jarvis_probe.py"), "--download-asr"], root, timeout=1800)
    if result.returncode:
        raise SetupError("语音模型下载失败，请检查 Hugging Face 网络与磁盘空间")
    return "语音模型已下载；未进行真人识别或播放测试"


def build_launchers(root: Path = BASE_DIR) -> str:
    root = _root(root)
    compiler = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
    source = root / "jarvis_distribution_launcher.cs"
    _absolute(str(source))
    if not compiler.is_file() or not source.is_file():
        raise SetupError("缺少系统 .NET C# 编译器或启动器源码；仍可使用 CMD 入口")
    builds = (("JarvisSetup.exe", "SETUP"), ("JarvisStart.exe", "LAUNCHER"), ("JarvisCheck.exe", "CHECK"))
    for name, _ in builds:
        if (root / name).exists() or is_redirect(root / name):
            raise SetupError("启动器已存在，不自动覆盖；请先自行备份后移走旧的安装/检查入口")
    for name, define in builds:
        result = _run([str(compiler), "/nologo", "/target:winexe", "/platform:x64", "/codepage:65001",
                       "/define:" + define, "/reference:System.Windows.Forms.dll", "/out:" + str(root / name), str(source)], root)
        if result.returncode:
            raise SetupError("EXE 编译未完成；仍可使用 CMD 入口。未更改 PowerShell 策略")
    return "EXE 启动入口已生成（不含 Python、模型或密钥）"


def prepare_voice_replies(root: Path = BASE_DIR) -> str:
    root = _root(root)
    result = _run([str(python_path(root)), str(root / "jarvis_probe.py"), "--prepare-replies"], root, timeout=1200)
    if result.returncode:
        raise SetupError("固定回复准备失败；请检查离线中文语音和 Skills 配置")
    return "固定技能回复已准备；未通过扬声器播放"


def _spawn(root: Path, args: list[str], name: str, *, env=None):
    logs = root / "logs"
    _absolute(str(logs))
    logs.mkdir(exist_ok=True)
    path = logs / name
    if is_redirect(path):
        raise SetupError("日志文件不允许是链接")
    try:
        with path.open("ab") as stream:
            return subprocess.Popen(args, cwd=root, shell=False, env=env,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                    stdin=subprocess.DEVNULL, stdout=stream, stderr=stream)
    except OSError as error:
        raise SetupError("启动失败；请运行安装检测") from error


def _configured(root: Path) -> None:
    if not (root / LOCAL_NAME).is_file():
        raise SetupError("请先在向导中保存本机设置，确认本机项目目录与应用路径")
    load_local_settings(root)


def start_desktop(root: Path = BASE_DIR, *, report=None) -> str:
    root = _root(root)
    _configured(root)
    report = report if report is not None else installation_report(root)
    if not report.get("base_ready") or not report.get("asr_cached"):
        raise SetupError("请先通过基础检测并下载语音模型")
    _spawn(root, [str(python_path(root, windowed=True)), str(root / "desktop_controller.py")], "desktop_launcher.log")
    return "已请求启动桌面监听；以桌面就绪提示和实时心跳为准"


def start_phone(root: Path = BASE_DIR, *, phone_key: str = "", report=None) -> str:
    root = _root(root)
    if is_redirect(root / ".env") or is_redirect(root / "openai.env"):
        raise SetupError("认证配置不允许符号链接；安装器不会跟随或读取链接")
    if phone_key and (not 16 <= len(phone_key) <= 256 or any(ord(c) < 33 or ord(c) > 126 for c in phone_key)):
        raise SetupError("本次连接密码须为 16 至 256 个非空格 ASCII 字符")
    if service_report(root)["phone"] == "online":
        return "已有手机服务在线；未重启或更改认证"
    _configured(root)
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", 8000)) == 0:
            raise SetupError("8000 端口已占用，不会停止已有进程")
    report = report if report is not None else installation_report(root)
    if not report.get("base_ready") or not report.get("asr_cached"):
        raise SetupError("请先通过基础检测并下载语音模型")
    auth_file = root / ".env"
    if not phone_key and "JARVIS_API_KEY" not in os.environ and not (auth_file.is_file() and not is_redirect(auth_file)):
        raise SetupError("请输入仅本次使用的连接密码，或自行安全配置本机认证；安装器不会读取或写入 .env")
    env = None
    if phone_key:
        # An explicitly typed password is sent only in this child's environment.
        # Never in argv, the parent environment, files, logs, or command shells.
        env = dict(os.environ)
        env["JARVIS_API_KEY"] = phone_key
    _spawn(root, [str(python_path(root, windowed=True)), "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8000"],
           "phone_launcher.log", env=env)
    return "已请求启动手机服务；请稍后点击检查本地服务确认 online"
