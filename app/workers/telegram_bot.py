import asyncio
import logging
from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy import select

from app.config import get_settings
from app.db.models import MailAccount, UserSettings
from app.db.repositories import ApprovalRepository, TaskRepository, UserRepository
from app.db.session import SessionLocal
from app.integrations.telegram import TelegramClient, _TelegramTransport
from app.services.approval_service import ApprovalService
from app.services.oauth_service import OAuthService
from app.services.source_email_service import create_source_email_token

logger = logging.getLogger(__name__)

STATUS_LABELS = {
    "inbox": "входящие",
    "planned": "запланирована",
    "in_progress": "в работе",
    "waiting": "ожидание",
    "review": "на проверке",
    "done": "готово",
    "cancelled": "отменена",
}
PRIORITY_LABELS = {
    "low": "низкий",
    "medium": "средний",
    "high": "высокий",
    "urgent": "срочный",
}
PROVIDER_LABELS = {
    "imap": "Почта",
    "gmail": "Gmail",
    "outlook": "Outlook",
}


def _short_text(value: str, limit: int = 180) -> str:
    value = " ".join(value.split())
    if len(value) <= limit:
        return value
    return f"{value[: limit - 1].rstrip()}…"


def _task_text(task, source_email=None, prefix: str | None = None) -> str:
    lines: list[str] = []
    if prefix:
        lines.extend([prefix, ""])

    raw_title = (task.title or "Без названия").strip()
    title = _short_text(raw_title)
    lines.extend(
        [
            title,
            "",
            f"Статус: {STATUS_LABELS.get(task.status, task.status)}",
            f"Приоритет: {PRIORITY_LABELS.get(task.priority, task.priority)}",
        ]
    )
    details = []
    if task.project:
        details.append(f"Проект: {task.project}")
    if task.due_at:
        details.append(f"Срок: {task.due_at:%d.%m.%Y %H:%M}")
    if task.assignee:
        details.append(f"Ответственный: {task.assignee}")
    if task.waiting_for:
        details.append(f"Ожидаем от: {task.waiting_for}")
    if task.next_action:
        details.append(f"Следующее действие: {task.next_action}")
    if task.requires_reply:
        details.append("Нужен ответ: да")

    lines.extend(details)
    description = (task.description or "").strip()
    description_heading = "Описание"
    if description.casefold().startswith(raw_title.casefold()):
        description = description[len(raw_title) :].lstrip(" \n:,-.;")
        description_heading = "Дополнение"
    if description and description.casefold() != raw_title.casefold():
        lines.extend(["", description_heading, description])

    if source_email:
        received_at = (
            f"{source_email.received_at:%d.%m.%Y %H:%M}"
            if source_email.received_at
            else "Дата не указана"
        )
        lines.extend(
            [
                "",
                "Письмо",
                f"От: {source_email.sender or 'не указан'}",
                f"Тема: {source_email.subject or 'Без темы'}",
                f"Дата: {received_at}",
            ]
        )
    return "\n".join(lines)


def _task_keyboard(task, source_url: str | None = None) -> list[list[dict[str, str]]]:
    if task.status in {"done", "cancelled"}:
        keyboard = [
            [{"text": "Вернуть в работу", "callback_data": f"task:reopen:{task.id}"}]
        ]
    else:
        keyboard = [
            [
                {"text": "Выполнено", "callback_data": f"task:done:{task.id}"},
                {"text": "Отменить", "callback_data": f"task:cancel:{task.id}"},
            ]
        ]
    if source_url:
        keyboard.append([{"text": "Открыть письмо", "url": source_url}])
    return keyboard


def _source_email_url(task, user_id: str) -> str | None:
    if task.source_permalink:
        return task.source_permalink
    if not task.source_message_id or not task.source_message_id.startswith("imap:"):
        return None
    token = create_source_email_token(get_settings(), task.id, user_id)
    base_url = get_settings().app_base_url.rstrip("/")
    return f"{base_url}/source-email/{quote(token, safe='')}"


