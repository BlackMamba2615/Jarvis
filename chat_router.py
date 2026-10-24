"""Simple chat uses local Qwen; complex chat uses the independent OpenAI API."""

from __future__ import annotations

import logging
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
import threading

from local_llm import LOCAL_MODEL, LocalModelUnavailable, chat_local
from openai_chat import OpenAIAPIUnavailable, chat_openai, model_name
from chat_sessions import (
    ChatCancelled, SessionTurn, append_turn, check_cancelled, context_messages,
    validate_session_id,
)


_LOGGER = logging.getLogger("jarvis.chat_router")
_ROUTE_LOCK = threading.Lock()
_ROUTES: OrderedDict[str, dict] = OrderedDict()
_WEB_PHRASES = (
    "联网", "上网查", "网上查", "帮我搜索", "帮我搜一下", "搜索一下", "搜索最新",
    "查一下最新", "最新新闻", "最新消息", "今天的新闻", "今日新闻", "实时查询",
)
_CLOUD_PHRASES = (
    "chatgpt", "云端模型", "深入分析", "深度分析", "详细分析",
    "认真推理", "复杂推理", "详细解释", "全面比较", "系统分析",
    "制定方案", "设计方案", "写一份报告", "写一篇文章", "长篇",
    "多步骤", "逐步推导", "证明", "代码审查", "架构设计",
    "联网搜索", "搜索最新", "查一下最新", "最新新闻", "最新消息",
)


def is_complex_query(text: str) -> bool:
    """This only selects a chat backend; it never authorizes an action."""
    prompt = text.strip().casefold()
    return len(prompt) >= 100 or any(phrase in prompt for phrase in _CLOUD_PHRASES)


def wants_web_search(text: str) -> bool:
    return any(phrase in text.strip().casefold() for phrase in _WEB_PHRASES)


def _record_route(session_id: str, backend: str, *, fallback=False, reason="", sources=None) -> None:
    route = {
        "backend": backend,
        "model": LOCAL_MODEL if backend == "local" else model_name() if backend == "openai" else "",
        "fallback": bool(fallback), "reason": reason,
        "sources": [dict(source) for source in (sources or [])],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    with _ROUTE_LOCK:
        _ROUTES[session_id] = route
        _ROUTES.move_to_end(session_id)
        while len(_ROUTES) > 64:
            _ROUTES.popitem(last=False)


def get_last_route(session_id: str = "default") -> dict:
    validate_session_id(session_id)
    with _ROUTE_LOCK:
        result = dict(_ROUTES.get(session_id, {
            "backend": "none", "model": "", "fallback": False,
            "reason": "not_used", "sources": [], "updated_at": None,
        }))
        result["sources"] = [dict(source) for source in result["sources"]]
        return result


@contextmanager
def _route_snapshot(session_id: str, route_out: dict | None):
    """Capture this request while its session is still locked, including errors."""
    try:
        yield
    finally:
        if route_out is not None:
            route_out.clear()
            route_out.update(get_last_route(session_id))


def chat(
    text: str, *, session_id: str = "default", on_delta=None,
    cancel_event=None, route_out: dict | None = None,
) -> str:
    """One bounded read-only turn; model switching retains this session's context."""
    if not isinstance(text, str) or not text.strip() or len(text.strip()) > 2000:
        raise ValueError("聊天内容不能为空且不得超过 2000 字")
    prompt = text.strip()
    validate_session_id(session_id)
    check_cancelled(cancel_event)
    with SessionTurn(session_id, cancel_event), _route_snapshot(session_id, route_out):
        history = context_messages(session_id)
        emitted = False

        def delta(value: str):
            nonlocal emitted
            check_cancelled(cancel_event)
            if value and on_delta is not None:
                emitted = True
                on_delta(value)

        callback = delta if on_delta is not None else None
        search = wants_web_search(prompt)
        preferred = "openai" if search or is_complex_query(prompt) else "local"
        backend, fallback, reason, sources = preferred, False, "", []
        _record_route(session_id, backend, reason="processing")

        def invoke(selected: str) -> str:
            kwargs = {"history": history, "on_delta": callback, "cancel_event": cancel_event}
            if selected == "local":
                return chat_local(prompt, **kwargs)
            return chat_openai(prompt, web_search=search, sources=sources, **kwargs)

        try:
            try:
                answer = invoke(preferred)
            except (LocalModelUnavailable, OpenAIAPIUnavailable) as error:
                check_cancelled(cancel_event)
                reason = error.reason
                if search:
                    # No offline substitution masquerading as a current search.
                    _record_route(session_id, "unavailable", reason=reason)
                    answer = "当前无法完成联网查询，不能核实最新信息。请稍后重试；我不会把离线回答当作实时结果。"
                    if on_delta is not None:
                        delta(answer)
                    return answer
                if emitted:
                    _record_route(session_id, preferred, reason="partial_response_failed")
                    raise
                fallback = True
                backend = "local" if preferred == "openai" else "openai"
                _LOGGER.warning("chat_backend_fallback backend=%s reason=%s", backend, reason)
                _record_route(session_id, backend, fallback=True, reason=reason)
                answer = invoke(backend)
            check_cancelled(cancel_event)
        except ChatCancelled:
            _record_route(session_id, backend, fallback=fallback, reason="cancelled")
            raise
        except (LocalModelUnavailable, OpenAIAPIUnavailable) as error:
            _record_route(session_id, backend, fallback=fallback, reason="partial_response_failed" if emitted else error.reason)
            raise
        append_turn(session_id, prompt, answer)
        _record_route(session_id, backend, fallback=fallback, reason=reason, sources=sources)
        return answer
