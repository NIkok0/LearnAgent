"""Cross-boundary data contracts (Pydantic envelopes and adapters)."""

from copilot_agent.contracts.base import CorrelationIds, RuntimeEvent
from copilot_agent.contracts.final_answer import FinalAnswerModel
from copilot_agent.contracts.plan import PlanModel, PlanStepModel
from copilot_agent.contracts.retrieval import RetrievalRequest, RetrievalResult
from copilot_agent.contracts.tool_data import ToolResultAuditEnvelope
from copilot_agent.contracts.tool_result import ToolResultModel

__all__ = [
    "CorrelationIds",
    "FinalAnswerModel",
    "PlanModel",
    "PlanStepModel",
    "RetrievalRequest",
    "RetrievalResult",
    "RuntimeEvent",
    "ToolResultAuditEnvelope",
    "ToolResultModel",
]
