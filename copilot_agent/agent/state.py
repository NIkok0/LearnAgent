from __future__ import annotations

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from typing_extensions import Annotated, TypedDict


class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
