from uuid import UUID

from app.agent.routing import needs_approval
from app.agent.schemas import EmailDecision
from app.config import Settings
from app.db.models import ApprovalRequest, Task
from app.db.repositories import ApprovalRepository, TaskRepository


class TaskService:
    def __init__(
        self,
        settings: Settings,
        tasks: TaskRepository,
        approvals: ApprovalRepository,
    ) -> None:
        self.settings = settings
        self.tasks = tasks
        self.approvals = approvals

    async def apply_decision(
        self,
        decision: EmailDecision,
        gmail_thread_id: str,
        gmail_message_id: str,
        email_payload: dict,
    ) -> tuple[Task | None, ApprovalRequest | None, str]:
        existing = await self.tasks.by_thread(gmail_thread_id)
        matched = None
        if decision.matched_task_id:
            matched = await self.tasks.get(str(decision.matched_task_id))
            if matched and existing and matched.id == existing.id:
                decision.matched_task_id = None
                matched = None
        if decision.action == "ignore":
            return None, None, "ignored"

        if (existing or matched) and decision.action == "create_task":
            decision.action = "update_task"
            if matched:
                decision.matched_task_id = UUID(matched.id)

        if (
            not existing
            and not matched
            and decision.action
            in {
                "create_task",
                "update_task",
                "request_review",
            }
        ):
            pending = await self.approvals.pending_for_thread(gmail_thread_id)
            if pending:
                pending.payload = {
                    "decision": decision.model_dump(mode="json"),
                    "gmail_thread_id": gmail_thread_id,
                    "gmail_message_id": gmail_message_id,
                    "email": email_payload,
                }
                return None, pending, "needs_approval"

        if needs_approval(decision, self.settings):
            approval = await self.approvals.create(
                "apply_email_decision",
                {
                    "decision": decision.model_dump(mode="json"),
                    "gmail_thread_id": gmail_thread_id,
                    "gmail_message_id": gmail_message_id,
                    "email": email_payload,
                },
                langgraph_thread_id=gmail_thread_id,
            )
            return None, approval, "needs_approval"

        if self.settings.dry_run:
            return None, None, "dry_run"

        if decision.action == "create_task":
            return (
                await self.tasks.create_from_decision(decision, gmail_thread_id, gmail_message_id),
                None,
                "task_created",
            )
        if decision.action == "update_task":
            task = matched or existing
            if task is None:
                task = await self.tasks.create_from_decision(
                    decision, gmail_thread_id, gmail_message_id
                )
                return task, None, "task_created"
            return await self.tasks.update_from_decision(task, decision), None, "task_updated"
        return None, None, "ignored"
