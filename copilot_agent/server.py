from __future__ import annotations

import json
import logging
import re
import uuid
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from typing import Literal

from fastapi import Body, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from copilot_agent.agent.runner import ChatRunner
from copilot_agent.scenario.bootstrap import apply_scenario_environment
from copilot_agent.scenario.loader import LoadedScenario, load_scenario, scenario_status
from copilot_agent.contracts.validate import ContractValidationError, enrich_event_row
from copilot_agent.credentials import CredentialManager
from copilot_agent.observability import flush_observability, provider_configured
from copilot_agent.rag import RagStoreManager
from copilot_agent.rag.docs_manifest import register_uploaded_file
from copilot_agent.rag.document_lifecycle import (
    build_ingest_result,
    delete_rag_document,
    document_source_hash,
    list_rag_documents,
)
from copilot_agent.rag.ingest import repo_docs_dir
from copilot_agent.runtime.event_schema import (
    EVENT_RAG_DOCUMENT_DELETE_PROOF,
    EVENT_RAG_DOCUMENT_DELETED,
    EVENT_RAG_DOCUMENT_INGESTED,
)
from copilot_agent.runtime.checkpoint_store import CheckpointStore
from copilot_agent.runtime.event_store import (
    THREAD_END_REASON_BROWSER_CLOSE,
    THREAD_END_REASON_EXPLICIT,
    ActiveRunExistsError,
    EventStore,
    IdempotencyConflictError,
    RunConcurrencyLimitError,
    ThreadNotActiveError,
)
from copilot_agent.runtime.execution_engine import ExecutionEngine
from copilot_agent.runtime.side_effects import build_side_effect_read_model
from copilot_agent.runtime.thread_checkpoint import archive_thread_and_purge_checkpoint
from copilot_agent.runtime.thread_lifecycle import ThreadLifecycleCleaner
from copilot_agent.runtime.timeline import TimelineProjector
from copilot_agent.settings import settings
from copilot_agent.tools.extensions.mcp import McpRuntime

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

credential_manager: CredentialManager | None = None
event_store = EventStore(settings.agent_event_store_path)
checkpoint_store = CheckpointStore(settings.agent_checkpoint_path)
timeline_projector = TimelineProjector()
runner: ChatRunner | None = None
execution_engine: ExecutionEngine | None = None
thread_lifecycle_cleaner: ThreadLifecycleCleaner | None = None
rag_manager: RagStoreManager | None = None
rag_watch_task: asyncio.Task | None = None
loaded_scenario: LoadedScenario | None = None
mcp_runtime: McpRuntime | None = None


