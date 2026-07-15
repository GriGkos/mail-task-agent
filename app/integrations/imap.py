from __future__ import annotations

import asyncio
import imaplib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from email import policy
from email.header import decode_header, make_header
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any

from bs4 import BeautifulSoup

from app.config import Settings
from app.integrations.gmail import FetchedEmail


class IMAPCursorExpired(RuntimeError):
    pass


class IMAPAuthenticationError(RuntimeError):
    pass


KNOWN_IMAP_PRESETS: dict[str, tuple[str, int, str]] = {
    "gmail.com": ("imap.gmail.com", 993, "ssl"),
    "googlemail.com": ("imap.gmail.com", 993, "ssl"),
    "outlook.com": ("imap-mail.outlook.com", 993, "ssl"),
    "hotmail.com": ("imap-mail.outlook.com", 993, "ssl"),
    "hotmail.co.uk": ("imap-mail.outlook.com", 993, "ssl"),
    "live.com": ("imap-mail.outlook.com", 993, "ssl"),
    "msn.com": ("imap-mail.outlook.com", 993, "ssl"),
    "yandex.ru": ("imap.yandex.ru", 993, "ssl"),
    "ya.ru": ("imap.yandex.ru", 993, "ssl"),
    "yandex.com": ("imap.yandex.com", 993, "ssl"),
    "mail.ru": ("imap.mail.ru", 993, "ssl"),
    "inbox.ru": ("imap.mail.ru", 993, "ssl"),
    "list.ru": ("imap.mail.ru", 993, "ssl"),
    "bk.ru": ("imap.mail.ru", 993, "ssl"),
    "yahoo.com": ("imap.mail.yahoo.com", 993, "ssl"),
    "yahoo.co.uk": ("imap.mail.yahoo.com", 993, "ssl"),
    "icloud.com": ("imap.mail.me.com", 993, "ssl"),
    "me.com": ("imap.mail.me.com", 993, "ssl"),
    "mac.com": ("imap.mail.me.com", 993, "ssl"),
}


def known_imap_preset(email_address: str) -> tuple[str, int, str] | None:
    domain = email_address.rsplit("@", 1)[-1].lower().strip()
    return KNOWN_IMAP_PRESETS.get(domain)


@dataclass(slots=True)
class IMAPAccountConfig:
    host: str
    port: int
    username: str
    password: str
    folder: str = "INBOX"
    security: str = "ssl"

    @classmethod
    def from_json(cls, payload: str) -> IMAPAccountConfig:
        data = json.loads(payload)
        return cls(
            host=str(data["host"]).strip(),
            port=int(data.get("port", 993)),
            username=str(data["username"]).strip(),
            password=str(data["password"]),
            folder=str(data.get("folder") or "INBOX").strip() or "INBOX",
            security=str(data.get("security") or ("ssl" if data.get("use_ssl", True) else "none")),
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "host": self.host,
                "port": self.port,
                "username": self.username,
                "password": self.password,
                "folder": self.folder,
                "security": self.security,
            }
        )


