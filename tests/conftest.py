from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.agent.schemas import EmailDecision
from app.config import Settings
from app.db.base import Base
from app.integrations.gmail import FetchedEmail


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        token_encryption_key=Fernet.generate_key().decode("utf-8"),
        dry_run=False,
        safe_mode=False,
        admin_api_key="test",
        deepseek_api_key="test",
        auto_action_confidence=0.9,
        review_confidence=0.7,
        store_email_body=False,
    )


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        yield db
    await engine.dispose()


def decision(**kwargs) -> EmailDecision:
    data = {
        "action": "create_task",
        "category": "work",
        "summary": "Prepare the quarterly report.",
        "task_title": "Prepare quarterly report",
        "project": "Finance",
        "status": "inbox",
        "priority": "medium",
        "requires_reply": True,
        "confidence": 0.95,
        "reason": "The sender explicitly asks for a report.",
    }
    data.update(kwargs)
    return EmailDecision.model_validate(data)


def fetched_email(**kwargs) -> FetchedEmail:
    data = {
        "gmail_message_id": "msg-1",
        "gmail_thread_id": "thread-1",
        "sender": "manager@example.com",
        "recipients": ["me@example.com"],
        "subject": "Quarterly report",
        "received_at": datetime(2026, 6, 29, 8, 0, tzinfo=UTC),
        "body_text": "Please prepare the quarterly report by Friday.",
        "thread_context": [],
        "permalink": "https://mail.google.com/mail/u/0/#inbox/msg-1",
    }
    data.update(kwargs)
    return FetchedEmail(**data)


class FakeAnalyzer:
    def __init__(self, *decisions: EmailDecision) -> None:
        self.decisions = list(decisions)
        self.requests = []

    async def analyze(self, email):
        self.requests.append(email)
        return self.decisions.pop(0)


class FakeGmail:
    def __init__(self, email: FetchedEmail) -> None:
        self.email = email
        self.labels: list[tuple[str, list[str]]] = []
        self.ensure_called = False

    async def list_message_ids(self, query: str, limit: int = 10) -> list[str]:
        return [self.email.gmail_message_id]

    async def fetch_email(self, gmail_message_id: str) -> FetchedEmail:
        assert gmail_message_id == self.email.gmail_message_id
        return self.email

    async def ensure_ai_labels(self) -> None:
        self.ensure_called = True

    async def apply_labels(self, gmail_message_id: str, labels) -> None:
        self.labels.append((gmail_message_id, list(labels)))


class FakeTelegram:
    def __init__(self) -> None:
        self.approvals: list[str] = []
        self.messages: list[str] = []

    async def send_approval(self, approval_id: str, text: str, email_url: str | None = None) -> str:
        self.approvals.append(text)
        return f"tg-{approval_id}"

    async def send_message(self, text: str) -> str:
        self.messages.append(text)
        return "tg-digest"