async def _rag_hot_reload_loop(manager: RagStoreManager) -> None:
    while True:
        await asyncio.sleep(settings.rag_hot_reload_poll_seconds)
        try:
            changed = await asyncio.to_thread(manager.check_and_reload_if_changed)
            if changed:
                st = manager.status()
                log.info(
                    "RAG hot-reload (watch): chunks=%s vector=%s",
                    st.get("chunk_count"),
                    st.get("vector_enabled"),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("RAG hot-reload watch failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global runner, execution_engine, thread_lifecycle_cleaner, rag_manager, rag_watch_task, loaded_scenario, mcp_runtime, credential_manager
    loaded_scenario = load_scenario(
        settings.scenario,
        scenarios_root_path=settings.scenarios_root or None,
    )
    credential_manager = CredentialManager.from_scenario_resources(
        loaded_scenario.resources,
        ttl_seconds=settings.conversation_cookie_ttl_seconds,
    )
    apply_scenario_environment(loaded_scenario)
    if loaded_scenario.budgets.max_context_chars:
        settings.rag_context_budget_chars = loaded_scenario.budgets.max_context_chars
    if loaded_scenario.budgets.max_run_seconds:
        settings.run_timeout_seconds = loaded_scenario.budgets.max_run_seconds
    log.info(
        "Scenario loaded: name=%s config=%s deployment_capabilities=%s docs=%s",
        loaded_scenario.name,
        loaded_scenario.config_path,
        list(settings.enabled_capabilities()),
        loaded_scenario.docs_dir(),
    )
    rag_manager = RagStoreManager(trigger="startup")
    log.info(
        "RAG ready: chunks=%d hybrid_vector=%s hot_reload=%s",
        len(rag_manager.store.chunks),
        rag_manager.store.vector_enabled,
        settings.rag_hot_reload_enabled,
    )
    log.info(
        "Observability provider=%s configured=%s",
        settings.observability_provider,
        provider_configured(),
    )
    mcp_runtime = await McpRuntime.start(loaded_scenario.mcp, scenario_root=loaded_scenario.root)
    if mcp_runtime is not None:
        mcp_tools = [
            tool.name
            for server in mcp_runtime.config.enabled_servers()
            for tool in server.tools
        ]
        log.info("MCP runtime ready: servers=%s tools=%s", list(mcp_runtime.clients.keys()), mcp_tools)
    runner = ChatRunner(
        rag_store=rag_manager.store,
        credential_manager=credential_manager,
        event_store=event_store,
        scenario=loaded_scenario,
        mcp_runtime=mcp_runtime,
    )
    rag_manager.attach_memory(runner.memory)
    execution_engine = ExecutionEngine(event_store=event_store, runner=runner)

    async def _compact_idle_thread(thread_id: str) -> None:
        await runner.compact_checkpoint(thread_id)

    thread_lifecycle_cleaner = ThreadLifecycleCleaner(
        event_store=event_store,
        checkpoint_store=checkpoint_store,
        active_idle_ttl_seconds=settings.thread_active_idle_ttl_seconds,
        ended_archive_ttl_seconds=settings.thread_ended_archive_ttl_seconds,
        interval_seconds=settings.thread_lifecycle_cleaner_interval_seconds,
        compact_idle_thread=_compact_idle_thread,
    )
    thread_lifecycle_cleaner.start()
    if settings.rag_hot_reload_enabled:
        rag_watch_task = asyncio.create_task(_rag_hot_reload_loop(rag_manager))
    yield
    if rag_watch_task is not None:
        rag_watch_task.cancel()
        try:
            await rag_watch_task
        except asyncio.CancelledError:
            pass
    if thread_lifecycle_cleaner is not None:
        await thread_lifecycle_cleaner.stop()
    if runner is not None:
        await runner.aclose()
    flush_observability()
    thread_lifecycle_cleaner = None
    execution_engine = None
    runner = None
    mcp_runtime = None
    rag_manager = None
    rag_watch_task = None
    loaded_scenario = None


app = FastAPI(title="LearnAgent", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

static_dir = Path(__file__).resolve().parent.parent / "static"
if static_dir.is_dir():
    app.mount("/ui", StaticFiles(directory=str(static_dir), html=True), name="ui")


class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str
    reasoning_content: str | None = None


class CreateThreadRequest(BaseModel):
    title: str | None = None


class EndThreadRequest(BaseModel):
    reason: Literal["explicit", "browser_close", "idle"] | None = None


class ChatRequest(BaseModel):
    conversation_id: str | None = None
    thread_id: str | None = None
    messages: list[ChatMessage]
    confirm_dangerous: bool = Field(default=False, description="Auto-approve scenario-declared dangerous tool calls")
    idempotency_key: str | None = None


class CreateRunRequest(BaseModel):
    messages: list[ChatMessage]
    confirm_dangerous: bool = Field(default=False, description="Auto-approve dangerous tool calls for this run")
    idempotency_key: str | None = None


class ContextPreviewRequest(BaseModel):
    messages: list[ChatMessage]
    confirm_dangerous: bool = Field(default=False, description="Preview route decisions with dangerous tool confirmation")


class RejectMemoryItemRequest(BaseModel):
    reason: str = "rejected"


class DeleteMemoryItemRequest(BaseModel):
    reason: str = "user_deleted"


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _require_rag_manager() -> RagStoreManager:
    if rag_manager is None:
        raise HTTPException(status_code=503, detail="RAG not initialized")
    return rag_manager


@app.get("/v1/scenario")
def get_scenario() -> dict[str, object]:
    if loaded_scenario is None:
        raise HTTPException(status_code=503, detail="Scenario not initialized")
    return {"scenario": scenario_status(loaded_scenario)}


@app.get("/v1/rag/status")
def rag_status() -> dict[str, object]:
    manager = _require_rag_manager()
    return {
        "rag": manager.status(),
        "hot_reload_enabled": settings.rag_hot_reload_enabled,
        "hot_reload_poll_seconds": settings.rag_hot_reload_poll_seconds,
    }


@app.post("/v1/rag/reload")
def rag_reload() -> dict[str, object]:
    manager = _require_rag_manager()
    return {"rag": manager.reload(trigger="api")}


@app.get("/v1/rag/documents")
def rag_documents() -> dict[str, object]:
    return {"rag": _require_rag_manager().status(), **list_rag_documents()}


@app.delete("/v1/rag/documents/{doc_id}")
def rag_delete_document(doc_id: str, reason: str = "api_delete") -> dict[str, object]:
    manager = _require_rag_manager()
    try:
        result = delete_rag_document(doc_id, manager=manager, reason=reason, sync_vector=True)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="document not found") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if result.vector_delete_attempted and not result.vector_delete_success:
        _append_rag_audit_event(EVENT_RAG_DOCUMENT_DELETED, result.audit_payload())
        raise HTTPException(status_code=500, detail={"error": "vector delete failed", **result.audit_payload()})
    delete_event = _append_rag_audit_event(EVENT_RAG_DOCUMENT_DELETED, result.audit_payload())
    proof_payload = result.proof_payload(delete_event_id=int(delete_event.get("id") or 0) or None)
    _append_rag_audit_event(EVENT_RAG_DOCUMENT_DELETE_PROOF, proof_payload)
    return {"deleted": result.as_response()}


@app.get("/v1/rag/documents/{doc_id}/deletion-proof")
def rag_document_deletion_proof(doc_id: str) -> dict[str, object]:
    event = event_store.find_latest_event_by_type_and_payload(
        EVENT_RAG_DOCUMENT_DELETE_PROOF,
        payload_key="doc_id",
        payload_value=doc_id,
    )
    if event is None:
        raise HTTPException(status_code=404, detail="deletion proof not found")
    return {"proof": event.get("payload") or {}, "event": event}


_UPLOAD_FILENAME = re.compile(r"^[A-Za-z0-9._-]+\.md$")


@app.post("/v1/rag/upload")
async def rag_upload(
    file: UploadFile = File(...),
    tenant_id: str = Form(""),
    classification: str = Form(""),
    acl: str = Form(""),
    doc_id: str = Form(""),
    pii_level: str = Form("none"),
    retention_policy: str = Form("default"),
) -> dict[str, object]:
    manager = _require_rag_manager()
    base = repo_docs_dir()
    if base is None:
        raise HTTPException(status_code=503, detail="docs dir not configured (set COPILOT_DOCS_PATH)")
    filename = Path(file.filename or "").name
    if not _UPLOAD_FILENAME.match(filename):
        raise HTTPException(status_code=400, detail="only safe *.md filenames are allowed")
    raw = await file.read()
    if len(raw) > 2_000_000:
        raise HTTPException(status_code=413, detail="upload exceeds 2MB limit")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="upload must be UTF-8 markdown") from exc
    source_hash = document_source_hash(text)
    parsed_acl = _parse_upload_acl(acl)
    security: dict[str, object] = {
        "doc_id": _require_rag_meta("doc_id", doc_id),
        "tenant_id": _require_rag_meta("tenant_id", tenant_id),
        "classification": _validate_choice(
            "classification",
            classification,
            {"public", "internal", "confidential", "secret"},
        ),
        "pii_level": _validate_choice("pii_level", pii_level, {"none", "low", "medium", "high"}),
        "retention_policy": _require_rag_meta("retention_policy", retention_policy),
        "source_hash": source_hash,
        "acl": parsed_acl,
    }
    (base / filename).write_text(text, encoding="utf-8")
    register_uploaded_file(base, filename, security=security)
    status = manager.reload(trigger="api")
    result = build_ingest_result(filename=filename, security=security, text=text, rag_status=status, docs_dir=base)
    _append_rag_audit_event(EVENT_RAG_DOCUMENT_INGESTED, result.audit_payload())
    return {"uploaded": filename, "ingested": result.as_response(), "doc_security": security}


def _require_execution_engine() -> ExecutionEngine:
    if execution_engine is None:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    return execution_engine


def _append_rag_audit_event(event_type: str, payload: dict[str, object]) -> dict[str, object]:
    thread_id = "__rag_audit__"
    run_id = "__rag_audit__"
    event_store.ensure_thread(thread_id, title="RAG audit")
    if event_store.get_run(run_id) is None:
        event_store.create_run(thread_id, run_id=run_id)
        event_store.append_event(thread_id, run_id, "run_created", {"status": "audit"})
        event_store.update_run_status(run_id, "running")
        event_store.append_event(thread_id, run_id, "done", {"audit": True})
        event_store.complete_run(run_id)
    return event_store.append_event(thread_id, run_id, event_type, payload)


def _require_rag_meta(name: str, value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail=f"{name} is required")
    return text


def _validate_choice(name: str, value: str, allowed: set[str]) -> str:
    text = _require_rag_meta(name, value).lower()
    if text not in allowed:
        raise HTTPException(status_code=400, detail=f"{name} must be one of {sorted(allowed)}")
    return text


def _parse_upload_acl(raw_acl: str) -> list[str]:
    text = str(raw_acl or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="acl is required")
    try:
        parsed_acl = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="acl must be a JSON array string") from exc
    if not isinstance(parsed_acl, list) or not parsed_acl:
        raise HTTPException(status_code=400, detail="acl must be a non-empty JSON array")
    acl = [str(item).strip() for item in parsed_acl if str(item).strip()]
    if not acl:
        raise HTTPException(status_code=400, detail="acl must include at least one non-empty item")
    return acl


