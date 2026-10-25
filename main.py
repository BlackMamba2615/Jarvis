import hashlib
import json
import os
from pathlib import Path
import queue
import secrets
import tempfile
import threading
from datetime import datetime

from dotenv import load_dotenv
from fastapi import (
    FastAPI,
    HTTPException,
    Security,
    UploadFile,
    File,
)
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from fastapi.security import APIKeyHeader
from faster_whisper import WhisperModel
from pydantic import BaseModel
from opencc import OpenCC
from starlette.concurrency import run_in_threadpool
from jarvis_skills import SkillSystem
from dynamic_tts import synthesize_wave
from codex_gateway import (
    CodexBridgeUnavailable,
    InvalidProject,
    confirm_draft as confirm_codex_draft,
    create_draft as create_codex_draft,
    is_project_task,
    list_projects as list_codex_projects,
    task_status as codex_task_status,
    list_tasks as list_codex_tasks,
    cancel_task as cancel_codex_task,
)
from chat_router import chat as assistant_chat
from chat_router import get_last_route
from chat_sessions import (ChatCancelled, remember, list_memories, delete_memory,
                           clear_history, clear_memories)
from jarvis_health import collect_health, check_cloud
from screen_vision import (ScreenUnavailable, prepare_preview, preview_bytes,
                           analyze_preview, discard_preview)
from local_llm import LocalModelUnavailable
from openai_chat import OpenAIAPIUnavailable
from wechat_desktop_bridge import request_open_wechat_from_desktop
from wechat_login_qr import (
    LoginQrUnavailable,
    QR_LIFETIME_SECONDS,
    consume_login_qr,
    request_login_qr,
)


# ============================================================
# Environment
# ============================================================

load_dotenv()

JARVIS_API_KEY = os.getenv("JARVIS_API_KEY")

if not JARVIS_API_KEY:
    raise RuntimeError(
        "JARVIS_API_KEY is not configured. "
        "Configure it locally before starting the phone service."
    )


# ============================================================
# FastAPI
# ============================================================

app = FastAPI(
    title="Jarvis PC Agent",
    version="4.1.0",
    description="Personal Jarvis PC Voice Agent",
)


# ============================================================
# Security
# ============================================================

api_key_header = APIKeyHeader(
    name="X-Jarvis-Key",
    auto_error=False,
)


def verify_api_key(
    api_key: str = Security(api_key_header),
):
    if api_key != JARVIS_API_KEY:
        raise HTTPException(
            status_code=401,
            detail="Invalid Jarvis API key.",
        )

    return api_key


# ============================================================
# Simplified / Traditional Chinese Converter
# ============================================================

# t2s = Traditional Chinese -> Simplified Chinese
chinese_converter = OpenCC("t2s")


# ============================================================
# Whisper Voice Engine
# ============================================================

print()
print("=" * 60)
print("JARVIS")
print("=" * 60)
print("Loading Jarvis Voice Model...")

voice_model = WhisperModel(
    "small",
    device="cpu",
    compute_type="int8",
)

print("Jarvis Voice Model READY")
print("=" * 60)
print()


# ============================================================
# Request Model
# ============================================================

class CommandRequest(BaseModel):
    command: str


class SpeechRequest(BaseModel):
    text: str


class CodexConfirmRequest(BaseModel):
    token: str
    project: str


class ChatRequest(BaseModel):
    text: str


class ScreenRequest(BaseModel):
    confirmed: bool = False
    token: str = ""
    question: str = "请解释当前窗口，若有报错请说明可能原因和安全的下一步。"


_CHAT_REQUESTS: dict[str, threading.Event] = {}
_CHAT_REQUESTS_LOCK = threading.Lock()


