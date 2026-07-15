from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.schemas import EmailDecision
from app.db.models import (
    AgentRun,
    ApprovalRequest,
    EmailMessage,
    EmailThread,
    MailAccount,
    MailSetupSession,
    OAuthState,
    Task,
    TaskEvent,
    TelegramIdentity,
    User,
    UserSettings,
)


class EmailRepository:
    def __init__(self, session: AsyncSession, user_id: str | None = None) -> None:
        self.session = session
        self.user_id = user_id

    async def get_message(self, gmail_message_id: str) -> EmailMessage | None:
        stmt = select(EmailMessage).where(EmailMessage.gmail_message_id == gmail_message_id)
        if self.user_id is not None:
            stmt = stmt.where(EmailMessage.user_id == self.user_id)
        return await self.session.scalar(stmt)

    async def upsert_thread(
        self,
        gmail_thread_id: str,
        subject: str,
        participants: list[str],
        last_message_at: datetime | None,
        summary: str | None = None,
    ) -> EmailThread:
        stmt = select(EmailThread).where(EmailThread.gmail_thread_id == gmail_thread_id)
        if self.user_id is not None:
            stmt = stmt.where(EmailThread.user_id == self.user_id)
        thread = await self.session.scalar(stmt)
        if thread is None:
            thread = EmailThread(
                user_id=self.user_id,
                gmail_thread_id=gmail_thread_id,
                subject=subject,
                participants=participants,
                last_message_at=last_message_at,
                summary=summary,
            )
            self.session.add(thread)
        else:
            thread.subject = subject or thread.subject
            thread.participants = sorted(set([*thread.participants, *participants]))
            thread.last_message_at = last_message_at or thread.last_message_at
            if summary:
                thread.summary = summary
        return thread

    async def create_message(
        self,
        gmail_message_id: str,
        gmail_thread_id: str,
        sender: str,
        recipients: list[str],
        subject: str,
        received_at: datetime | None,
        body_hash: str,
    ) -> EmailMessage:
        message = EmailMessage(
            user_id=self.user_id,
            gmail_message_id=gmail_message_id,
            gmail_thread_id=gmail_thread_id,
            sender=sender,
            recipients=recipients,
            subject=subject,
            received_at=received_at,
            body_hash=body_hash,
            processing_status="new",
        )
        self.session.add(message)
        return message

    async def mark_message(self, message: EmailMessage, status: str) -> None:
        message.processing_status = status


