import secrets
from datetime import UTC, datetime, timedelta

import httpx
import msal
from google_auth_oauthlib.flow import Flow
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.repositories import MailAccountRepository, OAuthStateRepository, UserRepository
from app.services.token_service import TokenCipher


class OAuthService:
    def __init__(self, settings: Settings, session: AsyncSession) -> None:
        self.settings = settings
        self.session = session
        self.users = UserRepository(session)
        self.states = OAuthStateRepository(session)
        self.accounts = MailAccountRepository(session)
        self.cipher = TokenCipher(settings)

    async def create_telegram_link(
        self, telegram_user_id: str, chat_id: str, username: str | None = None
    ) -> tuple[str, str]:
        link_token = secrets.token_urlsafe(32)
        await self.users.upsert_telegram_identity(
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            username=username,
            link_token=link_token,
        )
        await self.session.commit()
        return link_token, f"{self.settings.app_base_url.rstrip('/')}/onboarding/{link_token}"

    async def create_anonymous_onboarding(self) -> tuple[str, str]:
        user = await self.users.create_user()
        link_token = secrets.token_urlsafe(32)
        await self.users.upsert_telegram_identity(
            telegram_user_id=f"web-{user.id}",
            chat_id=f"web-{user.id}",
            username=None,
            link_token=link_token,
        )
        await self.session.commit()
        return link_token, f"{self.settings.app_base_url.rstrip('/')}/onboarding/{link_token}"

    async def start_gmail(self, link_token: str) -> str:
        user = await self._user_for_link(link_token)
        redirect_uri = self._redirect_uri("gmail")
        flow = Flow.from_client_secrets_file(
            self.settings.google_client_secret_file,
            scopes=self.settings.google_oauth_scope_list,
            redirect_uri=redirect_uri,
        )
        state = secrets.token_urlsafe(32)
        authorization_url, _ = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
            state=state,
        )
        await self.states.create(
            state=state,
            provider="gmail",
            user_id=user.id,
            redirect_uri=redirect_uri,
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
            code_verifier=flow.code_verifier,
        )
        await self.session.commit()
        return authorization_url

    async def finish_gmail(self, state: str, code: str) -> tuple[str, str]:
        oauth_state = await self.states.consume(state, "gmail")
        flow = Flow.from_client_secrets_file(
            self.settings.google_client_secret_file,
            scopes=self.settings.google_oauth_scope_list,
            redirect_uri=oauth_state.redirect_uri,
            state=state,
        )
        flow.code_verifier = oauth_state.code_verifier
        flow.fetch_token(code=code)
        credentials = flow.credentials
        token_json = credentials.to_json()
        email, history_id = await self._gmail_profile(credentials.token)
        encrypted = self.cipher.encrypt(token_json)
        await self.accounts.upsert(
            user_id=oauth_state.user_id,
            provider="gmail",
            email_address=email,
            encrypted_token=encrypted,
            scopes=list(credentials.scopes or self.settings.google_oauth_scope_list),
            token_expires_at=credentials.expiry,
            gmail_history_id=history_id,
        )
        await self.session.commit()
        return email, oauth_state.user_id

    async def start_outlook(self, link_token: str) -> str:
        if not self.settings.microsoft_client_id or not self.settings.microsoft_client_secret:
            raise RuntimeError(
                "MICROSOFT_CLIENT_ID and MICROSOFT_CLIENT_SECRET are required for Outlook OAuth"
            )
        user = await self._user_for_link(link_token)
        state = secrets.token_urlsafe(32)
        redirect_uri = self._redirect_uri("outlook")
        app = msal.ConfidentialClientApplication(
            self.settings.microsoft_client_id,
            client_credential=self.settings.microsoft_client_secret,
            authority=f"https://login.microsoftonline.com/{self.settings.microsoft_tenant_id}",
        )
        authorization_url = app.get_authorization_request_url(
            scopes=self.settings.microsoft_scope_list,
            state=state,
            redirect_uri=redirect_uri,
            prompt="select_account",
        )
        await self.states.create(
            state=state,
            provider="outlook",
            user_id=user.id,
            redirect_uri=redirect_uri,
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )
        await self.session.commit()
        return authorization_url

    async def finish_outlook(self, state: str, code: str) -> tuple[str, str]:
        if not self.settings.microsoft_client_id or not self.settings.microsoft_client_secret:
            raise RuntimeError(
                "MICROSOFT_CLIENT_ID and MICROSOFT_CLIENT_SECRET are required for Outlook OAuth"
            )
        oauth_state = await self.states.consume(state, "outlook")
        cache = msal.SerializableTokenCache()
        app = msal.ConfidentialClientApplication(
            self.settings.microsoft_client_id,
            client_credential=self.settings.microsoft_client_secret,
            authority=f"https://login.microsoftonline.com/{self.settings.microsoft_tenant_id}",
            token_cache=cache,
        )
        result = app.acquire_token_by_authorization_code(
            code,
            scopes=self.settings.microsoft_scope_list,
            redirect_uri=oauth_state.redirect_uri,
        )
        if "access_token" not in result:
            raise RuntimeError("Microsoft OAuth failed; check the Azure app permissions")
        email = await self._outlook_profile_email(str(result["access_token"]))
        encrypted = self.cipher.encrypt(cache.serialize())
        expires_at = datetime.now(UTC) + timedelta(seconds=int(result.get("expires_in", 3600)))
        await self.accounts.upsert(
            user_id=oauth_state.user_id,
            provider="outlook",
            email_address=email,
            encrypted_token=encrypted,
            scopes=self.settings.microsoft_scope_list,
            token_expires_at=expires_at,
        )
        await self.session.commit()
        return email, oauth_state.user_id

    async def _user_for_link(self, link_token: str):
        user = await self.users.get_by_link_token(link_token)
        if user is None:
            raise LookupError("onboarding link not found")
        return user

    def _redirect_uri(self, provider: str) -> str:
        return f"{self.settings.app_base_url.rstrip('/')}/oauth/{provider}/callback"

    async def _gmail_profile(self, access_token: str | None) -> tuple[str, str | None]:
        if not access_token:
            return "unknown@gmail-account", None
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                "https://gmail.googleapis.com/gmail/v1/users/me/profile",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            response.raise_for_status()
            data = response.json()
            return (
                str(data.get("emailAddress") or "unknown@gmail-account"),
                str(data["historyId"]) if data.get("historyId") else None,
            )

    async def _outlook_profile_email(self, access_token: str) -> str:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                f"{self.settings.microsoft_graph_base_url.rstrip('/')}/me",
                headers={"Authorization": f"Bearer {access_token}"},
                params={"$select": "mail,userPrincipalName"},
            )
            response.raise_for_status()
            data = response.json()
            return str(data.get("mail") or data.get("userPrincipalName") or "unknown@outlook")


def onboarding_html(link_token: str, app_base_url: str) -> str:
    return """<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Mail Task Agent</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 40px; max-width: 720px; }}
    a {{ display: inline-block; margin: 8px 12px 8px 0; padding: 10px 14px;
         border: 1px solid #222; border-radius: 6px; text-decoration: none; color: #111; }}
  </style>
</head>
<body>
  <h1>Подключение почты</h1>
  <p>Выберите почтовый аккаунт. LLM-ключи остаются на backend, пользователь их не вводит.</p>
  <p>Для подключения почты откройте Telegram и используйте кнопку «Привязать почту».</p>
</body>
</html>"""