def chat_stream_response(text: str, *, transcript: str | None = None):
    """One authenticated mobile stream. Disconnect cancels output, not Skills."""
    if not text.strip() or len(text.strip()) > 2000:
        raise HTTPException(status_code=400, detail="聊天内容须为 1 至 2000 字")
    request_id = secrets.token_urlsafe(18)
    cancel = threading.Event()
    messages = queue.Queue(maxsize=128)
    with _CHAT_REQUESTS_LOCK:
        for previous in _CHAT_REQUESTS.values():
            previous.set()
        _CHAT_REQUESTS[request_id] = cancel

    def emit(value):
        if cancel.is_set():
            raise ChatCancelled()
        while not cancel.is_set():
            try:
                messages.put(value, timeout=0.2)
                return
            except queue.Full:
                pass
        raise ChatCancelled()

    def worker():
        route = {}
        try:
            answer = assistant_chat(text, session_id="mobile", on_delta=lambda delta: emit(
                {"type": "delta", "text": delta}), cancel_event=cancel, route_out=route)
            emit({"type": "result", "success": True, "recognized": True, "actions": [],
                  "message": answer, "route": route, "streamed": True})
        except ChatCancelled:
            pass
        except (ValueError, LocalModelUnavailable, OpenAIAPIUnavailable):
            if not cancel.is_set():
                emit({"type": "result", "success": False, "recognized": True,
                      "message": "聊天服务暂时不可用，电脑技能仍可正常使用",
                      "route": route, "streamed": True})
        except Exception:
            if not cancel.is_set():
                emit({"type": "result", "success": False, "message": "聊天未完成，请稍后再试", "streamed": True})
        finally:
            if not cancel.is_set():
                try:
                    messages.put(None, timeout=1)
                except queue.Full:
                    cancel.set()

    def generate():
        try:
            yield json.dumps({"type": "start", "request_id": request_id,
                              "transcript": transcript}, ensure_ascii=False) + "\n"
            threading.Thread(target=worker, name="mobile-chat-stream", daemon=True).start()
            while not cancel.is_set():
                try:
                    value = messages.get(timeout=1)
                except queue.Empty:
                    yield '{"type":"pending"}\n'
                    continue
                if value is None:
                    break
                yield json.dumps(value, ensure_ascii=False) + "\n"
        finally:
            cancel.set()
            with _CHAT_REQUESTS_LOCK:
                _CHAT_REQUESTS.pop(request_id, None)

    class CancellableChatResponse(StreamingResponse):
        async def __call__(self, scope, receive, send):
            try:
                await super().__call__(scope, receive, send)
            finally:
                cancel.set()
                with _CHAT_REQUESTS_LOCK:
                    _CHAT_REQUESTS.pop(request_id, None)

    return CancellableChatResponse(generate(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


# ============================================================
# Command Normalizer
# ============================================================

def normalize_command(text: str) -> str:

    if not text:
        return ""

    # --------------------------------------------------------
    # 1. 去除首尾空格
    # --------------------------------------------------------

    text = text.strip()

    # --------------------------------------------------------
    # 2. 繁体 -> 简体
    # --------------------------------------------------------

    text = chinese_converter.convert(text)

    # --------------------------------------------------------
    # 3. 英文转小写
    # --------------------------------------------------------

    text = text.lower()

    # --------------------------------------------------------
    # 4. 去除标点
    # --------------------------------------------------------

    punctuation = skill_system.setting_list("punctuation")

    for char in punctuation:
        text = text.replace(char, "")

    # --------------------------------------------------------
    # 5. 去除多余空格
    # --------------------------------------------------------

    text = " ".join(text.split())

    # --------------------------------------------------------
    # 6. 去除开头唤醒词
    # --------------------------------------------------------

    wake_words = skill_system.setting_list("wake_words")

    for wake_word in wake_words:

        if text.startswith(wake_word):

            text = text[
                len(wake_word):
            ].strip()

            break

    return text


# ============================================================
# Config-driven Skill System V4
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SKILLS_CONFIG_PATH = os.path.join(BASE_DIR, "skills.json")

skill_system = SkillSystem.load(
    SKILLS_CONFIG_PATH,
    normalize_term=lambda value: chinese_converter.convert(value).lower().strip(),
)


def open_allowed_app(app_name: str):
    """Keep the existing protected API while routing through the whitelist."""

    legacy_names = {
        "wechat": "open_wechat",
        "notepad": "open_notepad",
        "explorer": "open_explorer",
        "jarvis": "open_jarvis",
    }
    action_id = legacy_names.get(app_name.lower(), app_name.lower())
    action = skill_system.actions.get(action_id)
    if not action or action.get("type") != "open_path":
        raise HTTPException(
            status_code=403,
            detail="Application is not in Jarvis whitelist.",
        )
    try:
        skill_system.execute_action(action_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


def execute_command(text: str, dry_run: bool = False):
    """Match speech to allowlisted action ids, then execute those ids only."""

    original_text = text
    command = normalize_command(text)

    planned, _scene_message = skill_system.plan(command)
    action_ids = [item.action_id for item in planned]
    if "wechat_login_qr" in action_ids:
        # The QR request is exclusive. The ordinary "open_wechat" match in a
        # phrase like "打开微信登录二维码" is folded into this one operation.
        if set(action_ids) - {"wechat_login_qr", "open_wechat"}:
            return {
                "success": False,
                "recognized": True,
                "command": original_text,
                "normalized_command": command,
                "actions": [],
                "message": "请单独说微信登录二维码指令",
            }
        if dry_run:
            return {
                "success": True,
                "recognized": True,
                "command": original_text,
                "normalized_command": command,
                "actions": ["wechat_login_qr"],
                "message": "微信登录二维码已发送到手机",
            }
        try:
            token = request_login_qr()
        except LoginQrUnavailable as error:
            return {
                "success": False,
                "recognized": True,
                "command": original_text,
                "normalized_command": command,
                "actions": [],
                "message": str(error),
            }
        return {
            "success": True,
            "recognized": True,
            "command": original_text,
            "normalized_command": command,
            "actions": ["wechat_login_qr"],
            "message": "微信登录二维码已发送到手机；请用已登录目标账号的微信扫描",
            "speech_text": "微信已打开",
            "qr_token": token,
            "qr_expires_seconds": QR_LIFETIME_SECONDS,
        }

    def execute_mobile_action(action_id: str, action_dry_run: bool) -> str:
        if action_id == "open_wechat":
            if action_dry_run:
                return skill_system.actions[action_id]["message"]
            return request_open_wechat_from_desktop()
        return skill_system.execute_action(action_id, dry_run=action_dry_run)

    return skill_system.run(
        original_text=original_text,
        normalized_command=command,
        dry_run=dry_run,
        action_executor=execute_mobile_action,
    )


def handle_mobile_voice(text: str, *, allow_chat: bool = True):
    """Preserve deterministic skills; route coding tasks through confirmation."""
    if is_project_task(text):
        projects = list_codex_projects()
        if not projects:
            return {
                "success": False, "recognized": True, "actions": [],
                "message": "已配置的项目目录暂无项目；请先放入项目",
            }
        try:
            token = create_codex_draft(text)
        except ValueError:
            return {
                "success": False, "recognized": True, "actions": [],
                "message": "项目任务描述过长，请简短重说",
            }
        return {
            "success": False, "recognized": True, "actions": [],
            "message": "请在手机上选择项目并确认，Codex 才会开始修改",
            "codex_draft_token": token,
            "codex_projects": projects,
        }
    result = execute_command(text)
    if result.get("recognized"):
        return result
    if not allow_chat:
        return None
    route = {}
    try:
        answer = assistant_chat(text, session_id="mobile", route_out=route)
    except (ValueError, LocalModelUnavailable, OpenAIAPIUnavailable):
        answer = "聊天服务暂时不可用，电脑技能仍可正常使用"
        success = False
    else:
        success = True
    return {
        "success": success, "recognized": True, "actions": [],
        "message": answer,
        "speech_text": answer[:180],
        "route": route,
    }


# ============================================================
# Public API
# ============================================================

@app.get("/")
def home():

    return {
        "name": "JARVIS",
        "status": "ONLINE",
        "voice": "READY",
        "version": "4.1.0",
        "traditional_chinese": "SUPPORTED",
    }


@app.get("/status")
def status():
    health = collect_health(phone_voice_ready=voice_model is not None)
    return {
        "jarvis": "online",
        "voice": health["phone_voice"],
        "pc": health["desktop_bridge"],
        "traditional_chinese": "supported",
        "time": datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
    }


@app.get("/health/details")
def health_details(api_key: str = Security(verify_api_key)):
    health = collect_health(phone_voice_ready=voice_model is not None)
    health["route"] = get_last_route("mobile")
    return health


@app.post("/health/cloud-check")
def cloud_check(api_key: str = Security(verify_api_key)):
    return check_cloud()


# ============================================================
# Protected App API
# ============================================================

@app.post("/apps/open/{app_name}")
def open_app(
    app_name: str,
    api_key: str = Security(
        verify_api_key
    ),
):

    open_allowed_app(
        app_name
    )

    return {
        "success": True,
        "action": "open_app",
        "app": app_name,
        "message":
            f"{app_name} opened successfully.",
    }


# ============================================================
# Text Command API
# ============================================================

@app.post("/command")
def command(
    request: CommandRequest,
    api_key: str = Security(
        verify_api_key
    ),
):

    return execute_command(
        request.command
    )


@app.post("/ai/chat")
def ai_chat(request: ChatRequest, api_key: str = Security(verify_api_key)):
    route = {}
    try:
        answer = assistant_chat(request.text, session_id="mobile", route_out=route)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except (LocalModelUnavailable, OpenAIAPIUnavailable) as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return {"message": answer, "route": route}


@app.post("/ai/chat/stream")
def ai_chat_stream(request: ChatRequest, api_key: str = Security(verify_api_key)):
    return chat_stream_response(request.text)


@app.post("/ai/cancel/{request_id}")
def cancel_chat(request_id: str, api_key: str = Security(verify_api_key)):
    with _CHAT_REQUESTS_LOCK:
        event = _CHAT_REQUESTS.get(request_id)
        if event is not None:
            event.set()
    return {"cancelled": event is not None}


@app.get("/ai/memories")
def memories_list(api_key: str = Security(verify_api_key)):
    return {"memories": list_memories("mobile")}


@app.post("/ai/memories")
def memories_add(request: ChatRequest, api_key: str = Security(verify_api_key)):
    try:
        return remember("mobile", request.text)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.delete("/ai/memories/{memory_id}")
def memories_delete(memory_id: str, api_key: str = Security(verify_api_key)):
    return {"deleted": delete_memory("mobile", memory_id)}


@app.delete("/ai/history")
def history_clear(api_key: str = Security(verify_api_key)):
    clear_history("mobile")
    return {"cleared": True}


@app.post("/screen/preview")
def screen_preview(request: ScreenRequest, api_key: str = Security(verify_api_key)):
    if not request.confirmed:
        raise HTTPException(status_code=400, detail="请先明确确认截取电脑当前窗口")
    try:
        return {"token": prepare_preview(), "expires_seconds": 45, "local_only": True}
    except ScreenUnavailable as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/screen/preview/{token}")
def screen_preview_image(token: str, api_key: str = Security(verify_api_key)):
    try:
        return Response(preview_bytes(token), media_type="image/png", headers={"Cache-Control": "no-store"})
    except ScreenUnavailable as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.delete("/screen/preview/{token}")
def screen_preview_delete(token: str, api_key: str = Security(verify_api_key)):
    discard_preview(token)
    return {"discarded": True}


@app.post("/screen/analyze")
def screen_analyze(request: ScreenRequest, api_key: str = Security(verify_api_key)):
    if not request.confirmed:
        raise HTTPException(status_code=400, detail="请先预览并确认本地分析")
    try:
        return {"message": analyze_preview(request.token, request.question), "backend": "local-vision",
                "local_only": True}
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except ScreenUnavailable as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/codex/projects")
def codex_projects(api_key: str = Security(verify_api_key)):
    return {"projects": list_codex_projects()}


@app.post("/codex/confirm")
def codex_confirm(request: CodexConfirmRequest, api_key: str = Security(verify_api_key)):
    try:
        task_id = confirm_codex_draft(request.token, request.project)
    except (InvalidProject, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except CodexBridgeUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return {"task_id": task_id, "status": "queued"}


@app.get("/codex/tasks/{task_id}")
def codex_task(task_id: str, api_key: str = Security(verify_api_key)):
    try:
        return codex_task_status(task_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="未找到该任务") from error


@app.get("/codex/tasks")
def codex_tasks(api_key: str = Security(verify_api_key)):
    return {"tasks": list_codex_tasks()}


@app.post("/codex/tasks/{task_id}/cancel")
def codex_cancel(task_id: str, api_key: str = Security(verify_api_key)):
    try:
        return cancel_codex_task(task_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="未找到该任务") from error


@app.get("/wechat/login-qr/{token}")
def get_wechat_login_qr(
    token: str,
    api_key: str = Security(verify_api_key),
):
    # One-time, short-lived delivery. Never place the API key in the URL.
    qr_png = consume_login_qr(token)
    if qr_png is None:
        raise HTTPException(status_code=404, detail="二维码已过期，请重新发出指令")
    return Response(
        content=qr_png,
        media_type="image/png",
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


# ============================================================
# Allowlisted Offline Speech Cache API
# ============================================================

SPEECH_CACHE_DIRECTORY = os.path.join(
    BASE_DIR,
    "speech_cache",
)


def speech_cache_path(
    text: str,
) -> str:

    digest = hashlib.sha256(
        text.encode("utf-8")
    ).hexdigest()

    return os.path.join(
        SPEECH_CACHE_DIRECTORY,
        f"{digest}.wav",
    )


@app.post("/speech")
def speech(
    request: SpeechRequest,
    api_key: str = Security(
        verify_api_key
    ),
):

    text = request.text.strip()

    if not text:
        raise HTTPException(
            status_code=400,
            detail="Speech text is empty.",
        )

    if len(text) > 200:
        raise HTTPException(
            status_code=400,
            detail="Speech text is too long.",
        )

    output_path = speech_cache_path(
        text
    )

    if not os.path.exists(output_path):
        raise HTTPException(
            status_code=404,
            detail=(
                "Speech is not in the allowlisted cache. "
                "Run generate_speech_cache.ps1."
            ),
        )

    return FileResponse(
        output_path,
        media_type="audio/wav",
        filename="jarvis-reply.wav",
    )


@app.post("/speech/dynamic")
def speech_dynamic(request: SpeechRequest, api_key: str = Security(verify_api_key)):
    try:
        output_path = synthesize_wave(request.text)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=503, detail="离线语音暂时不可用") from error
    return FileResponse(
        output_path,
        media_type="audio/wav",
        filename="jarvis-reply.wav",
        background=BackgroundTask(output_path.unlink, missing_ok=True),
    )

# ============================================================
# Voice Command API
# ============================================================

@app.post("/voice-command")
async def voice_command(
    audio: UploadFile = File(...),
    stream: bool = False,
    api_key: str = Security(
        verify_api_key
    ),
):

    temp_path = None

    try:

        content = await audio.read()

        if not content:

            raise HTTPException(
                status_code=400,
                detail="Empty audio file.",
            )


        # 最大 10MB
        if (
            len(content)
            >
            10 * 1024 * 1024
        ):

            raise HTTPException(
                status_code=413,
                detail=
                    "Audio file is too large.",
            )


        filename = (
            audio.filename
            or
            "voice.webm"
        )

        suffix = os.path.splitext(
            filename
        )[1]

        if not suffix:
            suffix = ".webm"


        with tempfile.NamedTemporaryFile(
            delete=False,
            suffix=suffix,
        ) as temp_file:

            temp_file.write(
                content
            )

            temp_path = (
                temp_file.name
            )


        print()
        print(
            "[VOICE] Audio received"
        )

        print(
            f"[VOICE] Temporary file: "
            f"{temp_path}"
        )


        # ====================================================
        # Whisper
        # ====================================================

        segments, info = (
            voice_model.transcribe(
                temp_path,
                language="zh",
                beam_size=5,
                vad_filter=True,

                # 提示 Whisper 更倾向输出简体中文
                initial_prompt=(
                    "以下是简体中文语音指令。"
                    "常见指令包括："
                    "打开微信，"
                    "帮我在电脑上登录微信，"
                    "电脑微信扫码登录，"
                    "在计算机上用二维码登录微信，"
                    "打开记事本，"
                    "打开文件夹，"
                    "打开资源管理器，"
                    "打开贾维斯，"
                    "开始工作，"
                    "电脑状态，"
                    "锁定电脑。"
                ),
            )
        )


        transcript_parts = []

        for segment in segments:

            segment_text = (
                segment.text.strip()
            )

            if segment_text:

                transcript_parts.append(
                    segment_text
                )


        raw_transcript = "".join(
            transcript_parts
        ).strip()


        # ====================================================
        # 无论 Whisper 输出简体还是繁体
        # 强制统一成简体
        # ====================================================

        simplified_transcript = (
            chinese_converter.convert(
                raw_transcript
            )
        )


        # Keep recognized speech in memory only, never in process logs.


        if not raw_transcript:

            return {
                "success": False,
                "recognized": False,
                "transcript": "",
                "message":
                    "没有听清，请再说一次",
            }


        # ====================================================
        # 执行统一后的简体指令
        # ====================================================

        result = await run_in_threadpool(
            handle_mobile_voice, simplified_transcript, allow_chat=not stream,
        )
        if result is None:
            return chat_stream_response(simplified_transcript, transcript=simplified_transcript)


        # 手机上仍然可以看到 Whisper 原始结果
        result["raw_transcript"] = (
            raw_transcript
        )

        # 手机主要显示标准化后的简体结果
        result["transcript"] = (
            simplified_transcript
        )

        result["language"] = (
            info.language
        )


        return result


    except HTTPException:
        raise


    except Exception as error:

        print(f"[VOICE ERROR] type={type(error).__name__}")

        raise HTTPException(
            status_code=500,
            detail="Voice processing failed; please try again.",
        )


    finally:

        if (
            temp_path
            and
            os.path.exists(
                temp_path
            )
        ):

            try:

                os.remove(
                    temp_path
                )

            except Exception:

                pass


# ============================================================
# Mobile Control Panel
# ============================================================

@app.get(
    "/control",
    response_class=HTMLResponse,
)
def control_panel():

    return """
<!DOCTYPE html>

<html lang="zh-CN">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width, initial-scale=1.0">

<title>JARVIS</title>

<style>

* {
    box-sizing: border-box;
}

body {

    margin: 0;

    min-height: 100vh;

    background:
        radial-gradient(
            circle at top,
            #102536,
            #05080d 55%
        );

    color: #e8f7ff;

    font-family:
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        Arial,
        sans-serif;

    text-align: center;
}


.container {

    max-width: 520px;

    margin: auto;

    padding:
        55px
        22px
        40px;
}


.logo {

    letter-spacing: 13px;

    font-size: 34px;

    font-weight: 500;

    margin-left: 13px;
}


.subtitle {

    margin-top: 8px;

    color: #607989;

    font-size: 12px;

    letter-spacing: 3px;
}


.status {

    margin-top: 30px;

    color: #59ff9b;

    font-size: 14px;
}


.mic-container {

    margin-top: 55px;

    margin-bottom: 35px;
}


.mic {

    width: 175px;

    height: 175px;

    border-radius: 50%;

    border:
        2px solid #55cfff;

    background:
        radial-gradient(
            circle,
            #132d40,
            #08121b
        );

    color: white;

    font-size: 65px;

    box-shadow:
        0 0 20px
        rgba(
            0,
            170,
            255,
            0.4
        ),
        0 0 60px
        rgba(
            0,
            170,
            255,
            0.15
        );

    transition:
        0.2s ease;

    cursor: pointer;
}


.mic:active {

    transform:
        scale(0.95);
}


.mic.recording {

    border-color:
        #ff4967;

    box-shadow:
        0 0 30px
        rgba(
            255,
            40,
            80,
            0.7
        ),
        0 0 80px
        rgba(
            255,
            40,
            80,
            0.3
        );

    animation:
        pulse
        1.2s
        infinite;
}


@keyframes pulse {

    0% {
        transform:
            scale(1);
    }

    50% {
        transform:
            scale(1.04);
    }

    100% {
        transform:
            scale(1);
    }
}


.state {

    margin-top: 15px;

    font-size: 21px;

    min-height: 30px;
}


.transcript {

    margin-top: 20px;

    color: #8da2ae;

    font-size: 17px;

    min-height: 25px;
}


.reply {

    margin-top: 14px;

    color: #55cfff;

    font-size: 20px;

    min-height: 30px;
}

.wechat-qr {
    display: none;
    margin: 18px auto 0;
    padding: 18px;
    max-width: 340px;
    border: 1px solid #247b90;
    border-radius: 16px;
    background: #0c1b24;
}

.wechat-qr img {
    display: block;
    width: 100%;
    max-width: 284px;
    margin: 0 auto 12px;
    background: white;
}

.wechat-qr a { color: #70e4ff; }


.examples {

    margin-top: 35px;

    padding: 20px;

    border:
        1px solid #132936;

    border-radius: 14px;

    background:
        rgba(
            8,
            18,
            26,
            0.6
        );

    color: #77909e;

    font-size: 14px;

    line-height: 2;
}


.examples strong {

    color: #a9c9da;
}


.security {

    margin-top: 40px;

    padding-top: 25px;

    border-top:
        1px solid #14232d;
}


input {

    width: 100%;

    padding: 15px;

    border-radius: 10px;

    border:
        1px solid #263b48;

    outline: none;

    background:
        rgba(
            8,
            15,
            22,
            0.8
        );

    color: white;

    font-size: 15px;
}


input:focus {

    border-color:
        #55cfff;
}


.save {

    width: 100%;

    margin-top: 12px;

    padding: 13px;

    border: none;

    border-radius: 10px;

    background: #102433;

    color: #9ddfff;

    font-size: 15px;

    cursor: pointer;
}


.hint {

    margin-top: 25px;

    color: #52636d;

    font-size: 13px;

    line-height: 1.7;
}

</style>

</head>


<body>

<div class="container">


<div class="logo">
JARVIS
</div>


<div class="subtitle">
VOICE CONTROL SYSTEM
</div>


<div class="status">
● PC ONLINE · VOICE READY
</div>


<div class="mic-container">

<button
    id="mic"
    class="mic"
    onclick="toggleRecording()">

🎙️

</button>

</div>


<div
    id="state"
    class="state">

点击麦克风开始说话

</div>


<div
    id="transcript"
    class="transcript">
</div>


<div
    id="reply"
    class="reply">
</div>

<div id="wechatQrBox" class="wechat-qr">
    <img id="wechatQrImage" alt="电脑微信登录二维码">
    <a id="wechatQrSave" download="wechat-login-qr.png">保存二维码（备用）</a>
    <p>在电脑前，可用手机微信直接扫描电脑上的二维码。远程操作时，请在另一台设备上展示本页二维码，再用已登录目标账号的手机微信扫描。同机相册识别若失败，请改用扫码。二维码约 60 秒后失效；过期请重新说指令。</p>
</div>

<div id="codexTaskBox" class="wechat-qr" style="display:none">
    <strong>Codex 项目任务</strong>
    <p>请核对上方识别出的任务，并选择项目。确认后 Codex 才会开始。</p>
    <select id="codexProjectSelect" style="width:100%;padding:12px;margin:8px 0"></select>
    <button class="save" id="codexConfirmButton" onclick="confirmCodexTask()">确认交给 Codex</button>
    <button class="save" onclick="clearCodexDraft()">取消</button>
    <p id="codexTaskStatus"></p>
</div>


<div class="examples">

<strong>现在可以更自然地说</strong>

<br>

“贾维斯，帮我打开微信”

<br>

“帮我在电脑上登录微信”

<br>

“帮我打开微信和记事本”

<br>

“打开微信和文件夹”

<br>

“贾维斯，开始工作”

<br>

“查看电脑状态”

<br>

“锁定电脑”

</div>


<div class="security">

<input
    id="key"
    type="password"
    autocomplete="off"
    placeholder="Jarvis API Key">


<button
    class="save"
    onclick="saveKey()">

保存密钥

</button>


<button
    id="voiceReplyToggle"
    class="save"
    onclick="toggleVoiceReply()">

语音回复：开启

</button>


<div
    id="voiceReplyStatus"
    class="hint">

点击语音回复按钮可测试声音

</div>


<audio
    id="voiceReplyAudio"
    controls
    preload="none"
    style="width: 100%; margin-top: 12px;">
</audio>


<div class="hint">

支持简体 / 繁体中文识别

<br>

点击一次开始录音，
再次点击结束并执行

</div>

</div>

</div>


<script>

let mediaRecorder = null;

let audioChunks = [];

let recording = false;

let activeStream = null;


let apiKey =
    localStorage.getItem(
        "jarvis_key"
    ) || "";


let voiceReplyEnabled =
    localStorage.getItem(
        "jarvis_voice_reply"
    ) !== "off";


let preferredVoice = null;


let activeSpeech = null;


let activeAudioUrl = null;

let activeQrUrl = null;
let qrExpiryTimer = null;
let activeCodexDraftToken = null;
let activeCodexTaskTimer = null;

function clearCodexDraft() {
    activeCodexDraftToken = null;
    document.getElementById("codexTaskBox").style.display = "none";
}

function showCodexDraft(token, projects) {
    activeCodexDraftToken = token;
    const select = document.getElementById("codexProjectSelect");
    select.replaceChildren();
    for (const name of projects) {
        const option = document.createElement("option");
        option.value = name;
        option.textContent = name;
        select.appendChild(option);
    }
    document.getElementById("codexTaskStatus").textContent = "等待你的确认";
    document.getElementById("codexConfirmButton").disabled = false;
    document.getElementById("codexTaskBox").style.display = "block";
}

async function confirmCodexTask() {
    if (!activeCodexDraftToken) return;
    const button = document.getElementById("codexConfirmButton");
    button.disabled = true;
    const status = document.getElementById("codexTaskStatus");
    try {
        const response = await fetch("/codex/confirm", {
            method: "POST",
            headers: {"Content-Type": "application/json", "X-Jarvis-Key": apiKey},
            body: JSON.stringify({
                token: activeCodexDraftToken,
                project: document.getElementById("codexProjectSelect").value
            })
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || "任务提交失败");
        activeCodexDraftToken = null;
        localStorage.setItem("jarvis_codex_task_id", data.task_id);
        status.textContent = "Codex 正在处理项目任务…";
        await pollCodexTask(data.task_id);
    } catch (error) {
        status.textContent = error.message;
        button.disabled = false;
    }
}

async function pollCodexTask(taskId) {
    if (activeCodexTaskTimer) clearTimeout(activeCodexTaskTimer);
    try {
        const response = await fetch("/codex/tasks/" + encodeURIComponent(taskId), {
            headers: {"X-Jarvis-Key": apiKey}, cache: "no-store"
        });
        if (!response.ok) throw new Error("暂时无法读取任务进度");
        const data = await response.json();
        document.getElementById("codexTaskBox").style.display = "block";
        const status = document.getElementById("codexTaskStatus");
        if (data.status === "completed" || data.status === "failed") {
            status.textContent = data.message || "任务已结束";
            localStorage.removeItem("jarvis_codex_task_id");
            if (data.status === "completed") speakReply("Codex 任务已完成。请查看手机上的结果。");
            return;
        }
        status.textContent = "Codex 正在处理 " + (data.project || "项目") + "…";
    } catch (error) {
        document.getElementById("codexTaskStatus").textContent = error.message;
    }
    activeCodexTaskTimer = setTimeout(() => pollCodexTask(taskId), 3000);
}

function clearLoginQr() {
    if (qrExpiryTimer) {
        clearTimeout(qrExpiryTimer);
        qrExpiryTimer = null;
    }
    document.getElementById("wechatQrBox").style.display = "none";
    document.getElementById("wechatQrImage").removeAttribute("src");
    document.getElementById("wechatQrSave").removeAttribute("href");
    if (activeQrUrl) {
        URL.revokeObjectURL(activeQrUrl);
        activeQrUrl = null;
    }
}

async function showLoginQr(token, lifetimeSeconds) {
    const response = await fetch(
        "/wechat/login-qr/" + encodeURIComponent(token),
        {headers: {"X-Jarvis-Key": apiKey}, cache: "no-store"}
    );
    if (!response.ok) {
        throw new Error("登录二维码已过期，请重新说指令");
    }
    activeQrUrl = URL.createObjectURL(await response.blob());
    document.getElementById("wechatQrImage").src = activeQrUrl;
    document.getElementById("wechatQrSave").href = activeQrUrl;
    document.getElementById("wechatQrBox").style.display = "block";
    qrExpiryTimer = setTimeout(
        clearLoginQr,
        Math.min(60, Math.max(1, lifetimeSeconds || 60)) * 1000
    );
}


document
    .getElementById("key")
    .value = apiKey;


updateVoiceReplyButton();


initializeVoiceReply();

const savedCodexTaskId = localStorage.getItem("jarvis_codex_task_id");
if (savedCodexTaskId) pollCodexTask(savedCodexTaskId);


// ============================================================
// Voice Reply
// ============================================================

function updateVoiceReplyButton() {

    const button =
        document.getElementById(
            "voiceReplyToggle"
        );


    button.innerText =
        voiceReplyEnabled
            ? "🔊 语音回复：开启"
            : "🔇 语音回复：关闭";
}


function setVoiceReplyStatus(
    message,
    isError = false
) {

    const status =
        document.getElementById(
            "voiceReplyStatus"
        );


    status.innerText = message;
    status.style.color =
        isError
            ? "#ff718a"
            : "#52636d";
}


function loadPreferredVoice() {

    if (!("speechSynthesis" in window)) {
        return;
    }


    const voices =
        window.speechSynthesis
            .getVoices();


    preferredVoice =
        voices.find(
            voice =>
                voice.lang
                    .toLowerCase()
                    === "zh-cn"
                && !voice.localService
        )
        ||
        voices.find(
            voice =>
                voice.lang
                    .toLowerCase()
                    === "zh-cn"
        )
        ||
        voices.find(
            voice =>
                voice.lang
                    .toLowerCase()
                    .startsWith("zh")
        )
        ||
        voices.find(
            voice => voice.default
        )
        ||
        null;
}


function initializeVoiceReply() {

    if (!("speechSynthesis" in window)) {

        setVoiceReplyStatus(
            "当前浏览器不支持语音合成",
            true
        );

        return;
    }


    loadPreferredVoice();


    window.speechSynthesis
        .addEventListener(
            "voiceschanged",
            loadPreferredVoice
        );
}


function toggleVoiceReply() {

    voiceReplyEnabled =
        !voiceReplyEnabled;


    localStorage.setItem(
        "jarvis_voice_reply",
        voiceReplyEnabled
            ? "on"
            : "off"
    );


    if (!voiceReplyEnabled) {

        if (
            "speechSynthesis"
            in window
        ) {
            activeSpeech = null;

            window.speechSynthesis
                .cancel();
        }


        setVoiceReplyStatus(
            "语音回复已关闭"
        );


        const audio =
            document.getElementById(
                "voiceReplyAudio"
            );


        audio.pause();


        if (activeAudioUrl) {
            URL.revokeObjectURL(
                activeAudioUrl
            );

            activeAudioUrl = null;
        }
    }


    updateVoiceReplyButton();


    if (voiceReplyEnabled) {

        loadPreferredVoice();


        speakReply(
            "语音回复已开启"
        );
    }
}


function speakBrowserReply(message) {

    if (
        !voiceReplyEnabled
        ||
        !message
    ) {
        return false;
    }


    if (!("speechSynthesis" in window)) {

        return false;
    }


    const synthesizer =
        window.speechSynthesis;

    const audio = document.getElementById("voiceReplyAudio");
    audio.pause();
    if (activeAudioUrl) {
        URL.revokeObjectURL(activeAudioUrl);
        activeAudioUrl = null;
    }


    const playSpeech = function() {

        if (!voiceReplyEnabled) {
            return;
        }


        const speech =
            new SpeechSynthesisUtterance(
                message
            );


        activeSpeech = speech;


        speech.lang = "zh-CN";
        speech.rate = 1;
        speech.pitch = 1;
        speech.volume = 1;


        if (preferredVoice) {
            speech.voice = preferredVoice;
        }


        speech.onstart = function() {

            if (activeSpeech !== speech) {
                return;
            }


            setVoiceReplyStatus(
                preferredVoice
                    ? "正在使用中文语音播放"
                    : "正在使用系统默认语音播放"
            );
        };


        speech.onend = function() {

            if (activeSpeech !== speech) {
                return;
            }


            activeSpeech = null;


            setVoiceReplyStatus(
                "语音回复已就绪"
            );
        };


        speech.onerror = function(event) {

            if (activeSpeech !== speech) {
                return;
            }


            activeSpeech = null;


            setVoiceReplyStatus(
                "手机语音不可用，切换离线语音"
            );
            setVoiceReplyStatus("系统语音播放失败，请点击音频播放器重试", true);
        };


        try {
            synthesizer.resume();
            synthesizer.speak(speech);
        } catch {
            activeSpeech = null;
            setVoiceReplyStatus("系统语音播放失败，请点击音频播放器重试", true);
        }
    };


    if (
        synthesizer.speaking
        ||
        synthesizer.pending
    ) {

        activeSpeech = null;
        synthesizer.cancel();


        window.setTimeout(
            playSpeech,
            250
        );

    } else {

        playSpeech();
    }

    return true;
}


function speakReply(message) {
    if (!voiceReplyEnabled || !message) return;
    speakOfflineReply(message);
}


async function speakOfflineReply(message) {

    if (
        !voiceReplyEnabled
        ||
        !message
    ) {
        return;
    }


    if (!apiKey) {

        setVoiceReplyStatus(
            "请先保存 API Key",
            true
        );

        return;
    }


    const audio =
        document.getElementById(
            "voiceReplyAudio"
        );


    audio.pause();


    if (activeAudioUrl) {
        URL.revokeObjectURL(
            activeAudioUrl
        );

        activeAudioUrl = null;
    }


    setVoiceReplyStatus(
        "电脑正在生成离线中文语音"
    );


    try {

        const response = await fetch(
                "/speech/dynamic",
                {
                    method: "POST",
                    headers: {
                        "Content-Type": "application/json",
                        "X-Jarvis-Key": apiKey
                    },
                    body: JSON.stringify({text: message.slice(0, 180)})
                }
            );


        if (!response.ok) {

            let detail =
                "离线语音生成失败";


            try {
                const errorData =
                    await response.json();

                detail =
                    errorData.detail
                    ||
                    detail;
            }
            catch {
            }


            throw new Error(detail);
        }


        const audioBlob =
            await response.blob();


        activeAudioUrl =
            URL.createObjectURL(
                audioBlob
            );


        audio.src = activeAudioUrl;


        audio.onplay = function() {
            setVoiceReplyStatus(
                "正在播放电脑生成的中文语音"
            );
        };


        audio.onended = function() {
            setVoiceReplyStatus(
                "离线语音回复已就绪"
            );
        };


        audio.onerror = function() {
            setVoiceReplyStatus(
                "手机无法播放生成的音频",
                true
            );
        };


        audio.load();


        try {
            await audio.play();
        }
        catch {
            setVoiceReplyStatus(
                "音频已生成，请点击播放器的播放按钮",
                true
            );
        }

    }
    catch (error) {

        setVoiceReplyStatus(
            "离线语音失败，尝试手机系统语音",
            true
        );
        speakBrowserReply(message);
    }
}


// ============================================================
// Save API Key
// ============================================================

function saveKey() {

    apiKey =
        document
            .getElementById("key")
            .value
            .trim();


    if (!apiKey) {

        document
            .getElementById("reply")
            .innerText =
            "请输入 API Key";

        return;
    }


    localStorage.setItem(
        "jarvis_key",
        apiKey
    );


    document
        .getElementById("reply")
        .innerText =
        "✓ 密钥已保存";
}


// ============================================================
// Toggle Recording
// ============================================================

async function toggleRecording() {

    if (!apiKey) {

        document
            .getElementById("reply")
            .innerText =
            "请先输入并保存 Jarvis API Key";

        return;
    }


    if (!recording) {

        await startRecording();

    } else {

        stopRecording();
    }
}


// ============================================================
// Start Recording
// ============================================================

async function startRecording() {

    try {

        activeStream =
            await navigator
                .mediaDevices
                .getUserMedia({
                    audio: true
                });


        audioChunks = [];


        let options = {};


        if (
            MediaRecorder
                .isTypeSupported(
                    "audio/webm;codecs=opus"
                )
        ) {

            options.mimeType =
                "audio/webm;codecs=opus";
        }


        mediaRecorder =
            new MediaRecorder(
                activeStream,
                options
            );


        mediaRecorder
            .ondataavailable =
            function(event) {

                if (
                    event.data &&
                    event.data.size > 0
                ) {

                    audioChunks.push(
                        event.data
                    );
                }
            };


        mediaRecorder.onstop =
            async function() {

                if (activeStream) {

                    activeStream
                        .getTracks()
                        .forEach(
                            track =>
                                track.stop()
                        );
                }


                const mimeType =
                    mediaRecorder.mimeType
                    || "audio/webm";


                const blob =
                    new Blob(
                        audioChunks,
                        {
                            type: mimeType
                        }
                    );


                await uploadAudio(
                    blob
                );
            };


        mediaRecorder.start();


        recording = true;


        document
            .getElementById("mic")
            .classList
            .add("recording");


        document
            .getElementById("state")
            .innerText =
            "正在聆听...";


        document
            .getElementById("transcript")
            .innerText =
            "";


        document
            .getElementById("reply")
            .innerText =
            "";

    }

    catch (error) {

        document
            .getElementById("state")
            .innerText =
            "麦克风不可用";


        document
            .getElementById("reply")
            .innerText =
            "无法使用麦克风：" +
            error.message;
    }
}


// ============================================================
// Stop Recording
// ============================================================

function stopRecording() {

    if (
        mediaRecorder &&
        mediaRecorder.state
            !== "inactive"
    ) {

        mediaRecorder.stop();
    }


    recording = false;


    document
        .getElementById("mic")
        .classList
        .remove("recording");


    document
        .getElementById("state")
        .innerText =
        "Jarvis 正在识别...";
}


// ============================================================
// Upload Audio
// ============================================================

async function uploadAudio(blob) {

    clearLoginQr();

    const formData =
        new FormData();


    formData.append(
        "audio",
        blob,
        "voice.webm"
    );


    try {

        const response =
            await fetch(
                "/voice-command",
                {
                    method: "POST",

                    headers: {
                        "X-Jarvis-Key":
                            apiKey
                    },

                    body:
                        formData
                }
            );


        let data;


        try {

            data =
                await response.json();

        }

        catch {

            throw new Error(
                "服务器返回了无法解析的数据"
            );
        }


        if (
            response.status === 401
        ) {

            document
                .getElementById("state")
                .innerText =
                "认证失败";


            document
                .getElementById("reply")
                .innerText =
                "API Key 不正确";

            return;
        }


        if (!response.ok) {

            document
                .getElementById("state")
                .innerText =
                "处理失败";


            document
                .getElementById("reply")
                .innerText =
                data.detail ||
                "Jarvis 处理录音失败";

            return;
        }


        if (data.transcript) {

            document
                .getElementById(
                    "transcript"
                )
                .innerText =
                "你说：" +
                data.transcript;
        }


        const replyMessage =
            data.message ||
            "任务完成";


        document
            .getElementById("reply")
            .innerText =
            "Jarvis：" +
            replyMessage;


        if (data.qr_token) {
            try {
                await showLoginQr(data.qr_token, data.qr_expires_seconds);
            }
            catch (error) {
                document.getElementById("reply").innerText =
                    "Jarvis：" + error.message;
                document.getElementById("state").innerText = "二维码获取失败";
                return;
            }
        }

        if (data.codex_draft_token) {
            showCodexDraft(data.codex_draft_token, data.codex_projects || []);
        }

        speakReply(
            data.speech_text || replyMessage
        );


        if (data.success) {

            document
                .getElementById("state")
                .innerText =
                "✓ 指令执行完成";

        }

        else {

            document
                .getElementById("state")
                .innerText =
                data.recognized
                    ? "指令已识别，但未完成"
                    : "没有匹配到指令";
        }

    }

    catch (error) {

        document
            .getElementById("state")
            .innerText =
            "连接失败";


        document
            .getElementById("reply")
            .innerText =
            "Jarvis 无法处理录音：" +
            error.message;
    }
}

</script>

<script src="/assets/upgrades.js?v=20261021"></script>

</body>

</html>
"""


# ============================================================
# Startup
# ============================================================

@app.get("/assets/upgrades.js")
def upgrades_script():
    return FileResponse(Path(__file__).resolve().parent / "upgrade_ui.js",
                        media_type="application/javascript", headers={"Cache-Control": "no-store"})


@app.on_event("startup")
async def startup_event():
    print()
    print("=" * 60)
    print("JARVIS PC AGENT")
    print("Status      : ONLINE")
    print("Voice       : READY")
    print("Chinese     : Simplified + Traditional")
    print("Version     : 4.1.0")
    print("=" * 60)
    print()