class TaskRepository:
    def __init__(self, session: AsyncSession, user_id: str | None = None) -> None:
        self.session = session
        self.user_id = user_id

    async def get(self, task_id: str) -> Task | None:
        task = await self.session.get(Task, task_id)
        if task and self.user_id is not None and task.user_id != self.user_id:
            return None
        return task

    async def list(self, limit: int = 100) -> list[Task]:
        stmt = select(Task).order_by(Task.updated_at.desc()).limit(limit)
        if self.user_id is not None:
            stmt = stmt.where(Task.user_id == self.user_id)
        result = await self.session.scalars(stmt)
        return list(result)

    async def by_thread(self, gmail_thread_id: str) -> Task | None:
        stmt = (
            select(Task)
            .where(Task.gmail_thread_id == gmail_thread_id)
            .where(Task.status.not_in(["done", "cancelled"]))
            .order_by(Task.updated_at.desc())
            .limit(1)
        )
        if self.user_id is not None:
            stmt = stmt.where(Task.user_id == self.user_id)
        return await self.session.scalar(stmt)

    async def list_open(self, limit: int = 20) -> list[Task]:
        stmt = (
            select(Task)
            .where(Task.status.not_in(["done", "cancelled"]))
            .order_by(Task.updated_at.desc())
            .limit(limit)
        )
        if self.user_id is not None:
            stmt = stmt.where(Task.user_id == self.user_id)
        return list(await self.session.scalars(stmt))

    async def list_completed(self, limit: int = 20) -> list[Task]:
        stmt = (
            select(Task)
            .where(Task.status == "done")
            .order_by(Task.completed_at.desc(), Task.updated_at.desc())
            .limit(limit)
        )
        if self.user_id is not None:
            stmt = stmt.where(Task.user_id == self.user_id)
        return list(await self.session.scalars(stmt))

    async def set_status(self, task_id: str, status: str, reason: str) -> Task:
        allowed_statuses = {
            "inbox",
            "planned",
            "in_progress",
            "waiting",
            "review",
            "done",
            "cancelled",
        }
        if status not in allowed_statuses:
            raise ValueError(f"Unsupported task status: {status}")

        task = await self.get(task_id)
        if task is None:
            raise LookupError("Task not found")
        if task.status == status:
            return task

        old = task_snapshot(task)
        now = datetime.now(UTC)
        task.status = status
        task.last_activity_at = now
        task.completed_at = now if status == "done" else None
        await self.add_event(
            task,
            old,
            task_snapshot(task),
            reason,
            confidence=1.0,
            source="telegram",
        )
        return task

    async def create_from_decision(
        self, decision: EmailDecision, gmail_thread_id: str, source_message_id: str
    ) -> Task:
        now = datetime.now(UTC)
        task = Task(
            user_id=self.user_id,
            title=decision.task_title or decision.summary[:120],
            description=decision.summary,
            project=decision.project,
            status=decision.status or "inbox",
            priority=decision.priority or "medium",
            due_at=decision.due_at,
            assignee=decision.assignee,
            waiting_for=decision.waiting_for,
            next_action=decision.next_action,
            gmail_thread_id=gmail_thread_id,
            source_message_id=source_message_id,
            requires_reply=decision.requires_reply,
            last_activity_at=now,
            completed_at=now if decision.status == "done" else None,
        )
        self.session.add(task)
        await self.session.flush()
        await self.add_event(task, {}, task_snapshot(task), decision.reason, decision.confidence)
        return task

    async def update_from_decision(self, task: Task, decision: EmailDecision) -> Task:
        old = task_snapshot(task)
        now = datetime.now(UTC)
        updates = {
            "title": decision.task_title,
            "description": decision.summary or task.description,
            "project": decision.project,
            "status": decision.status,
            "priority": decision.priority,
            "due_at": decision.due_at,
            "assignee": decision.assignee,
            "waiting_for": decision.waiting_for,
            "next_action": decision.next_action,
            "requires_reply": decision.requires_reply,
            "last_activity_at": now,
        }
        for field, value in updates.items():
            if value is not None:
                setattr(task, field, value)
        task.completed_at = now if task.status == "done" else None
        await self.add_event(task, old, task_snapshot(task), decision.reason, decision.confidence)
        return task

    async def add_event(
        self,
        task: Task,
        old_values: dict[str, Any],
        new_values: dict[str, Any],
        reason: str,
        confidence: float,
        source: str = "agent",
    ) -> None:
        self.session.add(
            TaskEvent(
                task_id=task.id,
                old_values=old_values,
                new_values=new_values,
                reason=reason,
                source=source,
                confidence=confidence,
            )
        )

    async def digest_candidates(self, stale_days: int) -> dict[str, list[Task]]:
        now = datetime.now(UTC)
        today_end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
        stale_before = now - timedelta(days=stale_days)

        async def fetch(stmt: Select[tuple[Task]]) -> list[Task]:
            return list(await self.session.scalars(stmt))

        active = Task.status.not_in(["done", "cancelled"])
        return {
            "due_today": await fetch(
                select(Task).where(active, Task.due_at <= today_end, Task.due_at >= now)
            ),
            "overdue": await fetch(select(Task).where(active, Task.due_at < now)),
            "requires_reply": await fetch(
                select(Task).where(active, Task.requires_reply.is_(True))
            ),
            "waiting": await fetch(select(Task).where(active, Task.status == "waiting")),
            "stale": await fetch(select(Task).where(active, Task.last_activity_at < stale_before)),
            "new": await fetch(select(Task).where(Task.created_at >= now - timedelta(days=1))),
        }