def _menu() -> list[list[dict[str, str]]]:
    return [
        [
            {"text": "Привязать почту", "callback_data": "connect:imap"},
        ],
        [
            {"text": "Мои задачи", "callback_data": "tasks"},
            {"text": "Выполненные", "callback_data": "tasks:done"},
        ],
        [
            {"text": "Статус", "callback_data": "status"},
        ],
        [{"text": "Ожидают подтверждения", "callback_data": "pending"}],
        [
            {"text": "Проверить почту", "callback_data": "scan"},
            {"text": "Сводка сейчас", "callback_data": "digest"},
        ],
        [
            {"text": "Настройки", "callback_data": "settings"},
            {"text": "Помощь", "callback_data": "help"},
        ],
    ]


async def _ensure_identity(session, telegram_user_id: int, chat_id: int, username: str | None):
    users = UserRepository(session)
    identity = await users.get_telegram_identity(str(telegram_user_id))
    if identity is not None:
        identity.chat_id = str(chat_id)
        identity.username = username
        await session.commit()
        return identity

    service = OAuthService(get_settings(), session)
    await service.create_telegram_link(str(telegram_user_id), str(chat_id), username)
    return await users.get_telegram_identity(str(telegram_user_id))


async def _send_welcome(client: TelegramClient) -> None:
    await client.send_menu(
        "Привет! Я помогу подключить почту и превращать письма в задачи.\n\n"
        "Привяжи почту. По умолчанию включён безопасный режим: письма "
        "анализируются, но реальные задачи и изменения не применяются.",
        _menu(),
    )


async def _send_help(client: TelegramClient) -> None:
    await client.send_message(
        "Команды:\n"
        "/start — главное меню\n"
        "/mail — привязать почту\n"
        "/tasks — мои задачи\n"
        "/done — выполненные задачи\n"
        "/pending — задачи, ожидающие подтверждения\n"
        "/status — состояние подключения и режима\n"
        "/scan — немедленно проверить новые письма\n"
        "/settings — режим обработки почты и подтверждений\n"
        "/digest — сводка задач сейчас\n"
        "/edit <approval_id> title=Новый заголовок — изменить approval\n"
        "/help — эта справка"
    )


async def _connect_gmail(session, identity, client: TelegramClient) -> None:
    service = OAuthService(get_settings(), session)
    try:
        link_token, _ = await service.create_telegram_link(
            identity.telegram_user_id, identity.chat_id, identity.username
        )
        authorization_url = await service.start_gmail(link_token)
    except FileNotFoundError:
        await client.send_message(
            "Не найден Google OAuth-файл. Положите его в "
            "secrets/google_client_secret.json и повторите /connect."
        )
        return
    except Exception:
        logger.exception("failed to create Gmail OAuth URL")
        await client.send_message("Не удалось начать подключение Gmail. Проверьте логи API.")
        return

    await client.send_menu(
        "Откройте ссылку, войдите в Google и разрешите доступ к Gmail. "
        "После завершения я пришлю подтверждение сюда.",
        [[{"text": "Открыть Google", "url": authorization_url}]],
    )


async def _connect_outlook(session, identity, client: TelegramClient) -> None:
    service = OAuthService(get_settings(), session)
    try:
        link_token, _ = await service.create_telegram_link(
            identity.telegram_user_id, identity.chat_id, identity.username
        )
        authorization_url = await service.start_outlook(link_token)
    except Exception:
        logger.exception("failed to create Outlook OAuth URL")
        await client.send_message(
            "Не удалось начать подключение Outlook. Проверьте MICROSOFT_CLIENT_ID и логи API."
        )
        return

    await client.send_menu(
        "Откройте ссылку, войдите в Microsoft и разрешите доступ к Outlook. "
        "После завершения я пришлю подтверждение сюда.",
        [[{"text": "Открыть Microsoft", "url": authorization_url}]],
    )


async def _connect_imap(session, identity, client: TelegramClient) -> None:
    from app.services.imap_setup_service import IMAPSetupService

    service = IMAPSetupService(get_settings(), session)
    setup_id = await service.create_setup_session(identity.user_id)
    base_url = get_settings().app_base_url.rstrip("/")
    setup_url = f"{base_url}/onboarding/imap/{setup_id}"
    await client.send_menu(
        "Привязка почты через IMAP. Откройте форму, выберите почтовый "
        "сервис и введите пароль приложения. "
        "Не отправляйте пароль сообщением в Telegram.",
        [[{"text": "Открыть форму подключения", "url": setup_url}]],
    )


