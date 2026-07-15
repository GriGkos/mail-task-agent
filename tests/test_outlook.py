from datetime import UTC, datetime

import pytest

from app.db.models import MailAccount
from app.integrations.mail import build_mail_gateway
from app.integrations.outlook import (
    OutlookClient,
    _message_body_text,
    _parse_graph_datetime,
)
from app.workers.gmail_worker import _process_account


class FakeOutlookClient(OutlookClient):
    def __init__(self, settings) -> None:
        super().__init__(settings)
        self.calls = []
        self.message_categories = ["Existing"]

    async def _get_access_token(self) -> str:
        return "token"

    async def _request(self, method: str, path: str, **kwargs):
        self.calls.append((method, path, kwargs))
        if method == "GET" and path == "/me/messages" and "$filter" in kwargs.get("params", {}):
            return {"value": [{"id": "outlook-msg-1"}]}
        if method == "GET" and path.endswith("/outlook-msg-1"):
            return {"categories": self.message_categories}
        if method == "PATCH":
            self.message_categories = kwargs["json"]["categories"]
            return {}
        if path == "/me/outlook/masterCategories":
            return {"value": []}
        return {"value": []}


def test_mail_factory_selects_outlook(settings):
    settings.mail_provider = "outlook"

    assert isinstance(build_mail_gateway(settings), OutlookClient)


@pytest.mark.asyncio
async def test_outlook_lists_unprocessed_category(settings):
    client = FakeOutlookClient(settings)
    ids = await client.list_message_ids("", limit=3)

    assert ids == ["outlook-msg-1"]
    params = client.calls[0][2]["params"]
    assert params["$top"] == "3"
    assert "AI_TEST" in params["$filter"]
    assert "AI_Processed" in params["$filter"]


@pytest.mark.asyncio
async def test_outlook_apply_labels_maps_gmail_labels_to_categories(settings):
    client = FakeOutlookClient(settings)
    await client.apply_labels("outlook-msg-1", ["AI/Task", "AI/Processed"])

    assert "Existing" in client.message_categories
    assert "AI_Task" in client.message_categories
    assert "AI_Processed" in client.message_categories


def test_outlook_html_body_is_converted_to_text():
    text = _message_body_text({"body": {"contentType": "html", "content": "<p>Hello<br>World</p>"}})

    assert "Hello" in text
    assert "World" in text
    assert "<p>" not in text


def test_outlook_datetime_parser_uses_utc():
    parsed = _parse_graph_datetime("2026-06-29T08:30:00Z")

    assert parsed == datetime(2026, 6, 29, 8, 30, tzinfo=UTC)


@pytest.mark.asyncio
async def test_outlook_initial_delta_baseline_does_not_process_existing_messages(settings):
    class DeltaClient(OutlookClient):
        def __init__(self, settings) -> None:
            super().__init__(settings)
            self.calls = []

        async def _get_access_token(self) -> str:
            return "token"

        async def _request(self, method: str, path: str, **kwargs):
            self.calls.append((method, path, kwargs))
            return {"@odata.deltaLink": "https://graph.microsoft.com/v1.0/delta?token=1"}

    client = DeltaClient(settings)
    link = await client.initialize_delta()

    assert link.endswith("token=1")
    assert client.calls[0][1] == "/me/mailFolders/inbox/messages/delta"
    assert client.calls[0][2]["params"]["changeType"] == "created"
    assert client.calls[0][2]["params"]["$top"] == str(settings.gmail_batch_size)


@pytest.mark.asyncio
async def test_outlook_delta_returns_new_ids_and_next_cursor(settings):
    class DeltaClient(OutlookClient):
        async def _get_access_token(self) -> str:
            return "token"

        async def _request_url(self, method: str, url: str, **kwargs):
            assert method == "GET"
            assert url.endswith("token=1")
            return {
                "value": [
                    {"id": "outlook-msg-1"},
                    {"@removed": {"reason": "deleted"}, "id": "old-msg"},
                ],
                "@odata.deltaLink": "https://graph.microsoft.com/v1.0/delta?token=2",
            }

    ids, cursor = await DeltaClient(settings).list_new_message_ids(
        "https://graph.microsoft.com/v1.0/delta?token=1"
    )

    assert ids == ["outlook-msg-1"]
    assert cursor.endswith("token=2")


@pytest.mark.asyncio
async def test_outlook_worker_baselines_without_scanning_old_mail(session, settings):
    class DeltaClient:
        ensure_called = False

        async def ensure_ai_labels(self) -> None:
            self.ensure_called = True

        async def initialize_delta(self) -> str:
            return "delta-token"

    account = MailAccount(
        user_id=None,
        provider="outlook",
        email_address="person@example.com",
        encrypted_token="encrypted",
        scopes=[],
    )
    client = DeltaClient()
    await _process_account(settings, session, client, account, None, None, 25)

    assert client.ensure_called is True
    assert account.outlook_delta_link == "delta-token"
    assert account.last_poll_at is not None