def _reject_inactive_thread(thread_id: str) -> None:
    thread = event_store.get_thread(thread_id)
    if thread is not None and str(thread.get("status", "")) != "active":
        raise HTTPException(status_code=409, detail="thread is not active")


def _maybe_validate_events(rows: list[dict[str, object]], validated: bool) -> list[dict[str, object]]:
    if not validated:
        return rows
    out: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            out.append(enrich_event_row(row))
        except ContractValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    return out


@app.post("/v1/threads")
def create_thread(body: CreateThreadRequest = Body(default_factory=CreateThreadRequest)) -> dict[str, object]:
    thread_id = str(uuid.uuid4())
    return event_store.ensure_thread(thread_id, title=body.title)


@app.get("/v1/threads/{thread_id}")
def get_thread(thread_id: str) -> dict[str, object]:
    thread = event_store.get_thread(thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="thread not found")
    return {"thread": thread, "runs": event_store.list_runs(thread_id)}


@app.post("/v1/threads/{thread_id}/end")
def end_thread(
    thread_id: str,
    body: EndThreadRequest = Body(default_factory=EndThreadRequest),
) -> dict[str, object]:
    reason = THREAD_END_REASON_EXPLICIT
    if body.reason == THREAD_END_REASON_BROWSER_CLOSE:
        reason = THREAD_END_REASON_BROWSER_CLOSE
    elif body.reason == "idle":
        reason = "idle"
    thread = event_store.end_thread(thread_id, reason=reason)
    if thread is None:
        raise HTTPException(status_code=404, detail="thread not found")
    return {"thread": thread}


