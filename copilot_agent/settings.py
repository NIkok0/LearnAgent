from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    watermark_api_base_url: str = "http://127.0.0.1:8080"
    openai_api_key: str = ""
    openai_base_url: Optional[str] = None
    openai_model: str = "gpt-4o-mini"
    openai_provider: str = "openai-compatible"
    openai_reasoning_effort: Optional[str] = None
    openai_thinking_type: Optional[str] = None
    openai_extra_body_json: Optional[str] = None
    openai_disable_thinking_for_tools: bool = False
    openai_proxy_url: Optional[str] = None
    openai_auto_proxy: bool = True
    openai_proxy_probe_hosts: str = "127.0.0.1:7890,127.0.0.1:7897,127.0.0.1:1080"

    copilot_host: str = "0.0.0.0"
    copilot_port: int = 8090

    copilot_allow_job_post: bool = False
    """When false, http_post to /api/v1/jobs/watermark is rejected regardless of user text."""

    conversation_cookie_ttl_seconds: int = 86400

    langfuse_enabled: bool = True
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    rag_use_vector: bool = False
    rag_rebuild_index: bool = False
    rag_embedding_model: str = "BAAI/bge-small-en-v1.5"
    rag_chroma_path: str = ""
    rag_keyword_weight: float = 0.5
    rag_vector_weight: float = 0.5
    rag_vector_top_k: int = 12

    # Phase 3: persisted LangGraph checkpoint.
    agent_checkpoint_path: str = "storage/langgraph-checkpoints.sqlite"

    # Runtime event store for threads, runs, and replayable SSE events.
    agent_event_store_path: str = "storage/learnagent-events.sqlite"

    # Thread lifecycle cleanup.
    thread_active_idle_ttl_seconds: int = 180
    thread_ended_archive_ttl_seconds: int = 3600
    thread_lifecycle_cleaner_interval_seconds: int = 60

    # Run execution limits.
    run_timeout_seconds: int = 120
    max_concurrent_runs: int = 4
    max_llm_inflight: int = 4

    # EventStore pagination defaults.
    event_page_default_limit: int = 100
    event_page_max_limit: int = 500
    event_page_legacy_max: int = 1000

    # Memory orchestration policy (episodic summary inject).
    memory_enabled: bool = True
    memory_thread_summary_max_runs: int = 5
    memory_thread_summary_max_chars: int = 1200
    memory_episodic_recall_top_k: int = 2
    memory_include_failed_runs: bool = False
    memory_include_cancelled_runs: bool = False
    memory_key_output_max_chars: int = 800

    # HuggingFace model cache root (hub/ lives under this directory).
    hf_home: str = r"F:\model"

    @property
    def langfuse_configured(self) -> bool:
        return bool(
            self.langfuse_enabled
            and self.langfuse_public_key.strip()
            and self.langfuse_secret_key.strip()
        )


def apply_hf_home(hf_home: str) -> str:
    """Point HuggingFace / sentence-transformers caches at ``hf_home`` (e.g. F:\\model)."""
    root = Path(hf_home.strip() or r"F:\model").resolve()
    root.mkdir(parents=True, exist_ok=True)
    hub = root / "hub"
    hub.mkdir(parents=True, exist_ok=True)
    os.environ["HF_HOME"] = str(root)
    os.environ["HF_HUB_CACHE"] = str(hub)
    os.environ["TRANSFORMERS_CACHE"] = str(hub)
    os.environ["SENTENCE_TRANSFORMERS_HOME"] = str(root)
    return str(root)


settings = Settings()
apply_hf_home(settings.hf_home)
