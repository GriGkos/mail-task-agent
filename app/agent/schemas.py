from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

TaskStatus = Literal["inbox", "planned", "in_progress", "waiting", "review", "done", "cancelled"]
TaskPriority = Literal["low", "medium", "high", "urgent"]


class ThreadTask(BaseModel):
    id: UUID | str
    title: str
    status: str
    next_action: str | None = None
    gmail_thread_id: str | None = None
    same_thread: bool = False


class EmailForAnalysis(BaseModel):
    gmail_message_id: str
    gmail_thread_id: str
    sender: str
    recipients: list[str] = Field(default_factory=list)
    subject: str
    received_at: datetime | None = None
    body_text: str
    thread_context: list[str] = Field(default_factory=list)
    existing_tasks: list[ThreadTask] = Field(default_factory=list)


class EmailDecision(BaseModel):
    action: Literal["create_task", "update_task", "request_review", "ignore"]
    category: Literal["work", "personal", "notification", "newsletter", "advertising", "unknown"]
    summary: str

    task_title: str | None = None
    project: str | None = None
    status: TaskStatus | None = None
    priority: TaskPriority | None = None
    due_at: datetime | None = None
    assignee: str | None = None
    waiting_for: str | None = None
    next_action: str | None = None

    requires_reply: bool = False
    proposed_gmail_label: str | None = None

    matched_task_id: UUID | None = None
    confidence: float = Field(ge=0, le=1)
    reason: str

    @field_validator("task_title")
    @classmethod
    def blank_title_to_none(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            return None
        return value


class ProcessingResult(BaseModel):
    run_id: str
    status: str
    decision: EmailDecision | None = None
    task_id: str | None = None
    approval_id: str | None = None
    dry_run: bool = False
