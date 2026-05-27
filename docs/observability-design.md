# LearnAgent Observability Design

> This document defines the two-track observability model: local product facts are stored in EventStore and projected by Timeline; external model traces are optional providers such as Langfuse or LangSmith.

Related docs: [agent-learning-guide.md](./agent-learning-guide.md), [runtime-design.md](./runtime-design.md), [data-flow-design.md](./data-flow-design.md), [tech-selection-design.md](./tech-selection-design.md).

## 0. Current Status

| Item | Status | Verification |
|---|---|---|
| EventStore product timeline | Done | `verify_runtime_domain.py --case event_store`, `verify_runtime_domain.py --case timeline` |
| `trace_id` in `RuntimeEvent.correlation` | Done | `verify_observability_domain.py --case correlation` |
| token usage in `run_completed_meta` | Done | `verify_observability_domain.py --case correlation` |
| Observability provider facade | Done | `verify_observability_domain.py --case provider` |
| `llm_generation` event and cost summary | Done | `verify_observability_domain.py --case cost` |
| Langfuse provider | Optional | disabled unless configured |
| LangSmith provider | Optional | disabled unless configured |

## 1. Design Principle

LearnAgent keeps two observability tracks:

1. Product track: SQLite EventStore + TimelineProjector. This is the authoritative runtime ledger for Run state, approval, cancel, tool audit, memory summaries, output guard, and checkpoint consistency.
2. Model track: optional external provider. Langfuse or LangSmith can record LLM generations, graph/tool spans, and later evaluation experiments.

External providers must not replace EventStore. If Langfuse or LangSmith is missing, misconfigured, or unreachable, a Run must still complete and remain debuggable locally.

## 2. Provider Modes

`OBSERVABILITY_PROVIDER` selects the model-track backend:

| Value | Behavior |
|---|---|
| `none` | default local MVP mode; uses local `trace_id`, no external service |
| `langfuse` | uses Langfuse trace/tool/generation spans when keys are configured |
| `langsmith` | uses LangSmith RunTree spans when `LANGSMITH_TRACING=true` and `LANGSMITH_API_KEY` are set |

LangSmith is a strong fit for LangGraph/LangChain trace and eval workflows, but it remains an optional model-track provider.

## 3. EventStore Contract

EventStore continues to store:

- `tool_start` / `tool_end`
- `retrieval_completed`
- `output_guard_checked`
- `run_completed_meta`
- `memory_run_summary` / `memory_thread_summary`
- `llm_generation`

`llm_generation` records provider, model, round index, latency, token usage, estimated cost, finish reason, tool call count, `trace_id`, provider name, and optional external trace URL.

`run_completed_meta` aggregates best-effort observability fields:

- `llm_rounds`
- `prompt_tokens`
- `completion_tokens`
- `total_tokens`
- `estimated_cost`
- `tool_count`
- `failed_tool_count`
- `retrieval_count`
- `trace_id`
- `observability_provider`
- `external_trace_url`

Cost is estimated from a static price table. It is useful for local debugging, not a billing source of truth.

## 4. Timeline Projection

`TimelineProjector` projects `llm_generation` as `kind: "observability"` and exposes:

- `timeline.observability`: LLM rounds, token totals, tool/retrieval counts, output guard action, trace metadata.
- `timeline.cost`: estimated USD cost when the model is in the static price table.
- `timeline.debugger.observability` and `timeline.debugger.cost`: compact debug summary.

Timeline remains a read model. It does not write events or mutate Run state.

## 5. Provider Boundary

External providers may receive sanitized inputs and outputs:

- run trace: thread id, run id, model, message count, last user preview.
- generation span: model, round index, output preview, finish reason, tool names.
- tool span: sanitized arguments and sanitized results.

External providers must not control:

- Run FSM transitions
- approval / reject / cancel semantics
- tool idempotency
- memory summary writes
- checkpoint consistency
- REST/SSE behavior

## 6. LangSmith Notes

LangSmith can replace Langfuse for the model-track layer because LearnAgent is LangGraph-centered. It is especially useful for graph traces and later eval datasets/experiments.

Recommended use:

```text
OBSERVABILITY_PROVIDER=langsmith
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=...
LANGSMITH_PROJECT=LearnAgent
```

The local EventStore and Timeline should still be checked first during debugging. LangSmith is the external trace view, not the product fact source.

## 7. Verification

Core local checks:

```powershell
E:\Conda\envs\learnagent312\python.exe scripts\verify_observability_domain.py --case all
E:\Conda\envs\learnagent312\python.exe scripts\verify_eval_suite.py --profile core-fast
```

Manual optional provider check:

1. Set `OBSERVABILITY_PROVIDER=langsmith`.
2. Set `LANGSMITH_TRACING=true`, `LANGSMITH_API_KEY`, and `LANGSMITH_PROJECT`.
3. Run one `hello agent` turn.
4. Confirm local Timeline has complete events and LangSmith shows the model/tool trace.

## 8. Non-goals

- Do not introduce Prometheus, OpenTelemetry, LiteLLM, Temporal, or Celery in this version.
- Do not make LangSmith or Langfuse a CI hard dependency.
- Do not treat estimated cost as billing truth.
- Do not remove EventStore, Timeline, or deterministic eval scripts.