@app.post("/v1/threads/{thread_id}/archive")
def archive_thread(thread_id: str) -> dict[str, object]:
    if event_store.get_thread(thread_id) is None:
        raise HTTPException(status_code=404, detail="thread not found")
    thread = archive_thread_and_purge_checkpoint(event_store, checkpoint_store, thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="thread not found")
    return {"thread": thread}


@app.get("/v1/threads/{thread_id}/runs")
def get_thread_runs(thread_id: str) -> dict[str, object]:
    if event_store.get_thread(thread_id) is None:
        raise HTTPException(status_code=404, detail="thread not found")
    return {"runs": event_store.list_runs(thread_id)}


@app.post("/v1/threads/{thread_id}/runs")
async def create_run(thread_id: str, req: CreateRunRequest) -> dict[str, object]:
    if not settings.openai_api_key:
        raise HTTPException(status_code=503, detail="OPENAI_API_KEY is not set")
    _reject_inactive_thread(thread_id)
    event_store.touch_thread(thread_id)
    manager = _require_execution_engine()
    msgs = [m.model_dump() for m in req.messages]
    try:
        managed = await manager.create_run(
            thread_id=thread_id,
            messages=msgs,
            confirm_dangerous=req.confirm_dangerous,
            stream=False,
            idempotency_key=req.idempotency_key,
        )
    except ThreadNotActiveError as exc:
        raise HTTPException(status_code=409, detail="thread is not active") from exc
    except ActiveRunExistsError as exc:
        raise HTTPException(status_code=409, detail="thread already has an active run") from exc
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail="idempotency key conflict") from exc
    except RunConcurrencyLimitError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"run": event_store.get_run(managed.run_id)}


