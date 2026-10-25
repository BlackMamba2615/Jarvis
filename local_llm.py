"""Private Ollama chat backend. Text is sent only to loopback, never a shell."""

from __future__ import annotations

from collections import deque
from contextlib import nullcontext
import json
import threading

import requests

from chat_sessions import check_cancelled
from jarvis_settings import load_local_settings


OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
LOCAL_MODEL = load_local_settings().get("local_model", "qwen3.8:27b")
_MAX_INPUT_CHARS = 2000
_MAX_REPLY_CHARS = 900
_HISTORY: deque[dict[str, str]] = deque(maxlen=8)
_CHAT_LOCK = threading.Lock()

SYSTEM_PROMPT = (
    "你是 Jarvis 的中文语音聊天助手。自然、简洁地回答用户，通常一到两句。"
    "你只能聊天，不能操作电脑、调用技能、修改文件或替用户完成登录。"
    "不要声称已经执行任何动作；操作请求应说明需要使用 Jarvis 的白名单技能。"
    "不要输出思考过程或 Markdown。"
)


class LocalModelUnavailable(RuntimeError):
    def __init__(self, *, reason: str) -> None:
        super().__init__("本地聊天模型暂时不可用")
        # Safe fixed category; never expose HTTP errors or user speech.
        self.reason = reason


def chat_local(
    text: str, *, timeout: int = 180, think: bool = False,
    history: list[dict[str, str]] | None = None, on_delta=None,
    cancel_event=None,
) -> str:
    """Read-only local text, optionally streamed; the router owns shared context."""
    prompt = text.strip()
    if not prompt or len(prompt) > _MAX_INPUT_CHARS:
        raise ValueError("聊天内容不能为空且不得超过 2000 字")

    check_cancelled(cancel_event)
    with _CHAT_LOCK if history is None else nullcontext():
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            *(list(_HISTORY) if history is None else history),
            {"role": "user", "content": prompt},
        ]
        payload = {
            "model": LOCAL_MODEL,
            "messages": messages,
            "think": bool(think),
            "stream": on_delta is not None or cancel_event is not None,
            "keep_alive": "15m",
            "options": {
                "temperature": 0.3,
                "top_p": 0.8,
                "num_ctx": 4096,
                "num_predict": 256,
            },
        }
        response = None
        try:
            check_cancelled(cancel_event)
            response = requests.post(
                OLLAMA_URL, json=payload, timeout=(3, timeout),
                proxies={"http": "", "https": "", "all": ""},
                allow_redirects=False,
                **({"stream": True} if payload["stream"] else {}),
            )
            status = getattr(response, "status_code", None)
            if isinstance(status, int) and 300 <= status < 400:
                raise LocalModelUnavailable(reason="redirect_refused")
            response.raise_for_status()
            if payload["stream"]:
                pieces: list[str] = []
                length = 0
                completed = False
                for raw in response.iter_lines(chunk_size=1):
                    check_cancelled(cancel_event)
                    if not raw:
                        continue
                    data = json.loads(raw)
                    if not isinstance(data, dict) or data.get("error"):
                        raise LocalModelUnavailable(reason="invalid_response")
                    message = data.get("message")
                    delta = message.get("content", "") if isinstance(message, dict) else ""
                    if not isinstance(delta, str):
                        raise LocalModelUnavailable(reason="invalid_response")
                    delta = delta[:_MAX_REPLY_CHARS - length]
                    if delta:
                        pieces.append(delta)
                        length += len(delta)
                        if on_delta is not None:
                            on_delta(delta)
                    if data.get("done") is True:
                        completed = True
                        break
                    if length >= _MAX_REPLY_CHARS:
                        completed = True
                        break
                check_cancelled(cancel_event)
                if not completed:
                    raise LocalModelUnavailable(reason="incomplete_response")
                content = "".join(pieces)
            else:
                data = response.json()
                message = data.get("message") if isinstance(data, dict) else None
                content = message.get("content") if isinstance(message, dict) else None
        except requests.exceptions.Timeout as error:
            raise LocalModelUnavailable(reason="timeout") from error
        except requests.exceptions.RequestException as error:
            raise LocalModelUnavailable(reason="connection") from error
        except ValueError as error:
            raise LocalModelUnavailable(reason="invalid_response") from error
        finally:
            if response is not None:
                response.close()
        if not isinstance(content, str) or not content.strip():
            raise LocalModelUnavailable(reason="empty_response")
        answer = content.strip()[:_MAX_REPLY_CHARS]
        check_cancelled(cancel_event)
        if history is None:
            _HISTORY.append({"role": "user", "content": prompt})
            _HISTORY.append({"role": "assistant", "content": answer})
        return answer


def ask_local(text: str, think: bool = False, timeout: int = 120) -> str:
    """Keep the earlier standalone test helper available."""
    try:
        return chat_local(text, timeout=timeout, think=think)
    except LocalModelUnavailable:
        return "本地模型暂时不可用，请检查 Ollama 是否正在运行。"


if __name__ == "__main__":
    print(ask_local("你好，请用一句话告诉我你是谁。"))
