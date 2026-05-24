from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from copilot_agent.agent.graph import _build_checkpointer, close_graph_checkpointer  # noqa: E402
from copilot_agent.agent.stream.event_mapper import GraphEventMapper  # noqa: E402
from copilot_agent.memory import MemoryManager  # noqa: E402
from copilot_agent.rag.retriever import RagStore  # noqa: E402
from copilot_agent.runtime.checkpoint_reader import CheckpointReader  # noqa: E402
from copilot_agent.runtime.event_store import EventStore, RUN_STATUS_RUNNING  # noqa: E402
from copilot_agent.tools.registry import ToolRegistry  # noqa: E402


@dataclass
class VerifyGraphFixture:
    store: EventStore
    memory: MemoryManager
    graph: Any
    mapper: GraphEventMapper
    reader: CheckpointReader
    thread_id: str
    run_id: str


def build_verify_fixture(
    *,
    event_store_path: Path,
    checkpoint_path: Path,
    thread_id: str,
    graph: Any,
    run_status: str | None = RUN_STATUS_RUNNING,
    rag_store: RagStore | None = None,
    tool_registry: ToolRegistry | None = None,
) -> VerifyGraphFixture:
    store = EventStore(str(event_store_path))
    run = store.create_run(thread_id)
    run_id = str(run["id"])
    if run_status:
        store.update_run_status(run_id, run_status)

    compiled = graph.compile(checkpointer=_build_checkpointer(str(checkpoint_path), async_checkpoint=True))
    memory = MemoryManager(
        rag_store=rag_store if rag_store is not None else RagStore([]),
        event_store=store,
        checkpoint_path=str(checkpoint_path),
    )
    reader = CheckpointReader(compiled)
    mapper = GraphEventMapper(
        memory=memory,
        tool_registry=tool_registry if tool_registry is not None else ToolRegistry(),
        checkpoint_reader=reader,
    )
    return VerifyGraphFixture(
        store=store,
        memory=memory,
        graph=compiled,
        mapper=mapper,
        reader=reader,
        thread_id=thread_id,
        run_id=run_id,
    )


async def collect_runtime_events(
    fixture: VerifyGraphFixture,
    *,
    graph_input: Any,
    graph_config: dict[str, Any] | None = None,
    thread_id: str | None = None,
    run_id: str | None = None,
    append_to_store: bool = True,
) -> list[dict[str, Any]]:
    active_thread_id = thread_id or fixture.thread_id
    active_run_id = fixture.run_id if run_id is None else run_id
    config = graph_config or {"configurable": {"thread_id": active_thread_id}}

    events: list[dict[str, Any]] = []
    async for runtime_event in fixture.mapper.map(
        graph=fixture.graph,
        graph_input=graph_input,
        graph_config=config,
        thread_id=active_thread_id,
        run_id=active_run_id,
    ):
        payload = runtime_event.to_store_payload()
        if append_to_store:
            fixture.memory.append_event(active_thread_id, active_run_id, runtime_event.kind, payload)
        events.append(
            {
                "type": runtime_event.kind,
                "trace_id": runtime_event.correlation.trace_id,
                "payload": payload,
            }
        )
    return events


async def close_fixture(fixture: VerifyGraphFixture | None) -> None:
    if fixture is None:
        return
    await close_graph_checkpointer(fixture.graph)
