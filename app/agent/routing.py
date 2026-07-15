from app.agent.schemas import EmailDecision
from app.config import Settings


def needs_approval(decision: EmailDecision, settings: Settings) -> bool:
    if decision.action == "request_review":
        return True
    if decision.matched_task_id:
        return True
    if settings.safe_mode and decision.action in {"create_task", "update_task"}:
        return True
    if decision.status == "done":
        return True
    if decision.action in {"create_task", "update_task"}:
        return decision.confidence < settings.auto_action_confidence
    return decision.confidence < settings.review_confidence


def gmail_label_for_decision(decision: EmailDecision) -> str:
    if decision.action == "ignore":
        return "AI/Info"
    if decision.status == "waiting":
        return "AI/Waiting"
    if decision.action == "request_review":
        return "AI/Review"
    if decision.action in {"create_task", "update_task"}:
        return "AI/Task"
    return decision.proposed_gmail_label or "AI/Info"
