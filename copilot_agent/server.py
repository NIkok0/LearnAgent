from __future__ import annotations

import json
import logging
import uuid
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from copilot_agent.agent.runner import ChatRunner
from copilot_agent.conversation_store import ConversationCookieStore
from copilot_agent.observability import flush_langfuse
from copilot_agent.rag import build_rag_store
from copilot_agent.runtime.event_store import ActiveRunExistsError, EventStore, ThreadNotActiveError
from copilot_agent.runtime.execution_engine import ExecutionEngine
from copilot_agent.runtime.thread_lifecycle import ThreadLifecycleCleaner
from copilot_agent.runtime.timeline import TimelineProjector
from copilot_agent.settings import settings

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

cookie_store = ConversationCookieStore(settings.conversation_cookie_ttl_seconds)
event_store = EventStore(settings.agent_event_store_path)
timeline_projector = TimelineProjector()
runner: ChatRunner | None = None
execution_engine: ExecutionEngine | None = None
thread_lifecycle_cleaner: ThreadLifecycleCleaner | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global runner, execution_engine, thread_lifecycle_cleaner
    rag_store = build_rag_store()
    log.info(
        "RAG ready: chunks=%d hybrid_vector=%s",
        len(rag_store.chunks),
        rag_store.vector_enabled,
    )
    log.info("Langfuse configured=%s", settings.langfuse_configured)
    runner = ChatRunner(rag_store=rag_store, cookie_store=cookie_store, event_store=event_store)
    execution_engine = ExecutionEngine(event_store=event_store, runner=runner)
    thread_lifecycle_cleaner = ThreadLifecycleCleaner(
        event_store=event_store,
        ended_archive_ttl_seconds=settings.thread_ended_archive_ttl_seconds,
        interval_seconds=settings.thread_lifecycle_cleaner_interval_seconds,
    )
    thread_lifecycle_cleaner.start()
    yield
    if thread_lifecycle_cleaner is not None:
        await thread_lifecycle_cleaner.stop()
    flush_langfuse()
    thread_lifecycle_cleaner = None
    execution_engine = None
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


class CreateRunRequest(BaseModel):
    messages: list[ChatMessage]
    confirm_dangerous: bool = Field(default=False, description="Auto-approve dangerous tool calls for this run")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _require_execution_engine() -> ExecutionEngine:
    if execution_engine is None:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    return execution_engine


def _reject_inactive_thread(thread_id: str) -> None:
    thread = event_store.get_thread(thread_id)
    if thread is not None and str(thread.get("status", "")) != "active":
        raise HTTPException(status_code=409, detail="thread is not active")


@app.post("/v1/threads")
async def create_thread(request: Request) -> dict[str, object]:
    title = None
    try:
        body = await request.json()
    except Exception:
        body = {}
    if isinstance(body, dict):
        raw_title = body.get("title")
        title = str(raw_title) if raw_title is not None else None
    thread_id = str(uuid.uuid4())
    return event_store.ensure_thread(thread_id, title=title)


@app.get("/v1/threads/{thread_id}")
def get_thread(thread_id: str) -> dict[str, object]:
    thread = event_store.get_thread(thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="thread not found")
    return {"thread": thread, "runs": event_store.list_runs(thread_id)}


@app.post("/v1/threads/{thread_id}/end")
def end_thread(thread_id: str) -> dict[str, object]:
    thread = event_store.end_thread(thread_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="thread not found")
    return {"thread": thread}


@app.post("/v1/threads/{thread_id}/archive")
def archive_thread(thread_id: str) -> dict[str, object]:
    thread = event_store.archive_thread(thread_id)
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
    manager = _require_execution_engine()
    msgs = [m.model_dump() for m in req.messages]
    try:
        managed = await manager.create_run(
            thread_id=thread_id,
            messages=msgs,
            confirm_dangerous=req.confirm_dangerous,
            stream=False,
        )
    except ThreadNotActiveError as exc:
        raise HTTPException(status_code=409, detail="thread is not active") from exc
    except ActiveRunExistsError as exc:
        raise HTTPException(status_code=409, detail="thread already has an active run") from exc
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
    manager = _require_execution_engine()
    msgs = [m.model_dump() for m in req.messages]

    try:
        managed = await manager.create_run(
            thread_id=conv,
            messages=msgs,
            confirm_dangerous=req.confirm_dangerous,
            stream=True,
        )
    except ThreadNotActiveError:
        return JSONResponse(status_code=409, content={"detail": "thread is not active"})
    except ActiveRunExistsError:
        return JSONResponse(status_code=409, content={"detail": "thread already has an active run"})

    async def gen():
        run_id = managed.run_id
        yield f"event: meta\ndata: {json.dumps({'conversation_id': conv, 'thread_id': conv, 'run_id': run_id}, ensure_ascii=False)}\n\n"
        async for chunk in manager.stream(run_id):
            yield chunk

    return StreamingResponse(gen(), media_type="text/event-stream")
