"""Bounded, real dependency checks; no secrets, speech or pixels are returned."""
from __future__ import annotations

import json
from pathlib import Path
import threading
import time

import requests

_CACHE = None
_CHECKED = 0.0
_LOCK = threading.Lock()
_CLOUD_PROBE = None
DESKTOP_HEALTH = Path(__file__).resolve().parent / "runtime" / "desktop_health.json"


def desktop_health() -> dict:
    try:
        if DESKTOP_HEALTH.stat().st_size > 4096:
            raise ValueError("oversized metadata")
        value = json.loads(DESKTOP_HEALTH.read_text(encoding="utf-8"))
        age = time.time() - float(value["timestamp"])
        if not 0 <= age <= 8:
            return {"state": "stale", "microphone": "unknown"}
        mic = value.get("microphone", "unknown")
        if mic not in {"ready", "recovering", "unavailable", "stopped"}:
            mic = "unknown"
        if mic == "ready" and not 0 <= time.time() - float(value.get("last_audio_at", 0)) <= 8:
            mic = "unavailable"
        return {"state": "connected" if mic != "stopped" else "stopped",
                "microphone": mic, "speaking": bool(value.get("speaking", False)),
                "model_ready": bool(value.get("model_ready", False))}
    except (OSError, ValueError, KeyError, TypeError):
        return {"state": "unknown", "microphone": "unknown"}


def collect_health(*, phone_voice_ready: bool = True, refresh: bool = False) -> dict:
    global _CACHE, _CHECKED
    with _LOCK:
        if not refresh and _CACHE is not None and time.monotonic() - _CHECKED < 5:
            return {**_CACHE, "phone_voice": "ready" if phone_voice_ready else "unavailable",
                    "desktop": desktop_health()}
        from local_llm import LOCAL_MODEL
        from openai_chat import is_configured
        from wechat_desktop_bridge import probe_desktop_bridge
        local = {"state": "unavailable", "model": LOCAL_MODEL}
        try:
            with requests.Session() as session:
                session.trust_env = False
                response = session.get("http://127.0.0.1:11434/api/tags", timeout=(0.5, 1), allow_redirects=False)
                response.raise_for_status()
                names = {m.get("name") for m in response.json().get("models", []) if isinstance(m, dict)}
            local["state"] = "ready" if LOCAL_MODEL in names else "model_missing"
        except (requests.RequestException, ValueError, AttributeError):
            pass
        result = {"service": "online", "phone_voice": "ready" if phone_voice_ready else "unavailable",
                  "desktop_bridge": "connected" if probe_desktop_bridge() else "unavailable",
                  "desktop": desktop_health(), "local": local,
                  "cloud": dict(_CLOUD_PROBE) if _CLOUD_PROBE else {"state": "configured_unverified" if is_configured() else "not_configured"},
                  "checked_at": time.time()}
        _CACHE, _CHECKED = result, time.monotonic()
        return result


def check_cloud() -> dict:
    """Explicit metadata probe only. It does not certify generated answers."""
    global _CLOUD_PROBE, _CHECKED
    from openai_chat import probe_cloud
    info = probe_cloud(timeout=5)
    value = {"state": "model_accessible" if info.get("status") == "available" else info.get("status", "unavailable"),
             "reason": info.get("reason", ""), "checked_at": info.get("checked_at")}
    with _LOCK:
        _CLOUD_PROBE = value
        _CHECKED = 0
    return dict(value)