@app.get("/v1/threads/{thread_id}/events")
def get_thread_events(
    thread_id: str,
    run_id: str | None = None,
    after_id: int | None = None,
    limit: int | None = None,
    validated: bool = Query(default=False, description="Validate payloads against contract models"),
) -> dict[str, object]:
    if event_store.get_thread(thread_id) is None:
        raise HTTPException(status_code=404, detail="thread not found")
    if after_id is not None or limit is not None:
        page = event_store.list_events_page(thread_id, run_id=run_id, after_id=after_id, limit=limit)
        if validated and isinstance(page.get("events"), list):
            page["events"] = _maybe_validate_events(page["events"], validated=True)
        return page
    events = _maybe_validate_events(event_store.list_events(thread_id, run_id=run_id), validated=validated)
    return {"events": events}


@app.get("/v1/threads/{thread_id}/memory")
def get_thread_memory(thread_id: str, goal: str | None = None) -> dict[str, object]:
    if event_store.get_thread(thread_id) is None:
        raise HTTPException(status_code=404, detail="thread not found")
    if runner is None:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    bundle = runner.memory.get_memory_preview(thread_id, goal=goal)
    return {
        "thread_id": thread_id,
        "enabled": runner.memory.policy.enabled,
        "thread_summary": bundle.thread_summary,
        "recalled_runs": bundle.recalled_runs,
        "recalled_long_term": bundle.recalled_long_term,
        "dropped_conflicts": bundle.dropped_conflicts,
        "dropped_long_term": bundle.dropped_long_term,
        "inject_preview": bundle.inject_preview,
        "budget": bundle.budget_applied,
        "sources": bundle.sources,
        "explainability": {
            "long_term_pending_excluded": True,
            "long_term_items_recalled": len(bundle.recalled_long_term),
            "episodic_runs_recalled": len(bundle.recalled_runs),
            "conflicts_dropped": len(bundle.dropped_conflicts),
            "long_term_dropped": len(bundle.dropped_long_term),
        },
    }


@app.get("/v1/threads/{thread_id}/memory/items")
def list_thread_memory_items(
    thread_id: str,
    status: Literal["active", "pending", "deprecated", "all"] = "active",
    scope: Literal["user", "session"] | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, object]:
    if event_store.get_thread(thread_id) is None:
        raise HTTPException(status_code=404, detail="thread not found")
    if runner is None:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    return {
        "thread_id": thread_id,
        "status": status,
        "scope": scope,
        "items": runner.memory.list_memory_items(thread_id, status=status, scope=scope, limit=limit),
    }


