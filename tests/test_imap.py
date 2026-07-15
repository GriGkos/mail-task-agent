from datetime import UTC

import pytest

from app.integrations.imap import (
    IMAPClient,
    IMAPCursorExpired,
    _message_body,
    known_imap_preset,
)
from app.services.imap_setup_service import imap_onboarding_html

RAW_MESSAGE = b"""From: Elena <elena@example.com>
To: me@example.com
Subject: =?utf-8?b?0J/RgNC40LLQtdGC?=
Date: Mon, 29 Jun 2026 08:30:00 +0000
Message-ID: <message-1@example.com>
Content-Type: text/plain; charset=utf-8

Please call Matthew today.
"""


class FakeIMAP:
    def __init__(self) -> None:
        self.started_tls = False
        self.logged_in = False
        self.selected = False

    def login(self, username: str, password: str):
        self.logged_in = username == "me@example.com" and password == "app-password"
        return ("OK", [b"logged in"])

    def starttls(self):
        self.started_tls = True
        return ("OK", [b"ready"])

    def select(self, folder: str, readonly: bool = True):
        self.selected = folder == "INBOX" and readonly
        return ("OK", [b"1"])

    def status(self, folder: str, status: str):
        return ("OK", [b"INBOX (UIDVALIDITY 7 UIDNEXT 4)"])

    def uid(self, command: str, *args):
        if command.lower() == "search":
            return ("OK", [b"2 3"])
        if command.lower() == "fetch":
            return ("OK", [(b"header", RAW_MESSAGE), b")"])
        raise AssertionError(command)

    def close(self):
        return ("OK", [b"closed"])

    def logout(self):
        return ("BYE", [b"logged out"])


def client(settings) -> IMAPClient:
    return IMAPClient(
        settings,
        '{"host":"imap.example.com","port":993,"username":"me@example.com",'
        '"password":"app-password","folder":"INBOX","security":"ssl"}',
        account_id="account-1",
    )


@pytest.mark.asyncio
async def test_imap_baseline_and_new_ids(monkeypatch, settings):
    fake = FakeIMAP()
    monkeypatch.setattr("app.integrations.imap.imaplib.IMAP4_SSL", lambda *args: fake)
    imap = client(settings)

    assert await imap.initialize_imap_cursor() == ("7", "3")
    ids, uidvalidity, last_uid = await imap.list_new_message_ids("7", "1", limit=10)

    assert ids == ["imap:account-1:2", "imap:account-1:3"]
    assert uidvalidity == "7"
    assert last_uid == "3"


@pytest.mark.asyncio
async def test_imap_detects_uidvalidity_change(monkeypatch, settings):
    fake = FakeIMAP()
    monkeypatch.setattr("app.integrations.imap.imaplib.IMAP4_SSL", lambda *args: fake)

    with pytest.raises(IMAPCursorExpired):
        await client(settings).list_new_message_ids("old", "1")


@pytest.mark.asyncio
async def test_imap_fetches_headers_and_body(monkeypatch, settings):
    fake = FakeIMAP()
    monkeypatch.setattr("app.integrations.imap.imaplib.IMAP4_SSL", lambda *args: fake)

    email = await client(settings).fetch_email("imap:account-1:2")

    assert email.gmail_message_id == "imap:account-1:2"
    assert email.sender == "Elena <elena@example.com>"
    assert email.recipients == ["me@example.com"]
    assert email.subject == "Привет"
    assert email.received_at is not None
    assert email.received_at.tzinfo == UTC
    assert "Matthew" in email.body_text
    assert email.gmail_thread_id.startswith("imap:account-1:")


def test_imap_body_converts_html():
    from email import policy
    from email.parser import BytesParser

    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: text/html; charset=utf-8\n\n<p>Hello<br>world</p>"
    )

    assert _message_body(message) == "Hello\nworld"


def test_known_imap_presets_cover_common_domains():
    assert known_imap_preset("person@gmail.com") == ("imap.gmail.com", 993, "ssl")
    assert known_imap_preset("person@outlook.com") == ("imap-mail.outlook.com", 993, "ssl")
    assert known_imap_preset("person@yandex.ru") == ("imap.yandex.ru", 993, "ssl")
    assert known_imap_preset("person@mail.ru") == ("imap.mail.ru", 993, "ssl")
    assert known_imap_preset("person@custom.example") is None


def test_imap_form_explains_application_passwords():
    html = imap_onboarding_html("setup-1", "https://mailtaskbot.ru")

    assert "Пароль приложения" in html
    assert "imap-mail.outlook.com" in html
    assert "imap.yandex.ru" in html
    assert "imap.mail.ru" in html
    assert "В Яндекс.Почте включите IMAP" in html
    assert "пароль для внешнего приложения" in html