async def _send_tasks(session, identity, client: TelegramClient) -> None:
    repository = TaskRepository(session, user_id=identity.user_id)
    tasks = await repository.list_open(limit=20)
    if not tasks:
        await client.send_message("Активных задач пока нет.")
        return
    await client.send_message(f"Активные задачи: {len(tasks)}")
    for task in tasks:
        source_email = await repository.source_email(task)
        await client.send_menu(
            _task_text(task, source_email),
            _task_keyboard(task, _source_email_url(task, identity.user_id)),
        )


async def _send_completed_tasks(session, identity, client: TelegramClient) -> None:
    repository = TaskRepository(session, user_id=identity.user_id)
    tasks = await repository.list_completed(limit=20)
    if not tasks:
        await client.send_message("Выполненных задач пока нет.")
        return
    await client.send_message(f"Выполненные задачи: {len(tasks)}")
    for task in tasks:
        source_email = await repository.source_email(task)
        await client.send_menu(
            _task_text(task, source_email),
            _task_keyboard(task, _source_email_url(task, identity.user_id)),
        )


async def _send_pending(session, identity, client: TelegramClient) -> None:
    approvals = await ApprovalRepository(session, user_id=identity.user_id).list_pending()
    if not approvals:
        await client.send_message("Задач, ожидающих подтверждения, нет.")
        return

    await client.send_message(f"Ожидают подтверждения: {len(approvals)}")
    for approval in approvals[:20]:
        payload = approval.payload or {}
        decision = payload.get("decision") or {}
        email = payload.get("email") or {}
        title = decision.get("task_title") or decision.get("summary") or "Без названия"
        confidence = decision.get("confidence")
        confidence_text = f"{float(confidence):.2f}" if confidence is not None else "-"
        text = (
            f"Задача: {title}\n"
            f"Тема: {email.get('subject') or '-'}\n"
            f"Сводка: {decision.get('summary') or '-'}\n"
            f"Уверенность: {confidence_text}"
        )
        keyboard = [
            [
                {"text": "Подтвердить", "callback_data": f"approve:{approval.id}"},
                {"text": "Изменить", "callback_data": f"edit:{approval.id}"},
                {"text": "Отклонить", "callback_data": f"reject:{approval.id}"},
            ]
        ]
        permalink = email.get("permalink")
        if permalink:
            keyboard.append([{"text": "Открыть письмо", "url": permalink}])
        await client.send_menu(text, keyboard)


async def _send_status(session, identity, client: TelegramClient) -> None:
    accounts = list(
        await session.scalars(
            select(MailAccount)
            .where(MailAccount.user_id == identity.user_id)
            .where(MailAccount.status == "active")
            .order_by(MailAccount.updated_at.desc())
        )
    )
    user_settings = await session.scalar(
        select(UserSettings).where(UserSettings.user_id == identity.user_id)
    )
    tasks = await TaskRepository(session, user_id=identity.user_id).list_open(limit=1000)
    pending = await ApprovalRepository(session, user_id=identity.user_id).list_pending()
    poll_seconds = min(
        get_settings().gmail_poll_interval_seconds,
        get_settings().outlook_poll_interval_seconds,
        get_settings().imap_poll_interval_seconds,
    )
    poll_interval = f"{poll_seconds} sec." if poll_seconds < 60 else f"{poll_seconds // 60} min."
    account_lines = []
    for account in accounts:
        provider = PROVIDER_LABELS.get(account.provider, account.provider.title())
        suffix = (
            f" (проверка: {account.last_poll_at:%d.%m.%Y %H:%M})"
            if account.last_poll_at
            else ""
        )
        account_lines.append(f"{provider}: подключён: {account.email_address}{suffix}")
    mail_status = "\n".join(account_lines) or "почта не подключена"
    safe_mode = "включён" if user_settings is None or user_settings.safe_mode else "выключен"
    dry_run = "включён" if user_settings is None or user_settings.dry_run else "выключен"
    mail_mode = (
        "автоматический, только новые письма"
        if user_settings is None or user_settings.gmail_mode == "automatic"
        else "только письма с ярлыком или категорией"
    )
    await client.send_message(
        "Статус агента\n"
        f"Интервал проверки: "
        f"каждые {poll_interval}\n"
        f"Почта:\n{mail_status}\n"
        f"Задач: {len(tasks)}\n"
        f"Ожидают подтверждения: {len(pending)}\n"
        f"Режим почты: {mail_mode}\n"
        f"Безопасный режим: {safe_mode}\n"
        f"Dry-run: {dry_run}"
    )