@app.post("/v1/threads/{thread_id}/memory/items/{item_id}/confirm")
def confirm_thread_memory_item(thread_id: str, item_id: str) -> dict[str, object]:
    if event_store.get_thread(thread_id) is None:
        raise HTTPException(status_code=404, detail="thread not found")
    if runner is None:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    item = runner.memory.confirm_memory_item(item_id, thread_id=thread_id)
    if item is None:
        raise HTTPException(status_code=404, detail="memory item not found")
    return {"thread_id": thread_id, "item": item}


@app.post("/v1/threads/{thread_id}/memory/items/{item_id}/reject")
def reject_thread_memory_item(
    thread_id: str,
    item_id: str,
    request: RejectMemoryItemRequest | None = None,
) -> dict[str, object]:
    if event_store.get_thread(thread_id) is None:
        raise HTTPException(status_code=404, detail="thread not found")
    if runner is None:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    item = runner.memory.reject_memory_item(
        item_id,
        thread_id=thread_id,
        reason=(request.reason if request else "rejected"),
    )
    if item is None:
        raise HTTPException(status_code=404, detail="memory item not found")
    return {"thread_id": thread_id, "item": item}


@app.delete("/v1/threads/{thread_id}/memory/items/{item_id}")
def delete_thread_memory_item(
    thread_id: str,
    item_id: str,
    request: DeleteMemoryItemRequest | None = Body(default=None),
) -> dict[str, object]:
    if event_store.get_thread(thread_id) is None:
        raise HTTPException(status_code=404, detail="thread not found")
    if runner is None:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    item = runner.memory.delete_memory_item(
        item_id,
        thread_id=thread_id,
        reason=(request.reason if request else "user_deleted"),
        actor="user",
    )
    if item is None:
        raise HTTPException(status_code=404, detail="memory item not found")
    proof = runner.memory.latest_memory_delete_proof(thread_id, item_id)
    return {"thread_id": thread_id, "item": item, "deletion_proof": proof}


@app.get("/v1/threads/{thread_id}/memory/items/{item_id}/deletion-proof")
def get_thread_memory_item_deletion_proof(thread_id: str, item_id: str) -> dict[str, object]:
    if event_store.get_thread(thread_id) is None:
        raise HTTPException(status_code=404, detail="thread not found")
    if runner is None:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    proof = runner.memory.latest_memory_delete_proof(thread_id, item_id)
    if proof is None:
        raise HTTPException(status_code=404, detail="deletion proof not found")
    return {"thread_id": thread_id, "item_id": item_id, "proof": proof}


@app.post("/v1/threads/{thread_id}/context/preview")
async def preview_thread_context(thread_id: str, request: ContextPreviewRequest) -> dict[str, object]:
    if event_store.get_thread(thread_id) is None:
        raise HTTPException(status_code=404, detail="thread not found")
    if runner is None:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    bundle = await runner.preview_context(
        thread_id=thread_id,
        messages=[message.model_dump() for message in request.messages],
        confirm_dangerous=request.confirm_dangerous,
    )
    return {"thread_id": thread_id, "dry_run": True, "context": bundle}


@app.get("/v1/runs/{run_id}")
def get_run(run_id: str) -> dict[str, object]:
    run = event_store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return {"run": run}


@app.get("/v1/runs/{run_id}/events")
def get_run_events(
    run_id: str,
    after_id: int | None = None,
    limit: int | None = None,
    validated: bool = Query(default=False, description="Validate payloads against contract models"),
) -> dict[str, object]:
    if event_store.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    if after_id is not None or limit is not None:
        page = event_store.list_run_events_page(run_id, after_id=after_id, limit=limit)
        if validated and isinstance(page.get("events"), list):
            page["events"] = _maybe_validate_events(page["events"], validated=True)
        return page
    events = _maybe_validate_events(event_store.list_run_events(run_id), validated=validated)
    return {"events": events}


