import base64
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Protocol

from bs4 import BeautifulSoup
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.config import Settings

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
AI_LABELS = [
    "AI_TEST",
    "AI/Task",
    "AI/Waiting",
    "AI/Review",
    "AI/Info",
    "AI/Processed",
    "AI/Error",
]


@dataclass(slots=True)
class FetchedEmail:
    gmail_message_id: str
    gmail_thread_id: str
    sender: str
    recipients: list[str]
    subject: str
    received_at: datetime | None
    body_text: str
    thread_context: list[str]
    permalink: str | None = None


class GmailHistoryExpired(RuntimeError):
    pass


class GmailGateway(Protocol):
    async def list_message_ids(self, query: str, limit: int = 10) -> list[str]:
        pass

    async def current_history_id(self) -> str:
        pass

    async def list_new_message_ids(
        self, start_history_id: str, limit: int = 25
    ) -> tuple[list[str], str]:
        pass

    async def fetch_email(self, gmail_message_id: str) -> FetchedEmail:
        pass

    async def ensure_ai_labels(self) -> None:
        pass

    async def apply_labels(self, gmail_message_id: str, labels: Iterable[str]) -> None:
        pass


class GmailClient:
    def __init__(self, settings: Settings, credentials_json: str | None = None) -> None:
        self.settings = settings
        self.credentials_json = credentials_json
        self._service: Any | None = None

    def service(self) -> Any:
        if self._service is None:
            creds = self._credentials()
            self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return self._service

    def _credentials(self) -> Credentials:
        if self.credentials_json:
            creds = Credentials.from_authorized_user_info(
                json.loads(self.credentials_json), self.settings.google_oauth_scope_list
            )
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
            return creds
        token_file = Path(self.settings.google_token_file)
        creds = (
            Credentials.from_authorized_user_file(str(token_file), SCOPES)
            if token_file.exists()
            else None
        )
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(
                self.settings.google_client_secret_file, SCOPES
            )
            creds = flow.run_local_server(port=0)
            token_file.parent.mkdir(parents=True, exist_ok=True)
            token_file.write_text(creds.to_json(), encoding="utf-8")
        return creds

    async def list_message_ids(self, query: str, limit: int = 10) -> list[str]:
        response = (
            self.service().users().messages().list(userId="me", q=query, maxResults=limit).execute()
        )
        return [item["id"] for item in response.get("messages", [])]

    async def current_history_id(self) -> str:
        response = self.service().users().getProfile(userId="me").execute()
        return str(response["historyId"])

    async def list_new_message_ids(
        self, start_history_id: str, limit: int = 25
    ) -> tuple[list[str], str]:
        try:
            response = (
                self.service()
                .users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=start_history_id,
                    historyTypes=["messageAdded"],
                    maxResults=max(limit, 25),
                )
                .execute()
            )
        except HttpError as exc:
            if exc.resp.status == 404:
                raise GmailHistoryExpired from exc
            raise

        message_ids: list[str] = []
        seen: set[str] = set()
        history = response.get("history", [])
        for record in history:
            for item in record.get("messagesAdded", []):
                message_id = str(item["message"]["id"])
                if message_id not in seen:
                    seen.add(message_id)
                    message_ids.append(message_id)
        if history:
            next_history_id = str(history[-1]["id"])
        else:
            next_history_id = str(response.get("historyId") or start_history_id)
        return message_ids, next_history_id

    async def fetch_email(self, gmail_message_id: str) -> FetchedEmail:
        message = (
            self.service()
            .users()
            .messages()
            .get(userId="me", id=gmail_message_id, format="full")
            .execute()
        )
        thread = (
            self.service()
            .users()
            .threads()
            .get(userId="me", id=message["threadId"], format="full")
            .execute()
        )
        parsed = [_message_to_text(item) for item in thread.get("messages", [])]
        headers = _headers(message)
        return FetchedEmail(
            gmail_message_id=message["id"],
            gmail_thread_id=message["threadId"],
            sender=headers.get("from", ""),
            recipients=_split_recipients(headers.get("to", "")),
            subject=headers.get("subject", ""),
            received_at=_parse_date(headers.get("date")),
            body_text=parsed[-1] if parsed else "",
            thread_context=parsed[:-1][-self.settings.max_thread_messages :],
            permalink=f"https://mail.google.com/mail/u/0/#inbox/{message['id']}",
        )

    async def ensure_ai_labels(self) -> None:
        service = self.service()
        existing = service.users().labels().list(userId="me").execute().get("labels", [])
        names = {label["name"] for label in existing}
        for name in AI_LABELS:
            if name not in names:
                service.users().labels().create(
                    userId="me",
                    body={
                        "name": name,
                        "labelListVisibility": "labelShow",
                        "messageListVisibility": "show",
                    },
                ).execute()

    async def apply_labels(self, gmail_message_id: str, labels: Iterable[str]) -> None:
        service = self.service()
        existing = service.users().labels().list(userId="me").execute().get("labels", [])
        by_name = {label["name"]: label["id"] for label in existing}
        label_ids = [by_name[name] for name in labels if name in by_name]
        if label_ids:
            service.users().messages().modify(
                userId="me", id=gmail_message_id, body={"addLabelIds": label_ids}
            ).execute()


def _headers(message: dict[str, Any]) -> dict[str, str]:
    return {
        item["name"].lower(): item.get("value", "")
        for item in message["payload"].get("headers", [])
    }


def _message_to_text(message: dict[str, Any]) -> str:
    payload = message.get("payload", {})
    return _part_to_text(payload).strip()


def _part_to_text(part: dict[str, Any]) -> str:
    mime = part.get("mimeType", "")
    data = part.get("body", {}).get("data")
    if data and mime in {"text/plain", "text/html"}:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode(
            "utf-8", errors="ignore"
        )
        if mime == "text/html":
            return BeautifulSoup(raw, "html.parser").get_text("\n")
        return raw
    return "\n".join(_part_to_text(child) for child in part.get("parts", []))


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = parsedate_to_datetime(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _split_recipients(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]
