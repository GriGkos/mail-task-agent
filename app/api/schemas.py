from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.agent.schemas import ProcessingResult


class TaskOut(BaseModel):
    id: str
    title: str
    description: str
    project: str | None
    status: str
    priority: str
    due_at: datetime | None
    assignee: str | None
    waiting_for: str | None
    next_action: str | None
    gmail_thread_id: str | None
    source_message_id: str | None
    source_permalink: str | None
    requires_reply: bool
    last_activity_at: datetime | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None

    model_config = {"from_attributes": True}


class AgentRunOut(BaseModel):
    id: str
    gmail_message_id: str | None
    langgraph_thread_id: str | None
    status: str
    decision: dict[str, Any] | None
    error: str | None
    duration_ms: int | None
    attempts: int
    started_at: datetime
    finished_at: datetime | None

    model_config = {"from_attributes": True}


class ApprovalOut(BaseModel):
    id: str
    action_type: str
    payload: dict[str, Any]
    status: str
    telegram_message_id: str | None
    langgraph_thread_id: str | None
    created_at: datetime
    resolved_at: datetime | None

    model_config = {"from_attributes": True}


class EditApprovalIn(BaseModel):
    patch: dict[str, Any]


class ProcessEmailOut(ProcessingResult):
    pass


class TelegramLinkIn(BaseModel):
    telegram_user_id: str
    chat_id: str
    username: str | None = None


class OnboardingLinkOut(BaseModel):
    link_token: str
    onboarding_url: str