class ApprovalRepository:
    def __init__(self, session: AsyncSession, user_id: str | None = None) -> None:
        self.session = session
        self.user_id = user_id

    async def get(self, approval_id: str) -> ApprovalRequest | None:
        approval = await self.session.get(ApprovalRequest, approval_id)
        if approval and self.user_id is not None and approval.user_id != self.user_id:
            return None
        return approval

    async def list_pending(self) -> list[ApprovalRequest]:
        stmt = select(ApprovalRequest).where(ApprovalRequest.status == "pending")
        if self.user_id is not None:
            stmt = stmt.where(ApprovalRequest.user_id == self.user_id)
        return list(await self.session.scalars(stmt))

    async def pending_for_message(self, gmail_message_id: str) -> ApprovalRequest | None:
        for approval in await self.list_pending():
            if approval.payload.get("gmail_message_id") == gmail_message_id:
                return approval
        return None

    async def pending_for_thread(self, gmail_thread_id: str) -> ApprovalRequest | None:
        for approval in await self.list_pending():
            if approval.payload.get("gmail_thread_id") == gmail_thread_id:
                return approval
        return None

    async def create(
        self, action_type: str, payload: dict[str, Any], langgraph_thread_id: str | None = None
    ) -> ApprovalRequest:
        approval = ApprovalRequest(
            user_id=self.user_id,
            action_type=action_type,
            payload=payload,
            langgraph_thread_id=langgraph_thread_id,
        )
        self.session.add(approval)
        await self.session.flush()
        return approval

    async def resolve(self, approval: ApprovalRequest, status: str) -> ApprovalRequest:
        if approval.status != "pending":
            return approval
        approval.status = status
        approval.resolved_at = datetime.now(UTC)
        return approval


class AgentRunRepository:
    def __init__(self, session: AsyncSession, user_id: str | None = None) -> None:
        self.session = session
        self.user_id = user_id

    async def create(
        self, gmail_message_id: str | None, langgraph_thread_id: str | None
    ) -> AgentRun:
        run = AgentRun(
            user_id=self.user_id,
            gmail_message_id=gmail_message_id,
            langgraph_thread_id=langgraph_thread_id,
        )
        self.session.add(run)
        await self.session.flush()
        return run

    async def list(self, limit: int = 100) -> list[AgentRun]:
        stmt = select(AgentRun).order_by(AgentRun.started_at.desc()).limit(limit)
        if self.user_id is not None:
            stmt = stmt.where(AgentRun.user_id == self.user_id)
        return list(await self.session.scalars(stmt))

    async def finish(
        self,
        run: AgentRun,
        status: str,
        decision: dict[str, Any] | None = None,
        error: str | None = None,
        attempts: int = 1,
    ) -> AgentRun:
        run.status = status
        run.decision = decision
        run.error = error
        run.attempts = attempts
        run.finished_at = datetime.now(UTC)
        if run.started_at:
            started_at = run.started_at
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=UTC)
            run.duration_ms = int((run.finished_at - started_at).total_seconds() * 1000)
        return run


def task_snapshot(task: Task) -> dict[str, Any]:
    return {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "project": task.project,
        "status": task.status,
        "priority": task.priority,
        "due_at": task.due_at.isoformat() if task.due_at else None,
        "assignee": task.assignee,
        "waiting_for": task.waiting_for,
        "next_action": task.next_action,
        "requires_reply": task.requires_reply,
    }


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_user(self, email: str | None = None, display_name: str | None = None) -> User:
        user = User(email=email, display_name=display_name)
        self.session.add(user)
        await self.session.flush()
        self.session.add(UserSettings(user_id=user.id))
        await self.session.flush()
        return user

    async def get_user(self, user_id: str) -> User | None:
        return await self.session.get(User, user_id)

    async def get_by_link_token(self, link_token: str) -> User | None:
        identity = await self.session.scalar(
            select(TelegramIdentity).where(TelegramIdentity.link_token == link_token)
        )
        if identity is None:
            return None
        return await self.session.get(User, identity.user_id)

    async def get_telegram_identity(self, telegram_user_id: str) -> TelegramIdentity | None:
        return await self.session.scalar(
            select(TelegramIdentity).where(TelegramIdentity.telegram_user_id == telegram_user_id)
        )

    async def get_telegram_identity_by_user_id(self, user_id: str) -> TelegramIdentity | None:
        return await self.session.scalar(
            select(TelegramIdentity).where(TelegramIdentity.user_id == user_id)
        )

    async def list_telegram_identities(self) -> list[TelegramIdentity]:
        return list(await self.session.scalars(select(TelegramIdentity)))

    async def upsert_telegram_identity(
        self,
        telegram_user_id: str,
        chat_id: str,
        username: str | None,
        link_token: str,
    ) -> TelegramIdentity:
        identity = await self.session.scalar(
            select(TelegramIdentity).where(TelegramIdentity.telegram_user_id == telegram_user_id)
        )
        if identity is None:
            user = await self.create_user(display_name=username)
            identity = TelegramIdentity(
                user_id=user.id,
                telegram_user_id=telegram_user_id,
                chat_id=chat_id,
                username=username,
                link_token=link_token,
            )
            self.session.add(identity)
        else:
            identity.chat_id = chat_id
            identity.username = username
            identity.link_token = link_token
        await self.session.flush()
        return identity


class MailAccountRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_active(self) -> list[MailAccount]:
        return list(
            await self.session.scalars(select(MailAccount).where(MailAccount.status == "active"))
        )

    async def upsert(
        self,
        user_id: str,
        provider: str,
        email_address: str,
        encrypted_token: str,
        scopes: list[str],
        token_expires_at: datetime | None = None,
        gmail_history_id: str | None = None,
        outlook_delta_link: str | None = None,
        imap_uidvalidity: str | None = None,
        imap_last_uid: str | None = None,
    ) -> MailAccount:
        if provider == "imap":
            await self.session.execute(
                update(MailAccount)
                .where(MailAccount.user_id == user_id)
                .where(MailAccount.provider != "imap")
                .where(MailAccount.email_address.ilike(email_address))
                .where(MailAccount.status == "active")
                .values(status="inactive")
            )
        account = await self.session.scalar(
            select(MailAccount)
            .where(MailAccount.user_id == user_id)
            .where(MailAccount.provider == provider)
            .where(MailAccount.email_address == email_address)
        )
        if account is None:
            account = MailAccount(
                user_id=user_id,
                provider=provider,
                email_address=email_address,
                encrypted_token=encrypted_token,
                scopes=scopes,
                token_expires_at=token_expires_at,
                gmail_history_id=gmail_history_id,
                outlook_delta_link=outlook_delta_link,
                imap_uidvalidity=imap_uidvalidity,
                imap_last_uid=imap_last_uid,
            )
            self.session.add(account)
        else:
            account.encrypted_token = encrypted_token
            account.scopes = scopes
            account.status = "active"
            account.token_expires_at = token_expires_at
            if gmail_history_id:
                account.gmail_history_id = gmail_history_id
            if outlook_delta_link is not None:
                account.outlook_delta_link = outlook_delta_link
            if provider == "imap":
                account.imap_uidvalidity = imap_uidvalidity
                account.imap_last_uid = imap_last_uid
        await self.session.flush()
        return account


class MailSetupSessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        user_id: str,
        provider: str,
        data: dict[str, Any],
        expires_at: datetime,
    ) -> MailSetupSession:
        setup = MailSetupSession(
            user_id=user_id,
            provider=provider,
            data=data,
            expires_at=expires_at,
        )
        self.session.add(setup)
        await self.session.flush()
        return setup

    async def get_valid(self, setup_id: str, provider: str) -> MailSetupSession:
        setup = await self.session.scalar(
            select(MailSetupSession)
            .where(MailSetupSession.id == setup_id)
            .where(MailSetupSession.provider == provider)
        )
        expires_at = setup.expires_at if setup else None
        if expires_at and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if setup is None or expires_at is None or expires_at < datetime.now(UTC):
            raise LookupError("setup link not found or expired")
        return setup

    async def delete(self, setup: MailSetupSession) -> None:
        await self.session.delete(setup)


class OAuthStateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        state: str,
        provider: str,
        user_id: str,
        redirect_uri: str,
        expires_at: datetime,
        code_verifier: str | None = None,
    ) -> OAuthState:
        oauth_state = OAuthState(
            state=state,
            provider=provider,
            user_id=user_id,
            redirect_uri=redirect_uri,
            expires_at=expires_at,
            code_verifier=code_verifier,
        )
        self.session.add(oauth_state)
        await self.session.flush()
        return oauth_state

    async def consume(self, state: str, provider: str) -> OAuthState:
        oauth_state = await self.session.scalar(
            select(OAuthState)
            .where(OAuthState.state == state)
            .where(OAuthState.provider == provider)
        )
        now = datetime.now(UTC)
        expires_at = oauth_state.expires_at if oauth_state else None
        if expires_at and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if oauth_state is None or expires_at is None or expires_at < now:
            raise LookupError("OAuth state not found or expired")
        await self.session.delete(oauth_state)
        return oauth_state
