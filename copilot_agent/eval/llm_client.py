from __future__ import annotations

import os

from copilot_agent.settings import settings


def ensure_eval_api_env() -> bool:
    """Sync .env LLM credentials into os.environ for RAGAS key checks."""
    key = (settings.openai_api_key or os.environ.get("OPENAI_API_KEY", "")).strip()
    if not key:
        return False
    os.environ.setdefault("OPENAI_API_KEY", key)
    if settings.openai_base_url:
        os.environ.setdefault("OPENAI_BASE_URL", settings.openai_base_url)
    return True


def get_eval_chat_model():
    """Chat model for offline RAG E2E / RAGAS eval (uses app LLMProvider config)."""
    ensure_eval_api_env()
    override = os.environ.get("RAG_E2E_MODEL", "").strip()
    if override:
        settings.openai_model = override
    elif os.environ.get("OPENAI_MODEL", "").strip():
        # Prefer settings.openai_model (.env) over a stale shell OPENAI_MODEL.
        os.environ.pop("OPENAI_MODEL", None)

    from copilot_agent.llm.provider import LLMProvider

    return LLMProvider().get_chat_model()
