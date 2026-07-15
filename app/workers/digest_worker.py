import asyncio
from datetime import datetime, timedelta

from app.config import get_settings
from app.db.repositories import TaskRepository, UserRepository
from app.db.session import SessionLocal
from app.integrations import TelegramClient
from app.services.digest_service import DigestService


async def main() -> None:
    settings = get_settings()
    while True:
        now = datetime.now(settings.timezone)
        hour, minute = [int(part) for part in settings.daily_digest_time.split(":", 1)]
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        if not settings.daily_digest_enabled:
            continue
        async with SessionLocal() as session:
            for identity in await UserRepository(session).list_telegram_identities():
                service = DigestService(
                    settings,
                    TaskRepository(session, user_id=identity.user_id),
                    TelegramClient(settings, chat_id=identity.chat_id),
                )
                await service.send_daily_digest()


if __name__ == "__main__":
    asyncio.run(main())