class IMAPClient:
    def __init__(self, settings: Settings, token_payload: str, account_id: str) -> None:
        self.settings = settings
        self.config = IMAPAccountConfig.from_json(token_payload)
        self.account_id = account_id

    async def verify(self) -> None:
        await asyncio.to_thread(self._verify_sync)

    async def current_history_id(self) -> str:
        _, max_uid = await asyncio.to_thread(self._mailbox_cursor_sync)
        return max_uid

    async def initialize_imap_cursor(self) -> tuple[str, str]:
        return await asyncio.to_thread(self._mailbox_cursor_sync)

    async def list_new_message_ids(
        self, uidvalidity: str, last_uid: str, limit: int = 25
    ) -> tuple[list[str], str, str]:
        return await asyncio.to_thread(
            self._list_new_message_ids_sync, uidvalidity, last_uid, limit
        )

    async def list_message_ids(self, query: str, limit: int = 10) -> list[str]:
        return await asyncio.to_thread(self._list_message_ids_sync, limit)

    async def fetch_email(self, message_id: str) -> FetchedEmail:
        uid = self._uid_from_message_id(message_id)
        raw, thread_raw = await asyncio.to_thread(self._fetch_message_sync, uid)
        message = BytesParser(policy=policy.default).parsebytes(raw)
        thread_messages = [
            BytesParser(policy=policy.default).parsebytes(item) for item in thread_raw
        ]
        body = _message_body(message)
        thread_context = [_message_body(item) for item in thread_messages]
        message_id_header = str(message.get("Message-ID") or message_id)
        references = str(message.get("References") or message.get("In-Reply-To") or "")
        thread_key = references.split()[0] if references else message_id_header
        return FetchedEmail(
            gmail_message_id=message_id,
            gmail_thread_id=f"imap:{self.account_id}:{_safe_id(thread_key)}",
            sender=_addresses(message.get("From")),
            recipients=_address_list(message, "To", "Cc"),
            subject=_decode_header(str(message.get("Subject") or "")),
            received_at=_parse_date(str(message.get("Date") or "")),
            body_text=body,
            thread_context=thread_context[-self.settings.max_thread_messages :],
            permalink=None,
        )

    async def ensure_ai_labels(self) -> None:
        # IMAP has no portable label/category API. State and deduplication live in our DB.
        return None

    async def apply_labels(self, message_id: str, labels: Iterable[str]) -> None:
        # Provider-specific labels are intentionally not emulated with IMAP flags.
        return None

    def _connect(self) -> imaplib.IMAP4:
        if self.config.security == "ssl":
            return imaplib.IMAP4_SSL(self.config.host, self.config.port)
        return imaplib.IMAP4(self.config.host, self.config.port)

    def _verify_sync(self) -> None:
        client = self._connect()
        try:
            self._login_and_select(client)
        finally:
            self._close(client)

    def _mailbox_cursor_sync(self) -> tuple[str, str]:
        client = self._connect()
        try:
            self._login_and_select(client)
            return self._mailbox_cursor_with_client(client)
        finally:
            self._close(client)

    def _list_message_ids_sync(self, limit: int) -> list[str]:
        client = self._connect()
        try:
            self._login_and_select(client)
            status, data = client.uid("search", None, "ALL")
            if status != "OK":
                raise RuntimeError("IMAP UID search failed")
            uids = [item.decode("ascii") for item in (data[0] or b"").split()]
            return [self._message_id(uid) for uid in uids[-limit:]]
        finally:
            self._close(client)

    def _list_new_message_ids_sync(
        self, uidvalidity: str, last_uid: str, limit: int
    ) -> tuple[list[str], str, str]:
        client = self._connect()
        try:
            self._login_and_select(client)
            selected_uidvalidity, current_uid = self._mailbox_cursor_with_client(client)
            if uidvalidity and uidvalidity != selected_uidvalidity:
                raise IMAPCursorExpired
            start = int(last_uid or 0) + 1
            if start > int(current_uid):
                return [], selected_uidvalidity, current_uid
            status, data = client.uid("search", None, f"UID {start}:{current_uid}")
            if status != "OK":
                raise RuntimeError("IMAP UID search failed")
            uids = [item.decode("ascii") for item in (data[0] or b"").split()]
            uids = uids[:limit]
            next_uid = max([int(last_uid or 0), *(int(item) for item in uids)])
            return [self._message_id(uid) for uid in uids], selected_uidvalidity, str(next_uid)
        finally:
            self._close(client)

    def _fetch_message_sync(self, uid: str) -> tuple[bytes, list[bytes]]:
        client = self._connect()
        try:
            self._login_and_select(client)
            status, data = client.uid("fetch", uid, "(RFC822)")
            if status != "OK" or not data:
                raise RuntimeError(f"IMAP message {uid} was not found")
            raw = next((item[1] for item in data if isinstance(item, tuple)), None)
            if not raw:
                raise RuntimeError(f"IMAP message {uid} has no content")
            return raw, []
        finally:
            self._close(client)

    def _login_and_select(self, client: imaplib.IMAP4) -> None:
        try:
            if self.config.security == "starttls":
                status, _ = client.starttls()
                if status != "OK":
                    raise RuntimeError("IMAP STARTTLS negotiation failed")
            status, _ = client.login(self.config.username, self.config.password)
            if status != "OK":
                raise IMAPAuthenticationError("IMAP login failed")
            status, _ = client.select(self.config.folder, readonly=True)
            if status != "OK":
                raise RuntimeError(f"IMAP folder does not exist: {self.config.folder}")
        except imaplib.IMAP4.error as exc:
            raise IMAPAuthenticationError("IMAP login or folder selection failed") from exc

    def _mailbox_cursor_with_client(self, client: imaplib.IMAP4) -> tuple[str, str]:
        status, data = client.status(self.config.folder, "(UIDVALIDITY UIDNEXT)")
        if status != "OK" or not data or not data[0]:
            raise RuntimeError("IMAP did not return mailbox cursor")
        text = data[0].decode("utf-8", errors="ignore")
        uidvalidity = _status_value(text, "UIDVALIDITY")
        uidnext = _status_value(text, "UIDNEXT")
        return uidvalidity, str(max(int(uidnext) - 1, 0))

    def _message_id(self, uid: str) -> str:
        return f"imap:{self.account_id}:{uid}"

    @staticmethod
    def _uid_from_message_id(message_id: str) -> str:
        return message_id.rsplit(":", 1)[-1]

    @staticmethod
    def _close(client: imaplib.IMAP4) -> None:
        try:
            client.close()
        except (imaplib.IMAP4.error, OSError):
            pass
        try:
            client.logout()
        except (imaplib.IMAP4.error, OSError):
            pass


def _status_value(status: str, name: str) -> str:
    match = re.search(rf"\b{name}\s+(\d+)", status, flags=re.IGNORECASE)
    if not match:
        raise RuntimeError(f"IMAP status has no {name}")
    return match.group(1)


def _decode_header(value: str) -> str:
    return str(make_header(decode_header(value)))


def _addresses(value: Any) -> str:
    values = getaddresses([str(value or "")])
    return ", ".join(
        f"{name} <{address}>" if name else address for name, address in values if address
    )


def _address_list(message: Any, *headers: str) -> list[str]:
    values: list[str] = []
    for header in headers:
        for name, address in getaddresses([str(message.get(header) or "")]):
            if address:
                values.append(f"{name} <{address}>" if name else address)
    return values


def _message_body(message: Any) -> str:
    if message.is_multipart():
        plain: list[str] = []
        html: list[str] = []
        for part in message.walk():
            if part.get_content_disposition() == "attachment":
                continue
            content_type = part.get_content_type()
            try:
                content = part.get_content()
            except (LookupError, UnicodeError):
                continue
            if content_type == "text/plain":
                plain.append(str(content))
            elif content_type == "text/html":
                html.append(BeautifulSoup(str(content), "html.parser").get_text("\n"))
        return "\n".join(plain or html).strip()
    content = message.get_content()
    if message.get_content_type() == "text/html":
        return BeautifulSoup(str(content), "html.parser").get_text("\n").strip()
    return str(content).strip()


def _parse_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.@-]+", "_", value)[:220]
