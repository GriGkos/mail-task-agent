import asyncio
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import msal
from bs4 import BeautifulSoup

from app.config import Settings
from app.integrations.gmail import FetchedEmail

AI_CATEGORY_LABELS = ["AI/Task", "AI/Waiting", "AI/Review", "AI/Info", "AI/Processed", "AI/Error"]


class OutlookDeltaExpired(RuntimeError):
    pass


class OutlookClient:
    def __init__(self, settings: Settings, token_cache_content: str | None = None) -> None:
        self.settings = settings
        self.token_cache_content = token_cache_content
        self._access_token: str | None = None

    async def list_message_ids(self, query: str, limit: int = 10) -> list[str]:
        categories_filter = (
            f"categories/any(c:c eq '{_odata_string(self.settings.outlook_category)}') "
            f"and not categories/any(c:c eq "
            f"'{_odata_string(self.settings.outlook_processed_category)}')"
        )
        response = await self._request(
            "GET",
            "/me/messages",
            params={
                "$top": str(limit),
                "$select": "id",
                "$orderby": "receivedDateTime desc",
                "$filter": categories_filter,
            },
        )
        return [item["id"] for item in response.get("value", [])]

    async def initialize_delta(self) -> str:
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        response = await self._request(
            "GET",
            "/me/mailFolders/inbox/messages/delta",
            params={
                "$select": "id",
                "$filter": f"receivedDateTime ge {now}",
                "$orderby": "receivedDateTime desc",
                "$top": str(self.settings.gmail_batch_size),
                "changeType": "created",
            },
        )
        while response.get("@odata.nextLink"):
            response = await self._request_url("GET", response["@odata.nextLink"])
        delta_link = response.get("@odata.deltaLink")
        if not delta_link:
            raise RuntimeError("Microsoft Graph did not return an Outlook delta link")
        return str(delta_link)

    async def list_new_message_ids(self, delta_link: str, limit: int = 25) -> tuple[list[str], str]:
        try:
            response = await self._request_url("GET", delta_link)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {400, 410}:
                raise OutlookDeltaExpired from exc
            raise

        message_ids: list[str] = []
        for item in response.get("value", []):
            if item.get("@removed") or not item.get("id"):
                continue
            message_ids.append(str(item["id"]))
            if len(message_ids) >= limit:
                break

        next_link = response.get("@odata.nextLink") or response.get("@odata.deltaLink")
        if not next_link:
            raise RuntimeError("Microsoft Graph did not return the next Outlook delta link")
        return message_ids, str(next_link)

    async def fetch_email(self, gmail_message_id: str) -> FetchedEmail:
        message = await self._request(
            "GET",
            f"/me/messages/{gmail_message_id}",
            params={
                "$select": (
                    "id,conversationId,subject,from,toRecipients,receivedDateTime,body,webLink"
                )
            },
        )
        conversation_id = message.get("conversationId") or message["id"]
        thread_messages = await self._conversation_messages(conversation_id)
        texts = [_message_body_text(item) for item in thread_messages]
        body_text = _message_body_text(message)
        if body_text and (not texts or texts[-1] != body_text):
            texts.append(body_text)
        return FetchedEmail(
            gmail_message_id=message["id"],
            gmail_thread_id=conversation_id,
            sender=_email_address(message.get("from")),
            recipients=[_email_address(item) for item in message.get("toRecipients", [])],
            subject=message.get("subject") or "",
            received_at=_parse_graph_datetime(message.get("receivedDateTime")),
            body_text=body_text,
            thread_context=texts[:-1][-self.settings.max_thread_messages :],
            permalink=message.get("webLink"),
        )

    async def ensure_ai_labels(self) -> None:
        existing = await self._request("GET", "/me/outlook/masterCategories")
        names = {item["displayName"] for item in existing.get("value", [])}
        wanted = {self.settings.outlook_category}
        wanted.update(self._category_for_label(label) for label in AI_CATEGORY_LABELS)
        for name in sorted(wanted):
            if name in names:
                continue
            await self._request(
                "POST",
                "/me/outlook/masterCategories",
                json={"displayName": name, "color": "preset0"},
            )

    async def apply_labels(self, gmail_message_id: str, labels: Iterable[str]) -> None:
        message = await self._request(
            "GET", f"/me/messages/{gmail_message_id}", params={"$select": "categories"}
        )
        categories = set(message.get("categories") or [])
        categories.update(self._category_for_label(label) for label in labels)
        await self._request(
            "PATCH", f"/me/messages/{gmail_message_id}", json={"categories": sorted(categories)}
        )

    async def _conversation_messages(self, conversation_id: str) -> list[dict[str, Any]]:
        response = await self._request(
            "GET",
            "/me/messages",
            params={
                "$top": str(self.settings.max_thread_messages),
                "$select": "id,body,receivedDateTime",
                "$orderby": "receivedDateTime asc",
                "$filter": f"conversationId eq '{_odata_string(conversation_id)}'",
            },
        )
        return list(response.get("value", []))

    async def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        url = f"{self.settings.microsoft_graph_base_url.rstrip('/')}/{path.lstrip('/')}"
        return await self._request_url(method, url, **kwargs)

    async def _request_url(self, method: str, url: str, **kwargs) -> dict[str, Any]:
        token = await self._get_access_token()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {token}"
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.request(method, url, headers=headers, **kwargs)
            response.raise_for_status()
            if response.status_code == 204:
                return {}
            return response.json()

    async def _get_access_token(self) -> str:
        if self._access_token:
            return self._access_token
        self._access_token = await asyncio.to_thread(self._get_access_token_sync)
        return self._access_token

    def _get_access_token_sync(self) -> str:
        if not self.settings.microsoft_client_id or not self.settings.microsoft_client_secret:
            raise RuntimeError(
                "MICROSOFT_CLIENT_ID and MICROSOFT_CLIENT_SECRET are required for Outlook"
            )
        cache = msal.SerializableTokenCache()
        cache_path = Path(self.settings.microsoft_token_cache_file)
        if self.token_cache_content:
            cache.deserialize(self.token_cache_content)
        elif cache_path.exists():
            cache.deserialize(cache_path.read_text(encoding="utf-8"))
        app = msal.ConfidentialClientApplication(
            self.settings.microsoft_client_id,
            client_credential=self.settings.microsoft_client_secret,
            authority=f"https://login.microsoftonline.com/{self.settings.microsoft_tenant_id}",
            token_cache=cache,
        )
        result: dict[str, Any] | None = None
        accounts = app.get_accounts()
        if accounts:
            result = app.acquire_token_silent(
                self.settings.microsoft_scope_list, account=accounts[0]
            )
        if not result:
            raise RuntimeError("Outlook token expired or is invalid; reconnect Outlook in Telegram")
        if cache.has_state_changed and not self.token_cache_content:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(cache.serialize(), encoding="utf-8")
        if not result or "access_token" not in result:
            raise RuntimeError(f"Microsoft auth failed: {result}")
        return str(result["access_token"])

    def _category_for_label(self, label: str) -> str:
        mapping = {
            "AI/Task": self.settings.outlook_task_category,
            "AI/Waiting": self.settings.outlook_waiting_category,
            "AI/Review": self.settings.outlook_review_category,
            "AI/Info": self.settings.outlook_info_category,
            "AI/Processed": self.settings.outlook_processed_category,
            "AI/Error": self.settings.outlook_error_category,
        }
        return mapping.get(label, label.replace("/", "_"))


def _message_body_text(message: dict[str, Any]) -> str:
    body = message.get("body") or {}
    content = body.get("content") or ""
    if body.get("contentType", "").lower() == "html":
        return BeautifulSoup(content, "html.parser").get_text("\n").strip()
    return content.strip()


def _email_address(value: dict[str, Any] | None) -> str:
    if not value:
        return ""
    email = value.get("emailAddress") or {}
    name = email.get("name")
    address = email.get("address", "")
    return f"{name} <{address}>" if name and address else address


def _parse_graph_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _odata_string(value: str) -> str:
    return value.replace("'", "''")
