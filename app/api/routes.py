import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Query, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import (
    AgentRunOut,
    ApprovalOut,
    EditApprovalIn,
    OnboardingLinkOut,
    ProcessEmailOut,
    TaskOut,
    TelegramLinkIn,
)
from app.config import Settings, get_settings
from app.db import get_session
from app.db.repositories import (
    AgentRunRepository,
    ApprovalRepository,
    TaskRepository,
    UserRepository,
)
from app.integrations import TelegramClient, build_email_analyzer, build_mail_gateway
from app.services.approval_service import ApprovalService
from app.services.email_processing import EmailProcessingService
from app.services.imap_setup_service import IMAPSetupService, imap_onboarding_html
from app.services.oauth_service import OAuthService, onboarding_html

router = APIRouter()
logger = logging.getLogger(__name__)


def require_admin(
    settings: Annotated[Settings, Depends(get_settings)],
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> None:
    if settings.admin_api_key and x_api_key != settings.admin_api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API key")


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def ready(session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, str]:
    await session.execute(text("select 1"))
    return {"status": "ready"}


@router.post(
    "/telegram/link-token",
    dependencies=[Depends(require_admin)],
    response_model=OnboardingLinkOut,
)
async def create_telegram_link(
    body: TelegramLinkIn,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    service = OAuthService(settings, session)
    link_token, onboarding_url = await service.create_telegram_link(
        telegram_user_id=body.telegram_user_id,
        chat_id=body.chat_id,
        username=body.username,
    )
    return OnboardingLinkOut(link_token=link_token, onboarding_url=onboarding_url)


@router.post("/onboarding/new", response_model=OnboardingLinkOut)
async def create_web_onboarding(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    service = OAuthService(settings, session)
    link_token, onboarding_url = await service.create_anonymous_onboarding()
    return OnboardingLinkOut(link_token=link_token, onboarding_url=onboarding_url)


@router.get("/onboarding/{link_token}", response_class=HTMLResponse)
async def onboarding(
    link_token: str,
    settings: Annotated[Settings, Depends(get_settings)],
):
    return HTMLResponse(onboarding_html(link_token, settings.app_base_url))


@router.get("/onboarding/imap/{setup_id}", response_class=HTMLResponse)
async def imap_onboarding(
    setup_id: str,
    settings: Annotated[Settings, Depends(get_settings)],
):
    return HTMLResponse(imap_onboarding_html(setup_id, settings.app_base_url))


@router.post("/onboarding/imap/{setup_id}", response_class=HTMLResponse)
async def imap_onboarding_submit(
    setup_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    email_address: Annotated[str, Form()],
    host: Annotated[str, Form()],
    port: Annotated[int, Form()],
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    folder: Annotated[str, Form()] = "INBOX",
    security: Annotated[str, Form()] = "ssl",
):
    service = IMAPSetupService(settings, session)
    try:
        email = await service.connect(
            setup_id,
            email_address,
            host,
            port,
            username,
            password,
            folder,
            security,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        return HTMLResponse(
            imap_onboarding_html(setup_id, settings.app_base_url, str(exc)),
            status_code=400,
        )
    return HTMLResponse(
        "<h1>Почта подключена</h1><p>Адрес: "
        f"{email}. Можно вернуться в Telegram.</p>"
    )


@router.get("/oauth/gmail/start")
async def oauth_gmail_start(
    link_token: Annotated[str, Query()],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    service = OAuthService(settings, session)
    try:
        authorization_url = await service.start_gmail(link_token)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return RedirectResponse(authorization_url)


@router.get("/oauth/gmail/callback", response_class=HTMLResponse)
async def oauth_gmail_callback(
    state: str,
    code: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    service = OAuthService(settings, session)
    try:
        email, user_id = await service.finish_gmail(state, code)
    except LookupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    identity = await UserRepository(session).get_telegram_identity_by_user_id(user_id)
    if identity:
        try:
            await TelegramClient(settings, chat_id=identity.chat_id).send_message(
                f"Gmail подключён: {email}\nТеперь я могу обрабатывать тестовые письма."
            )
        except Exception:
            logger.exception("failed to notify Telegram about Gmail OAuth completion")
    return HTMLResponse("<h1>Gmail подключен</h1><p>Можно вернуться в Telegram.</p>")


@router.get("/oauth/outlook/start")
async def oauth_outlook_start(
    link_token: Annotated[str, Query()],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    service = OAuthService(settings, session)
    try:
        authorization_url = await service.start_outlook(link_token)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return RedirectResponse(authorization_url)


@router.get("/oauth/outlook/callback", response_class=HTMLResponse)
async def oauth_outlook_callback(
    state: str,
    code: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    service = OAuthService(settings, session)
    try:
        email, user_id = await service.finish_outlook(state, code)
    except LookupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        logger.exception("Outlook OAuth callback failed")
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    identity = await UserRepository(session).get_telegram_identity_by_user_id(user_id)
    if identity:
        try:
            await TelegramClient(settings, chat_id=identity.chat_id).send_message(
                f"Outlook подключён: {email}"
            )
        except Exception:
            logger.exception("failed to notify Telegram about Outlook OAuth completion")
    return HTMLResponse("<h1>Outlook подключен</h1><p>Можно вернуться в Telegram.</p>")


@router.get("/tasks", dependencies=[Depends(require_admin)], response_model=list[TaskOut])
async def list_tasks(session: Annotated[AsyncSession, Depends(get_session)]) -> list:
    return await TaskRepository(session).list()


@router.get("/tasks/{task_id}", dependencies=[Depends(require_admin)], response_model=TaskOut)
async def get_task(task_id: str, session: Annotated[AsyncSession, Depends(get_session)]):
    task = await TaskRepository(session).get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return task


@router.get("/runs", dependencies=[Depends(require_admin)], response_model=list[AgentRunOut])
async def list_runs(session: Annotated[AsyncSession, Depends(get_session)]) -> list:
    return await AgentRunRepository(session).list()


@router.get("/approvals", dependencies=[Depends(require_admin)], response_model=list[ApprovalOut])
async def list_approvals(session: Annotated[AsyncSession, Depends(get_session)]) -> list:
    return await ApprovalRepository(session).list_pending()


@router.post(
    "/emails/{gmail_message_id}/process",
    dependencies=[Depends(require_admin)],
    response_model=ProcessEmailOut,
)
async def process_email(
    gmail_message_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    service = EmailProcessingService(
        settings,
        session,
        build_mail_gateway(settings),
        build_email_analyzer(settings),
        TelegramClient(settings),
    )
    return await service.process_gmail_message(gmail_message_id)


@router.post("/approvals/{approval_id}/approve", dependencies=[Depends(require_admin)])
async def approve(
    approval_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    service = ApprovalService(settings, ApprovalRepository(session), TaskRepository(session))
    try:
        approval, task = await service.approve(approval_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await session.commit()
    return {
        "approval_id": approval.id,
        "status": approval.status,
        "task_id": task.id if task else None,
    }


@router.post("/approvals/{approval_id}/reject", dependencies=[Depends(require_admin)])
async def reject(
    approval_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    service = ApprovalService(settings, ApprovalRepository(session), TaskRepository(session))
    try:
        approval = await service.reject(approval_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await session.commit()
    return {"approval_id": approval.id, "status": approval.status}


@router.post("/approvals/{approval_id}/edit", dependencies=[Depends(require_admin)])
async def edit(
    approval_id: str,
    body: EditApprovalIn,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
):
    service = ApprovalService(settings, ApprovalRepository(session), TaskRepository(session))
    try:
        approval, task = await service.edit(approval_id, body.patch)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await session.commit()
    return {
        "approval_id": approval.id,
        "status": approval.status,
        "task_id": task.id if task else None,
    }
