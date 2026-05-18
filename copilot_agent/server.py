from __future__ import annotations

import json
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from copilot_agent.agent.runner import ChatRunner
from copilot_agent.conversation_store import ConversationCookieStore
from copilot_agent.observability import flush_langfuse
from copilot_agent.rag import build_rag_store
from copilot_agent.settings import settings

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

cookie_store = ConversationCookieStore(settings.conversation_cookie_ttl_seconds)
runner: ChatRunner | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global runner
    rag_store = build_rag_store()
    log.info(
        "RAG ready: chunks=%d hybrid_vector=%s",
        len(rag_store.chunks),
        rag_store.vector_enabled,
    )
    log.info("Langfuse configured=%s", settings.langfuse_configured)
    runner = ChatRunner(rag_store=rag_store, cookie_store=cookie_store)
    yield
    flush_langfuse()
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
    messages: list[ChatMessage]
    confirm_dangerous: bool = Field(default=False, description="Required with COPILOT_ALLOW_JOB_POST for watermark enqueue")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/chat")
async def chat(req: ChatRequest):
    if not settings.openai_api_key:
        raise HTTPException(status_code=503, detail="OPENAI_API_KEY is not set")
    if runner is None:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    conv = req.conversation_id or str(uuid.uuid4())
    msgs = [m.model_dump() for m in req.messages]

    async def gen():
        yield f"event: meta\ndata: {json.dumps({'conversation_id': conv}, ensure_ascii=False)}\n\n"
        try:
            async for chunk in runner.run_stream(
                conversation_id=conv,
                messages=msgs,
                confirm_dangerous=req.confirm_dangerous,
            ):
                yield chunk
        except Exception as e:
            log.exception("chat failed")
            err = json.dumps({"error": str(e)}, ensure_ascii=False)
            yield f"event: error\ndata: {err}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")
