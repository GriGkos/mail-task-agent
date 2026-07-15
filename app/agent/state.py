from typing import Any, TypedDict

from app.agent.schemas import EmailDecision, EmailForAnalysis


class AgentState(TypedDict, total=False):
    gmail_message_id: str
    email: EmailForAnalysis
    decision: EmailDecision
    task_id: str | None
    approval_id: str | None
    error: str | None
    metadata: dict[str, Any]
