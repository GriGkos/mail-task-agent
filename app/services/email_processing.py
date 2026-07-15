from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.routing import gmail_label_for_decision
from app.agent.schemas import EmailForAnalysis, ProcessingResult, ThreadTask
from app.config import Settings
from app.db.models import AgentRun
from app.db.repositories import (
    AgentRunRepository,
    ApprovalRepository,
    EmailRepository,
    TaskRepository,
)
from app.integrations.deepseek import EmailAnalyzer
from app.integrations.gmail import FetchedEmail, GmailGateway
from app.integrations.telegram import TelegramGateway
from app.services.redaction_service import RedactionService
from app.services.task_service import TaskService


class EmailProcessingService:
    def __init__(
        self,
        settings: Settings,
        session: AsyncSession,
        gmail: GmailGateway,
        analyzer: EmailAnalyzer,
        telegram: TelegramGateway,
        user_id: str | None = None,
    ) -> None:
        self.settings = settings
        self.session = session
        self.gmail = gmail
        self.analyzer = analyzer
        self.telegram = telegram
        self.user_id = user_id
        self.redaction = RedactionService()
        self.emails = EmailRepository(session, user_id=user_id)
        self.tasks = TaskRepository(session, user_id=user_id)
        self.approvals = ApprovalRepository(session, user_id=user_id)
        self.runs = AgentRunRepository(session, user_id=user_id)

    async def process_gmail_message(self, gmail_message_id: str) -> ProcessingResult:
        run = await self.runs.create(gmail_message_id, langgraph_thread_id=None)
        try:
            existing = await self.emails.get_message(gmail_message_id)
            if existing and existing.processing_status in {"processed", "review"}:
                await self.runs.finish(run, "skipped_duplicate", attempts=0)
                await self.session.commit()
                return ProcessingResult(
                    run_id=run.id,
                    status="skipped_duplicate",
                    dry_run=self.settings.dry_run,
                )

            fetched = await self.gmail.fetch_email(gmail_message_id)
            return await self.process_fetched_email(fetched, run)
        except Exception as exc:
            await self.runs.finish(run, "error", error=str(exc))
            await self.session.commit()
            if not self.settings.dry_run:
                await self.gmail.apply_labels(gmail_message_id, ["AI/Error"])
            raise

    async def process_fetched_email(
        self, fetched: FetchedEmail, run: AgentRun | None = None
    ) -> ProcessingResult:
        run = run or await self.runs.create(fetched.gmail_message_id, fetched.gmail_thread_id)
        existing_message = await self.emails.get_message(fetched.gmail_message_id)
        if existing_message and existing_message.processing_status in {"processed", "review"}:
            await self.runs.finish(run, "skipped_duplicate", attempts=0)
            await self.session.commit()
            return ProcessingResult(
                run_id=run.id,
                status="skipped_duplicate",
                dry_run=self.settings.dry_run,
            )

        sanitized = self.redaction.sanitize(fetched.body_text, self.settings.max_email_chars)
        participants = sorted({fetched.sender, *fetched.recipients})
        await self.emails.upsert_thread(
            fetched.gmail_thread_id,
            fetched.subject,
            participants,
            fetched.received_at,
        )
        message = existing_message or await self.emails.create_message(
            fetched.gmail_message_id,
            fetched.gmail_thread_id,
            fetched.sender,
            fetched.recipients,
            fetched.subject,
            fetched.received_at,
            self.redaction.hash_body(sanitized),
        )
        existing_task = await self.tasks.by_thread(fetched.gmail_thread_id)
        candidate_tasks = await self.tasks.list_open(limit=20)
        if existing_task is not None:
            candidate_tasks = [
                existing_task,
                *[task for task in candidate_tasks if task.id != existing_task.id],
            ]
        analysis = EmailForAnalysis(
            gmail_message_id=fetched.gmail_message_id,
            gmail_thread_id=fetched.gmail_thread_id,
            sender=fetched.sender,
            recipients=fetched.recipients,
            subject=fetched.subject,
            received_at=fetched.received_at,
            body_text=sanitized,
            thread_context=[
                self.redaction.sanitize(item, self.settings.max_email_chars)
                for item in fetched.thread_context[-self.settings.max_thread_messages :]
            ],
            existing_tasks=[
                ThreadTask(
                    id=task.id,
                    title=task.title,
                    status=task.status,
                    next_action=task.next_action,
                    gmail_thread_id=task.gmail_thread_id,
                    same_thread=task.gmail_thread_id == fetched.gmail_thread_id,
                )
                for task in candidate_tasks
            ],
        )
        decision = await self.analyzer.analyze(analysis)
        task_service = TaskService(self.settings, self.tasks, self.approvals)
        task, approval, status = await task_service.apply_decision(
            decision,
            fetched.gmail_thread_id,
            fetched.gmail_message_id,
            {
                "sender": fetched.sender,
                "subject": fetched.subject,
                "summary": decision.summary,
                "permalink": fetched.permalink,
            },
        )
        message_status = "processed" if status != "needs_approval" else "review"
        await self.emails.mark_message(message, message_status)
        if approval and not approval.telegram_message_id:
            approval.telegram_message_id = await self.telegram.send_approval(
                approval.id, self._approval_text(fetched, decision), fetched.permalink
            )
        elif task and status in {"task_created", "task_updated"}:
            await self.telegram.send_message(self._task_notice(fetched, task, status))
        if not self.settings.dry_run:
            labels = [gmail_label_for_decision(decision), "AI/Processed"]
            await self.gmail.apply_labels(fetched.gmail_message_id, labels)
        await self.runs.finish(run, status, decision.model_dump(mode="json"))
        await self.session.commit()
        return ProcessingResult(
            run_id=run.id,
            status=status,
            decision=decision,
            task_id=task.id if task else None,
            approval_id=approval.id if approval else None,
            dry_run=self.settings.dry_run,
        )

    def _approval_text(self, email: FetchedEmail, decision) -> str:
        action_labels = {
            "create_task": "создать задачу",
            "update_task": "обновить задачу",
            "request_review": "нужна проверка",
            "ignore": "пропустить",
        }
        task_title = (
            decision.task_title or decision.summary[:120] or decision.matched_task_id or "-"
        )
        return (
            f"Отправитель: {email.sender}\n"
            f"Тема: {email.subject}\n"
            f"Сводка: {decision.summary}\n"
            f"Действие: {action_labels.get(decision.action, decision.action)}\n"
            f"Задача: {task_title}\n"
            f"Уверенность: {decision.confidence:.2f}\n"
            f"Причина: {decision.reason}"
        )

    def _task_notice(self, email: FetchedEmail, task, status: str) -> str:
        heading = "Новая задача добавлена" if status == "task_created" else "Задача обновлена"
        source_lines = [
            f"От: {email.sender}",
            f"Тема: {email.subject or 'Без темы'}",
        ]
        if email.permalink:
            source_lines.append(f"Письмо: {email.permalink}")
        return "\n".join(
            [
                heading,
                "",
                task.title,
                "Источник",
                *source_lines,
                "",
                "Откройте /tasks, чтобы посмотреть задачу и выполнить действие.",
            ]
        )