async def _send_settings(session, identity, client: TelegramClient) -> None:
    user_settings = await session.scalar(
        select(UserSettings).where(UserSettings.user_id == identity.user_id)
    )
    if user_settings is None:
        user_settings = UserSettings(user_id=identity.user_id)
        session.add(user_settings)
        await session.flush()
    safe = "включён" if user_settings.safe_mode else "выключен"
    dry = "включён" if user_settings.dry_run else "выключен"
    mail_mode = (
        "автоматический"
        if user_settings.gmail_mode == "automatic"
        else "только по ярлыку или категории"
    )
    await client.send_menu(
        f"Настройки\nБезопасный режим: {safe}\nТестовый режим: {dry}\nПочта: {mail_mode}",
        [
            [
                {
                    "text": "Выключить безопасный режим"
                    if user_settings.safe_mode
                    else "Включить безопасный режим",
                    "callback_data": f"setting:safe_mode:{int(not user_settings.safe_mode)}",
                }
            ],
            [
                {
                    "text": "Выключить тестовый режим"
                    if user_settings.dry_run
                    else "Включить тестовый режим",
                    "callback_data": f"setting:dry_run:{int(not user_settings.dry_run)}",
                }
            ],
            [
                {
                    "text": "Включить режим только по ярлыку или категории"
                    if user_settings.gmail_mode == "automatic"
                    else "Включить автоматический режим",
                    "callback_data": (
                        "setting:gmail_mode:label"
                        if user_settings.gmail_mode == "automatic"
                        else "setting:gmail_mode:automatic"
                    ),
                }
            ],
        ],
    )


async def _set_setting(session, identity, client: TelegramClient, data: str) -> None:
    _, field, raw_value = data.split(":", 2)
    if field in {"safe_mode", "dry_run"} and raw_value in {"0", "1"}:
        value: bool | str = raw_value == "1"
    elif field == "gmail_mode" and raw_value in {"automatic", "label"}:
        value = raw_value
    else:
        return
    user_settings = await session.scalar(
        select(UserSettings).where(UserSettings.user_id == identity.user_id)
    )
    if user_settings is None:
        user_settings = UserSettings(user_id=identity.user_id)
        session.add(user_settings)
    setattr(user_settings, field, value)
    await session.commit()
    await _send_settings(session, identity, client)


async def _send_digest(session, identity, client: TelegramClient) -> None:
    from app.services.digest_service import DigestService

    service = DigestService(
        get_settings(),
        TaskRepository(session, user_id=identity.user_id),
        client,
    )
    groups = await service.tasks.digest_candidates(get_settings().stale_task_days)
    text = service.render(groups) or "Сейчас нет задач для сводки."
    await client.send_message("Сводка задач\n\n" + text)


async def _scan_mail(client: TelegramClient) -> None:
    await client.send_message("Проверяю новые письма в подключённой почте...")
    try:
        from app.workers.gmail_worker import run_once

        await run_once(limit=10)
    except Exception:
        logger.exception("manual mail scan failed")
        await client.send_message("Проверка почты не завершилась. Проверьте логи worker.")
        return
    await client.send_message(
        "Проверка завершена. Новые письма появятся здесь, если агент увидит в них задачу."
    )


async def _handle_task_action(
    session,
    identity,
    client: TelegramClient,
    action: str,
    task_id: str,
    message_id: str | int | None,
) -> None:
    statuses = {
        "done": ("done", "Задача отмечена выполненной."),
        "cancel": ("cancelled", "Задача отменена."),
        "reopen": ("inbox", "Задача возвращена в работу."),
    }
    if action not in statuses:
        return
    status, result = statuses[action]
    repository = TaskRepository(session, user_id=identity.user_id)
    try:
        task = await repository.set_status(task_id, status, result)
    except LookupError:
        await client.send_message("Задача не найдена или больше вам не принадлежит.")
        return
    await session.commit()
    source_email = await repository.source_email(task)
    if message_id is not None:
        await client.edit_message(
            message_id,
            _task_text(task, source_email, result),
            _task_keyboard(task, _source_email_url(task, identity.user_id)),
        )
    else:
        await client.send_menu(
            _task_text(task, source_email, result),
            _task_keyboard(task, _source_email_url(task, identity.user_id)),
        )


