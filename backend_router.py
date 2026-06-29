"""
Backend Router — provider-agnostic message dispatcher.

backend_mode.json schema (extended):
  {
    "mode": "openai",          # provider id OR legacy "api"/"claude_web"
    "model": "claude-opus-4.6",  # optional model override
    "updated_at": "..."
  }

Backward-compatible: old "api" maps to the first OpenAI-compatible provider,
old "claude_web" maps to ClaudeWebProvider.
"""

from __future__ import annotations

import json
import os
import re
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

JST = timezone(timedelta(hours=9))

_WEIXIN_ROOT = Path(__file__).resolve().parent
_PROVIDERS_FILE = _WEIXIN_ROOT / "providers.json"

CLAUDE_IMPRINT_ROOT = Path(os.environ.get("CASS_ROOT", str(Path.home() / "claude-imprint")))
BACKEND_MODE_FILE = CLAUDE_IMPRINT_ROOT / "data" / "backend_mode.json"
LOG_PATH = CLAUDE_IMPRINT_ROOT / "logs" / "backend_router.log"

CLAUDE_WEB_TOOL_DISPATCHED = "__CLAUDE_WEB_TOOL_DISPATCHED__"

# Legacy mode names → provider id
_LEGACY_MODE_MAP = {
    "api": "openai",
    "claude_web": "claude_web",
}


def now_jst() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def log_line(message: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"[{now_jst()}] {message}\n")


# ─── providers.json ────────────────────────────────────────────────────────────

def _resolve_env(value: str) -> str:
    """Expand ${VAR} and ${VAR:-default} references in a config value."""
    def replace(m: re.Match) -> str:
        inner = m.group(1)
        if ":-" in inner:
            var, default = inner.split(":-", 1)
            return os.environ.get(var.strip(), default.strip())
        return os.environ.get(inner.strip(), "")
    return re.sub(r"\$\{([^}]+)\}", replace, value)


def load_providers_config() -> dict:
    """Load and resolve providers.json. Falls back to env-var config if missing."""
    if _PROVIDERS_FILE.exists():
        try:
            raw = json.loads(_PROVIDERS_FILE.read_text(encoding="utf-8"))
            # Resolve env vars in provider configs
            for p in raw.get("providers", []):
                for k, v in p.items():
                    if isinstance(v, str):
                        p[k] = _resolve_env(v)
            mcp_url = raw.get("mcp_url", "http://localhost:8080/mcp")
            raw["mcp_url"] = _resolve_env(mcp_url)
            return raw
        except Exception as e:
            log_line(f"load_providers_config error: {e}")

    # Fallback to environment variables
    return {
        "providers": [
            {
                "id": "openai",
                "name": "OpenAI-compatible",
                "base_url": os.environ.get("OPENAI_BASE_URL", ""),
                "api_key": os.environ.get("OPENAI_API_KEY", ""),
                "default_model": os.environ.get("OPENAI_MODEL", "gpt-4o"),
            }
        ],
        "mcp_url": "http://localhost:8080/mcp",
    }


def get_provider_configs() -> list[dict]:
    return load_providers_config().get("providers", [])


def get_mcp_url() -> str:
    return load_providers_config().get("mcp_url", "http://localhost:8080/mcp")


# ─── backend_mode.json ─────────────────────────────────────────────────────────

