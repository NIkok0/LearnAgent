from __future__ import annotations

import hashlib
import uuid
from typing import Literal

from pydantic import BaseModel, Field

from copilot_agent.rag.schema import DocChunk

Classification = Literal["public", "internal", "confidential", "secret"]
PiiLevel = Literal["none", "low", "medium", "high"]

CLASSIFICATION_ORDER: dict[str, int] = {
    "public": 0,
    "internal": 1,
    "confidential": 2,
    "secret": 3,
}


class RetrievalRequest(BaseModel):
    tenant_id: str = "default"
    user_id: str = "local_user"
    query: str
    purpose: str = "agent_context"
    max_classification: Classification = "internal"
    allowed_scopes: list[str] = Field(default_factory=list)
    allow_high_pii: bool = False


class RetrievalPolicyDecision(BaseModel):
    allowed: bool
    reason: str = "allowed"
    tenant_id: str = ""
    doc_id: str = ""
    chunk_id: str = ""
    classification: str = ""
    pii_level: str = ""
    acl: list[str] = Field(default_factory=list)


class RetrievalResult(BaseModel):
    request: RetrievalRequest
    allowed_chunks: list[DocChunk] = Field(default_factory=list)
    blocked_count: int = 0
    retrieved_chunk_ids: list[str] = Field(default_factory=list)
    allowed_chunk_ids: list[str] = Field(default_factory=list)
    blocked_chunk_ids: list[str] = Field(default_factory=list)
    prefilter_blocked_chunk_ids: list[str] = Field(default_factory=list)
    prefilter_blocked_count: int = 0
    policy_decisions: list[RetrievalPolicyDecision] = Field(default_factory=list)
    policy_trace_id: str = Field(default_factory=lambda: uuid.uuid4().hex)

    def audit_payload(self) -> dict[str, object]:
        return {
            "tenant_id": self.request.tenant_id,
            "user_id": self.request.user_id,
            "purpose": self.request.purpose,
            "query_hash": query_hash(self.request.query),
            "max_classification": self.request.max_classification,
            "retrieved_chunk_ids": self.retrieved_chunk_ids,
            "allowed_chunk_ids": self.allowed_chunk_ids,
            "blocked_chunk_ids": self.blocked_chunk_ids,
            "blocked_count": self.blocked_count,
            "prefilter_blocked_chunk_ids": self.prefilter_blocked_chunk_ids,
            "prefilter_blocked_count": self.prefilter_blocked_count,
            "policy_trace_id": self.policy_trace_id,
            "policy_decisions": [
                decision.model_dump(exclude_none=True)
                for decision in self.policy_decisions
                if not decision.allowed
            ],
        }


def query_hash(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]


def classification_allowed(value: str, max_classification: str) -> bool:
    return CLASSIFICATION_ORDER.get(value, 99) <= CLASSIFICATION_ORDER.get(max_classification, -1)
