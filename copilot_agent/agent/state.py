from __future__ import annotations

from typing import Any

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from typing_extensions import Annotated, NotRequired, TypedDict


class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    tool_route: NotRequired[dict[str, Any]]
