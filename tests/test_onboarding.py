import pytest
from sqlalchemy import select

from app.db.models import OAuthState, TelegramIdentity, User
from app.db.repositories import UserRepository
from app.services.oauth_service import OAuthService, onboarding_html
from app.services.token_service import TokenCipher


def test_token_cipher_round_trip(settings):
    cipher = TokenCipher(settings)

    encrypted = cipher.encrypt('{"refresh_token":"secret"}')

    assert "secret" not in encrypted
    assert cipher.decrypt(encrypted) == '{"refresh_token":"secret"}'


async def test_create_telegram_link_creates_user_and_identity(session, settings):
    service = OAuthService(settings, session)
    token, url = await service.create_telegram_link("42", "chat-42", "alice")

    identity = (await session.scalars(select(TelegramIdentity))).one()
    user = (await session.scalars(select(User))).one()

    assert identity.user_id == user.id
    assert identity.telegram_user_id == "42"
    assert identity.link_token == token
    assert url.endswith(f"/onboarding/{token}")


async def test_user_repository_finds_user_by_link_token(session):
    repo = UserRepository(session)
    identity = await repo.upsert_telegram_identity("42", "chat-42", "alice", "link-token")
    await session.commit()

    user = await repo.get_by_link_token("link-token")

    assert user is not None
    assert user.id == identity.user_id


def test_onboarding_html_points_to_mail_connection(settings):
    html = onboarding_html("abc", settings.app_base_url)

    assert "Привязать почту" in html
    assert "/oauth/gmail/start" not in html
    assert "/oauth/outlook/start" not in html


@pytest.mark.asyncio
async def test_gmail_oauth_persists_pkce_verifier(monkeypatch, session, settings):
    class FakeFlow:
        code_verifier = None

        def authorization_url(self, **kwargs):
            self.code_verifier = "test-code-verifier"
            return "https://accounts.google.com/test", kwargs["state"]

    monkeypatch.setattr(
        "app.services.oauth_service.Flow.from_client_secrets_file",
        lambda *args, **kwargs: FakeFlow(),
    )
    service = OAuthService(settings, session)
    link_token, _ = await service.create_telegram_link("42", "chat-42", "alice")

    await service.start_gmail(link_token)

    oauth_state = (await session.scalars(select(OAuthState))).one()
    assert oauth_state.code_verifier == "test-code-verifier"
