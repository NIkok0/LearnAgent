from __future__ import annotations

import json
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from copilot_agent.agent.runner import ChatRunner
from copilot_agent.conversation_store import ConversationCookieStore
from copilot_agent.observability import flush_langfuse
from copilot_agent.rag import build_rag_store
from copilot_agent.runtime.event_store import EventStore
from copilot_agent.runtime.run_manager import RunManager
from copilot_agent.settings import settings

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

cookie_store = ConversationCookieStore(settings.conversation_cookie_ttl_seconds)
event_store = EventStore(settings.agent_event_store_path)
runner: ChatRunner | None = None
run_manager: RunManager | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global runner, run_manager
    rag_store = build_rag_store()
    log.info(
        "RAG ready: chunks=%d hybrid_vector=%s",
        len(rag_store.chunks),
        rag_store.vector_enabled,
    )
    log.info("Langfuse configured=%s", settings.langfuse_configured)
    runner = ChatRunner(rag_store=rag_store, cookie_store=cookie_store, event_store=event_store)
    run_manager = RunManager(event_store=event_store, runner=runner)
    yield
    flush_langfuse()
    run_manager = None
    runner = None


app = FastAPI(title="Watermark Copilot Agent", lifespan=lifespan)
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
    role: str
    content: str


class ChatRequest(BaseModel):
    conversation_id: str | None = None
    thread_id: str | None = None
    messages: list[ChatMessage]
    confirm_dangerous: bool = Field(default=False, description="Required with COPILOT_ALLOW_JOB_POST for watermark enqueue")


class CreateThreadRequest(BaseModel):
    title: str | None = None


class CreateRunRequest(BaseModel):
    messages: list[ChatMessage]
    confirm_dangerous: bool = Field(default=False, description="Auto-approve dangerous tool calls for this run")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _require_run_manager() -> RunManager:
    if run_manager is None:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    return run_manager


@app.post("/v1/threads")
def create_thread(req: CreateThreadRequest | None = Body(default=None)) -> dict[str, object]:
    thread_id = str(uuid.uuid4())
    return event_store.ensure_thread(thread_id, title=req.title if req else None)


@app.get("/v1/threads/{thread_id}")
def get_thread(thread_id: str) -> dict[str, object]:
    thread = event_store.get_thread(thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="thread not found")
    return {"thread": thread, "runs": event_store.list_runs(thread_id)}


@app.get("/v1/threads/{thread_id}/runs")
def get_thread_runs(thread_id: str) -> dict[str, object]:
    if event_store.get_thread(thread_id) is None:
        raise HTTPException(status_code=404, detail="thread not found")
    return {"runs": event_store.list_runs(thread_id)}


@app.post("/v1/threads/{thread_id}/runs")
async def create_run(thread_id: str, req: CreateRunRequest) -> dict[str, object]:
    if not settings.openai_api_key:
        raise HTTPException(status_code=503, detail="OPENAI_API_KEY is not set")
    manager = _require_run_manager()
    msgs = [m.model_dump() for m in req.messages]
    managed = await manager.create_run(
        thread_id=thread_id,
        messages=msgs,
        confirm_dangerous=req.confirm_dangerous,
        stream=False,
    )
    return {"run": event_store.get_run(managed.run_id)}


@app.get("/v1/threads/{thread_id}/events")
def get_thread_events(thread_id: str, run_id: str | None = None) -> dict[str, object]:
    if event_store.get_thread(thread_id) is None:
        raise HTTPException(status_code=404, detail="thread not found")
    return {"events": event_store.list_events(thread_id, run_id=run_id)}


@app.get("/v1/runs/{run_id}")
def get_run(run_id: str) -> dict[str, object]:
    run = event_store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return {"run": run}


@app.get("/v1/runs/{run_id}/events")
def get_run_events(run_id: str) -> dict[str, object]:
    if event_store.get_run(run_id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    return {"events": event_store.list_run_events(run_id)}


@app.post("/v1/runs/{run_id}/cancel")
async def cancel_run(run_id: str) -> dict[str, object]:
    manager = _require_run_manager()
    try:
        run = await manager.cancel(run_id)
    except KeyError:
        run = event_store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
    return {"run": run}


@app.post("/v1/runs/{run_id}/approve")
async def approve_run(run_id: str) -> dict[str, object]:
    manager = _require_run_manager()
    try:
        run = await manager.approve(run_id)
    except KeyError:
        if event_store.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail="run not found")
        raise HTTPException(status_code=409, detail="run is not waiting for approval")
    return {"run": run}


@app.post("/v1/runs/{run_id}/reject")
async def reject_run(run_id: str) -> dict[str, object]:
    manager = _require_run_manager()
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
    manager = _require_run_manager()
    conv = req.thread_id or req.conversation_id or str(uuid.uuid4())
    msgs = [m.model_dump() for m in req.messages]

    async def gen():
        managed = await manager.create_run(
            thread_id=conv,
            messages=msgs,
            confirm_dangerous=req.confirm_dangerous,
            stream=True,
        )
        run_id = managed.run_id
        yield f"event: meta\ndata: {json.dumps({'conversation_id': conv, 'thread_id': conv, 'run_id': run_id}, ensure_ascii=False)}\n\n"
        async for chunk in manager.stream(run_id):
            yield chunk

    return StreamingResponse(gen(), media_type="text/event-stream")