async def _handle_approval(
    session, identity, client: TelegramClient, action: str, approval_id: str
) -> None:
    if action == "edit":
        await client.send_message(
            f"Для изменения отправьте:\n/edit {approval_id} title=Новый заголовок\n\n"
            "Также можно изменить priority, status, next_action или waiting_for."
        )
        return

    user_settings = await session.scalar(
        select(UserSettings).where(UserSettings.user_id == identity.user_id)
    )
    user_dry_run = user_settings is None or user_settings.dry_run
    service = ApprovalService(
        get_settings(),
        ApprovalRepository(session, user_id=identity.user_id),
        TaskRepository(session, user_id=identity.user_id),
        dry_run=user_dry_run,
    )
    try:
        if action == "approve":
            approval, task = await service.approve(approval_id)
            message = "Подтверждение принято"
            if task:
                message += f": задача «{task.title}» создана."
            elif user_dry_run:
                message += ". Dry-run включён: задача не записана."
        elif action == "reject":
            approval = await service.reject(approval_id)
            message = "Действие отклонено."
        else:
            return
    except LookupError:
        await client.send_message("Approval не найден или уже обработан.")
        return
    await session.commit()
    await client.send_message(f"{message}\nСтатус: {approval.status}")


async def _edit_approval(session, identity, client: TelegramClient, text: str) -> None:
    parts = text.split(maxsplit=2)
    if len(parts) != 3 or "=" not in parts[2]:
        await client.send_message("Формат: /edit <approval_id> title=Новый заголовок")
        return
    approval_id, assignment = parts[1], parts[2]
    field, value = assignment.split("=", 1)
    aliases = {"title": "task_title"}
    field = aliases.get(field.strip(), field.strip())
    allowed = {
        "task_title",
        "summary",
        "project",
        "status",
        "priority",
        "next_action",
        "waiting_for",
        "assignee",
    }
    if field not in allowed or not value.strip():
        await client.send_message(
            "Можно изменить: title, summary, project, status, priority, "
            "next_action, waiting_for, assignee."
        )
        return

    user_settings = await session.scalar(
        select(UserSettings).where(UserSettings.user_id == identity.user_id)
    )
    service = ApprovalService(
        get_settings(),
        ApprovalRepository(session, user_id=identity.user_id),
        TaskRepository(session, user_id=identity.user_id),
        dry_run=user_settings is None or user_settings.dry_run,
    )
    try:
        approval, task = await service.edit(approval_id, {field: value.strip()})
    except LookupError:
        await client.send_message("Approval не найден или уже обработан.")
        return
    except ValueError:
        await client.send_message("Изменение не прошло проверку схемы задачи.")
        return
    await session.commit()
    result = f"Approval изменён: {approval.status}."
    if task:
        result += f" Задача: {task.title}."
    await client.send_message(result)


async def _handle_message(message: dict[str, Any]) -> None:
    sender = message.get("from") or {}
    chat = message.get("chat") or {}
    telegram_user_id = int(sender.get("id", 0))
    chat_id = int(chat.get("id", telegram_user_id))
    text = str(message.get("text") or "").strip()
    if not telegram_user_id or not text:
        return

    settings = get_settings()
    async with SessionLocal() as session:
        identity = await _ensure_identity(
            session,
            telegram_user_id,
            chat_id,
            sender.get("username"),
        )
        if identity is None:
            return
        client = TelegramClient(settings, chat_id=identity.chat_id)
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower()
        if command == "/start":
            await _send_welcome(client)
        elif command in {"/connect", "/gmail", "/outlook", "/imap", "/mail"}:
            await _connect_imap(session, identity, client)
        elif command == "/tasks":
            await _send_tasks(session, identity, client)
        elif command == "/done":
            await _send_completed_tasks(session, identity, client)
        elif command == "/pending":
            await _send_pending(session, identity, client)
        elif command == "/status":
            await _send_status(session, identity, client)
        elif command == "/digest":
            await _send_digest(session, identity, client)
        elif command == "/scan":
            await _scan_mail(client)
        elif command in {"/settings", "/mode"}:
            await _send_settings(session, identity, client)
        elif command == "/edit":
            await _edit_approval(session, identity, client, text)
        else:
            await _send_help(client)


