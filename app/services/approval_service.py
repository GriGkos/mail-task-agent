from pydantic import ValidationError

from app.agent.schemas import EmailDecision
from app.config import Settings
from app.db.models import ApprovalRequest, Task
from app.db.repositories import ApprovalRepository, TaskRepository


class ApprovalService:
    def __init__(
        self,
        settings: Settings,
        approvals: ApprovalRepository,
        tasks: TaskRepository,
        *,
        dry_run: bool | None = None,
    ) -> None:
        self.settings = settings
        self.approvals = approvals
        self.tasks = tasks
        self.dry_run = settings.dry_run if dry_run is None else dry_run

    async def approve(self, approval_id: str) -> tuple[ApprovalRequest, Task | None]:
        approval = await self._pending(approval_id)
        task = await self._apply_payload(approval)
        await self.approvals.resolve(approval, "approved")
        return approval, task

    async def reject(self, approval_id: str) -> ApprovalRequest:
        approval = await self._pending(approval_id)
        return await self.approvals.resolve(approval, "rejected")

    async def edit(self, approval_id: str, patch: dict) -> tuple[ApprovalRequest, Task | None]:
        approval = await self._pending(approval_id)
        payload = dict(approval.payload)
        decision_data = dict(payload.get("decision", {}))
        decision_data.update(patch)
        try:
            EmailDecision.model_validate(decision_data)
        except ValidationError as exc:
            raise ValueError(str(exc)) from exc
        payload["decision"] = decision_data
        approval.payload = payload
        task = await self._apply_payload(approval)
        await self.approvals.resolve(approval, "edited")
        return approval, task

    async def _pending(self, approval_id: str) -> ApprovalRequest:
        approval = await self.approvals.get(approval_id)
        if approval is None:
            raise LookupError("approval not found")
        return approval

    async def _apply_payload(self, approval: ApprovalRequest) -> Task | None:
        if approval.status != "pending":
            return None
        if self.dry_run:
            return None
        payload = approval.payload
        decision = EmailDecision.model_validate(payload["decision"])
        thread_id = payload["gmail_thread_id"]
        message_id = payload["gmail_message_id"]
        existing = await self.tasks.by_thread(thread_id)
        matched = (
            await self.tasks.get(str(decision.matched_task_id))
            if decision.matched_task_id
            else None
        )
        task = matched or existing
        if decision.action in {"create_task", "update_task"} and task is None:
            return await self.tasks.create_from_decision(decision, thread_id, message_id)
        if task:
            return await self.tasks.update_from_decision(task, decision)
        return None
