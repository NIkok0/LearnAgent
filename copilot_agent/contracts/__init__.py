"""Cross-boundary data contracts (Pydantic envelopes and adapters)."""

from copilot_agent.contracts.base import CorrelationIds, RuntimeEvent
from copilot_agent.contracts.retrieval import RetrievalRequest, RetrievalResult
from copilot_agent.contracts.tool_data import ToolResultAuditEnvelope
from copilot_agent.contracts.tool_result import ToolResultModel

__all__ = [
    "CorrelationIds",
    "RetrievalRequest",
    "RetrievalResult",
    "RuntimeEvent",
    "ToolResultAuditEnvelope",
    "ToolResultModel",
]