def read_backend_mode() -> dict:
    """
    Returns {"mode": str, "model": str | None}.
    mode is always resolved to a provider id (never a legacy alias).
    """
    try:
        data = json.loads(BACKEND_MODE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    except Exception as e:
        log_line(f"read_backend_mode error: {e}; fallback")
        data = {}

    raw_mode = data.get("mode", "openai")
    # Translate legacy aliases
    mode = _LEGACY_MODE_MAP.get(raw_mode, raw_mode)
    model = data.get("model") or None
    return {"mode": mode, "model": model}


def write_backend_mode(mode: str, model: str | None = None) -> dict[str, Any]:
    """Persist backend selection. mode must be a known provider id or 'claude_web'."""
    known_ids = {p["id"] for p in get_provider_configs()} | {"claude_web"}
    # also accept legacy aliases
    resolved = _LEGACY_MODE_MAP.get(mode, mode)
    if resolved not in known_ids:
        raise ValueError(f"unknown mode/provider: {mode!r}; known={sorted(known_ids)}")

    data: dict[str, Any] = {"mode": resolved, "updated_at": now_jst()}
    if model:
        data["model"] = model

    BACKEND_MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
    BACKEND_MODE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    log_line(f"backend mode changed: mode={resolved} model={model}")
    return data


# ─── provider factory ──────────────────────────────────────────────────────────

def _make_provider(provider_id: str) -> Any:
    """Instantiate a provider by id."""
    if provider_id == "claude_web":
        from providers.claude_web_provider import ClaudeWebProvider
        return ClaudeWebProvider()

    configs = get_provider_configs()
    cfg = next((p for p in configs if p["id"] == provider_id), None)
    if cfg is None:
        raise ValueError(f"provider {provider_id!r} not found in providers.json")

    from providers.openai_provider import OpenAIProvider
    return OpenAIProvider(
        base_url=cfg["base_url"],
        api_key=cfg["api_key"],
        default_model=cfg.get("default_model", "gpt-4o"),
    )


def _make_mcp_client() -> Any:
    """Create an MCPClient. Returns None if the URL is not configured."""
    try:
        from mcp_client import MCPClient
        return MCPClient(url=get_mcp_url())
    except Exception as e:
        log_line(f"MCPClient init failed: {e}")
        return None


# ─── public routing API ────────────────────────────────────────────────────────

def chat(
    backend: str | None = None,
    model: str | None = None,
    messages: list[dict] | None = None,
    **kwargs: Any,
) -> str:
    """
    Unified chat interface.

    Args:
        backend:  provider id (e.g. "openai", "claude_web"). If None, reads backend_mode.json.
        model:    model override. If None, reads backend_mode.json then uses provider default.
        messages: full messages list (system + history + user).
        **kwargs: passed to provider.chat() / agent_loop.

    Returns the assistant reply text.
    """
    if messages is None:
        raise ValueError("chat() requires messages")

    bm = read_backend_mode()
    resolved_backend = backend or bm["mode"]
    resolved_model = model or bm["model"]

    log_line(f"chat backend={resolved_backend} model={resolved_model} msgs={len(messages)}")

    provider = _make_provider(resolved_backend)

    if resolved_backend == "claude_web":
        source = kwargs.pop("source", "wechat")
        return provider.chat(messages, model=resolved_model, source=source, **kwargs)

    # Use agent loop with MCP tools for OpenAI-compatible providers
    kwargs.pop("source", None)  # source is channel metadata, not an LLM param
    mcp_client = _make_mcp_client()
    from agent_loop import run_agent_loop
    return run_agent_loop(
        messages,
        provider,
        mcp_client,
        model=resolved_model,
        log_fn=log_line,
        **kwargs,
    )


def route_message(
    user_text: str,
    *,
    history: Any = None,
    source: str = "wechat",
    **kwargs: Any,
) -> str:
    """
    Legacy entry point used by standalone_wechat_cass.js bridge and wechat_proactive_once.py.

    Builds messages in-place and calls chat().
    Preserves CLAUDE_WEB_TOOL_DISPATCHED sentinel for WeChat Claude Web mode.
    """
    bm = read_backend_mode()
    mode = bm["mode"]
    text_preview = (user_text or "").replace("\n", " ")[:80]
    log_line(f"route_message source={source} mode={mode} text={text_preview!r}")

    try:
        # Build messages list (system prompt + history + user)
        from cass_openai_client import build_cass_system_prompt, _build_user_content
        system_prompt = build_cass_system_prompt()
        messages: list[dict] = [{"role": "system", "content": system_prompt}]
        if history:
            messages.extend(history[-20:])
        messages.append({"role": "user", "content": _build_user_content(user_text, kwargs.get("image_paths"))})

        if mode == "claude_web":
            try:
                from claude_web_backend import call_claude_web
                reply = call_claude_web(user_text, source=source, history=history)
                if isinstance(reply, str) and "not implemented yet" in reply:
                    raise RuntimeError(reply)
                log_line(f"claude_web success source={source}")
                if source == "wechat":
                    return CLAUDE_WEB_TOOL_DISPATCHED
                return reply
            except Exception as e:
                log_line(f"claude_web failed: {e}; fallback to openai")
                log_line(traceback.format_exc().rstrip())
                mode = "openai"

        return chat(backend=mode, model=bm["model"], messages=messages, source=source)

    except Exception as e:
        log_line(f"route_message error: {type(e).__name__}: {e}")
        log_line(traceback.format_exc().rstrip())
        raise
