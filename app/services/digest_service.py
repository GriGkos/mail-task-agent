from app.config import Settings
from app.db.models import Task
from app.db.repositories import TaskRepository
from app.integrations.telegram import TelegramGateway


class DigestService:
    def __init__(
        self, settings: Settings, tasks: TaskRepository, telegram: TelegramGateway
    ) -> None:
        self.settings = settings
        self.tasks = tasks
        self.telegram = telegram

    async def send_daily_digest(self) -> str | None:
        groups = await self.tasks.digest_candidates(self.settings.stale_task_days)
        text = self.render(groups)
        if not text:
            return None
        return await self.telegram.send_message(text)

    def render(self, groups: dict[str, list[Task]]) -> str:
        titles = {
            "due_today": "Today",
            "overdue": "Overdue",
            "requires_reply": "Needs reply",
            "waiting": "Waiting",
            "stale": "Stale",
            "new": "New in last 24h",
        }
        chunks: list[str] = []
        for key, tasks in groups.items():
            if not tasks:
                continue
            lines = [f"{titles[key]}:"]
            lines.extend(f"- {task.title} [{task.status}]" for task in tasks[:10])
            chunks.append("\n".join(lines))
        return "\n\n".join(chunks)