async def _handle_callback(update: dict[str, Any]) -> None:
    callback = update.get("callback_query") or {}
    data = str(callback.get("data") or "")
    sender = callback.get("from") or {}
    telegram_user_id = int(sender.get("id", 0))
    message = callback.get("message") or {}
    chat_id = int((message.get("chat") or {}).get("id", telegram_user_id))
    if not telegram_user_id or not data:
        return

    settings = get_settings()
    client = TelegramClient(settings, chat_id=chat_id)
    callback_id = str(callback.get("id") or "")
    if callback_id:
        await client.answer_callback(callback_id)

    async with SessionLocal() as session:
        identity = await UserRepository(session).get_telegram_identity(str(telegram_user_id))
        if identity is None:
            await client.send_message("Сначала отправьте /start.")
            return
        if data in {"connect:gmail", "connect:outlook", "connect:imap"}:
            await _connect_imap(session, identity, client)
        elif data == "tasks":
            await _send_tasks(session, identity, client)
        elif data == "tasks:done":
            await _send_completed_tasks(session, identity, client)
        elif data == "pending":
            await _send_pending(session, identity, client)
        elif data == "status":
            await _send_status(session, identity, client)
        elif data == "digest":
            await _send_digest(session, identity, client)
        elif data == "scan":
            await _scan_mail(client)
        elif data == "settings":
            await _send_settings(session, identity, client)
        elif data.startswith("setting:"):
            await _set_setting(session, identity, client, data)
        elif data == "help":
            await _send_help(client)
        elif data.startswith("task:"):
            parts = data.split(":", 2)
            if len(parts) == 3:
                await _handle_task_action(
                    session,
                    identity,
                    client,
                    parts[1],
                    parts[2],
                    message.get("message_id"),
                )
        elif ":" in data:
            action, approval_id = data.split(":", 1)
            await _handle_approval(session, identity, client, action, approval_id)


async def _handle_update(update: dict[str, Any]) -> None:
    if update.get("message"):
        await _handle_message(update["message"])
    elif update.get("callback_query"):
        await _handle_callback(update)


async def main() -> None:
    settings = get_settings()
    if not settings.telegram_bot_token:
        logger.warning("TELEGRAM_BOT_TOKEN is empty; bot worker is idle")
        await asyncio.Event().wait()

    offset = 0
    base_url = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
    try:
        await TelegramClient(settings).set_commands(
            [
                {"command": "start", "description": "Открыть главное меню"},
                {"command": "mail", "description": "Привязать почту"},
                {"command": "tasks", "description": "Показать мои задачи"},
                {"command": "done", "description": "Показать выполненные задачи"},
                {"command": "pending", "description": "Задачи на подтверждении"},
                {"command": "status", "description": "Показать состояние агента"},
                {"command": "scan", "description": "Проверить новые письма"},
                {"command": "settings", "description": "Настройки режима работы"},
                {"command": "digest", "description": "Показать сводку задач"},
                {"command": "help", "description": "Показать помощь"},
            ]
        )
    except httpx.HTTPError:
        logger.exception("failed to configure Telegram commands")
    transport = _TelegramTransport(settings.telegram_api_ip)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(40, connect=10),
        trust_env=False,
        transport=transport,
    ) as client:
        while True:
            try:
                response = await client.get(
                    f"{base_url}/getUpdates",
                    params={
                        "timeout": 25,
                        "offset": offset,
                        "allowed_updates": ["message", "callback_query"],
                    },
                )
                response.raise_for_status()
                payload: dict[str, Any] = response.json()
                for update in payload.get("result", []):
                    update_id = int(update["update_id"])
                    offset = max(offset, update_id + 1)
                    kind = "message" if update.get("message") else "callback_query"
                    logger.info("telegram update received id=%s kind=%s", update_id, kind)
                    try:
                        await _handle_update(update)
                        logger.info("telegram update processed id=%s", update_id)
                    except Exception:
                        logger.exception("telegram update failed id=%s", update_id)
            except httpx.HTTPError:
                logger.exception("telegram polling failed")
                await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
