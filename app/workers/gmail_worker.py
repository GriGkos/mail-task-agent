import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import select

from app.config import get_settings
from app.db.models import MailAccount, UserSettings
from app.db.repositories import MailAccountRepository, UserRepository
from app.db.session import SessionLocal
from app.integrations import TelegramClient, build_email_analyzer, build_mail_gateway
from app.integrations.gmail import GmailHistoryExpired
from app.integrations.imap import IMAPCursorExpired
from app.integrations.outlook import OutlookDeltaExpired
from app.services.email_processing import EmailProcessingService
from app.services.token_service import TokenCipher

logger = logging.getLogger(__name__)


async def run_once(limit: int | None = None) -> None:
    settings = get_settings()
    batch_size = limit or settings.gmail_batch_size
    async with SessionLocal() as session:
        accounts = await MailAccountRepository(session).list_active()
        if not accounts:
            if settings.telegram_bot_token:
                logger.info("No linked mail accounts; waiting for Telegram onboarding")
                return
            await _process_single_user_mode(settings, session, batch_size)
            return
        cipher = TokenCipher(settings)
        users = UserRepository(session)
        for account in accounts:
            user_settings = await session.scalar(
                select(UserSettings).where(UserSettings.user_id == account.user_id)
            )
            account_settings = settings
            if user_settings:
                account_settings = settings.model_copy(
                    update={
                        "safe_mode": user_settings.safe_mode,
                        "dry_run": user_settings.dry_run,
                        "gmail_query": user_settings.gmail_query,
                        "gmail_mode": user_settings.gmail_mode,
                        "outlook_category": user_settings.outlook_category,
                    }
                )
            token_payload = cipher.decrypt(account.encrypted_token)
            mail = build_mail_gateway(
                account_settings, account=account, token_payload=token_payload
            )
            identity = await users.get_telegram_identity_by_user_id(account.user_id)
            chat_id = identity.chat_id if identity else None
            await _process_account(
                account_settings, session, mail, account, account.user_id, chat_id, batch_size
            )


async def _process_single_user_mode(settings, session, limit: int) -> None:
    mail = build_mail_gateway(settings)
    await _process_account(settings, session, mail, None, None, None, limit)


async def _process_account(
    settings,
    session,
    mail,
    account: MailAccount | None,
    user_id: str | None,
    chat_id: str | None,
    limit: int,
) -> None:
    await mail.ensure_ai_labels()
    history_cursor: str | None = None
    outlook_cursor: str | None = None
    imap_cursor: tuple[str, str] | None = None
    mail_mode = getattr(settings, "gmail_mode", "automatic")
    if account is not None and account.provider == "imap":
        if not account.imap_uidvalidity or not account.imap_last_uid:
            uidvalidity, last_uid = await mail.initialize_imap_cursor()
            account.imap_uidvalidity = uidvalidity
            account.imap_last_uid = last_uid
            account.last_poll_at = datetime.now(UTC)
            await session.commit()
            logger.info("Initialized IMAP baseline for account %s", account.email_address)
            return
        try:
            ids, uidvalidity, last_uid = await mail.list_new_message_ids(
                account.imap_uidvalidity, account.imap_last_uid, limit=limit
            )
            imap_cursor = (uidvalidity, last_uid)
        except IMAPCursorExpired:
            uidvalidity, last_uid = await mail.initialize_imap_cursor()
            account.imap_uidvalidity = uidvalidity
            account.imap_last_uid = last_uid
            account.last_poll_at = datetime.now(UTC)
            await session.commit()
            logger.warning("IMAP UIDVALIDITY changed; reset baseline for %s", account.email_address)
            return
    elif account is not None and account.provider == "gmail" and mail_mode == "automatic":
        if not account.gmail_history_id:
            account.gmail_history_id = await mail.current_history_id()
            account.last_poll_at = datetime.now(UTC)
            await session.commit()
            logger.info("Initialized Gmail history baseline for account %s", account.email_address)
            return
        try:
            ids, history_cursor = await mail.list_new_message_ids(
                account.gmail_history_id, limit=limit
            )
        except GmailHistoryExpired:
            account.gmail_history_id = await mail.current_history_id()
            account.last_poll_at = datetime.now(UTC)
            await session.commit()
            logger.warning("Gmail history expired; reset baseline for %s", account.email_address)
            return
    elif account is not None and account.provider == "outlook" and mail_mode == "automatic":
        if not account.outlook_delta_link:
            account.outlook_delta_link = await mail.initialize_delta()
            account.last_poll_at = datetime.now(UTC)
            await session.commit()
            logger.info("Initialized Outlook delta baseline for account %s", account.email_address)
            return
        try:
            ids, outlook_cursor = await mail.list_new_message_ids(
                account.outlook_delta_link, limit=limit
            )
        except OutlookDeltaExpired:
            account.outlook_delta_link = await mail.initialize_delta()
            account.last_poll_at = datetime.now(UTC)
            await session.commit()
            logger.warning("Outlook delta expired; reset baseline for %s", account.email_address)
            return
    else:
        ids = await mail.list_message_ids(settings.gmail_query, limit=limit)

    service = EmailProcessingService(
        settings,
        session,
        mail,
        build_email_analyzer(settings),
        TelegramClient(settings, chat_id=chat_id),
        user_id=user_id,
    )
    failed = False
    for message_id in ids:
        try:
            await service.process_gmail_message(message_id)
        except Exception:
            failed = True
            logger.exception("failed to process mail message %s", message_id)
    if account is not None:
        if history_cursor is not None and not failed:
            account.gmail_history_id = history_cursor
        if outlook_cursor is not None and not failed:
            account.outlook_delta_link = outlook_cursor
        if imap_cursor is not None and not failed:
            account.imap_uidvalidity, account.imap_last_uid = imap_cursor
        account.last_poll_at = datetime.now(UTC)
    await session.commit()


async def main() -> None:
    settings = get_settings()
    while True:
        await run_once()
        interval = min(
            settings.gmail_poll_interval_seconds,
            settings.outlook_poll_interval_seconds,
            settings.imap_poll_interval_seconds,
        )
        await asyncio.sleep(interval)


if __name__ == "__main__":
    asyncio.run(main())