@app.get("/v1/runs/{run_id}/timeline")
def get_run_timeline(run_id: str) -> dict[str, object]:
    run = event_store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    events = event_store.list_run_events(run_id)
    return {
        "run": run,
        "timeline": timeline_projector.project_run(run, events),
        "events": events,
    }


@app.get("/v1/runs/{run_id}/side-effects")
def get_run_side_effects(run_id: str) -> dict[str, object]:
    run = event_store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    events = event_store.list_run_events(run_id)
    return build_side_effect_read_model(run, events)


@app.websocket("/v1/runs/{run_id}/ws")
async def run_events_ws(websocket: WebSocket, run_id: str):
    run = event_store.get_run(run_id)
    if run is None:
        await websocket.close(code=4404)
        return

    await websocket.accept()
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()

    def on_event(event: dict[str, object]) -> None:
        if str(event.get("run_id", "")) == run_id:
            loop.call_soon_threadsafe(queue.put_nowait, {"type": "event", "event": event})

    unsubscribe = event_store.subscribe(on_event)
    try:
        await websocket.send_json({"type": "run", "run": run})
        await websocket.send_json({"type": "events", "events": event_store.list_run_events(run_id)})
        while True:
            message = await queue.get()
            await websocket.send_json(message)
            latest = event_store.get_run(run_id)
            if latest is not None:
                await websocket.send_json({"type": "run", "run": latest})
    except WebSocketDisconnect:
        pass
    finally:
        unsubscribe()


@app.post("/v1/runs/{run_id}/cancel")
async def cancel_run(run_id: str) -> dict[str, object]:
    manager = _require_execution_engine()
    try:
        run = await manager.cancel(run_id)
    except KeyError:
        run = event_store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
    return {"run": run}


@app.post("/v1/runs/{run_id}/approve")
async def approve_run(run_id: str) -> dict[str, object]:
    manager = _require_execution_engine()
    try:
        run = await manager.approve(run_id)
    except KeyError:
        if event_store.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail="run not found")
        raise HTTPException(status_code=409, detail="run is not waiting for approval")
    return {"run": run}


@app.post("/v1/runs/{run_id}/reject")
async def reject_run(run_id: str) -> dict[str, object]:
    manager = _require_execution_engine()
    try:
        run = await manager.reject(run_id)
    except KeyError:
        if event_store.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail="run not found")
        raise HTTPException(status_code=409, detail="run is not waiting for approval")
    return {"run": run}


@app.post("/v1/chat")
async def chat(req: ChatRequest):
    if not settings.openai_api_key:
        raise HTTPException(status_code=503, detail="OPENAI_API_KEY is not set")
    conv = req.thread_id or req.conversation_id or str(uuid.uuid4())
    _reject_inactive_thread(conv)
    event_store.ensure_thread(conv)
    event_store.touch_thread(conv)
    manager = _require_execution_engine()
    msgs = [m.model_dump() for m in req.messages]

    try:
        managed = await manager.create_run(
            thread_id=conv,
            messages=msgs,
            confirm_dangerous=req.confirm_dangerous,
            stream=True,
            idempotency_key=req.idempotency_key,
        )
    except ThreadNotActiveError:
        return JSONResponse(status_code=409, content={"detail": "thread is not active"})
    except ActiveRunExistsError:
        return JSONResponse(status_code=409, content={"detail": "thread already has an active run"})
    except IdempotencyConflictError:
        return JSONResponse(status_code=409, content={"detail": "idempotency key conflict"})
    except RunConcurrencyLimitError as exc:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    async def gen():
        run_id = managed.run_id
        yield f"event: meta\ndata: {json.dumps({'conversation_id': conv, 'thread_id': conv, 'run_id': run_id}, ensure_ascii=False)}\n\n"
        async for chunk in manager.stream(run_id):
            yield chunk

    return StreamingResponse(gen(), media_type="text/event-stream")
