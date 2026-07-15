import pytest

from app.integrations.gmail import GmailClient


class FakeHistoryRequest:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.kwargs = None

    def list(self, **kwargs):
        self.kwargs = kwargs
        return self

    def execute(self):
        return self.response


class FakeUsers:
    def __init__(self, request: FakeHistoryRequest) -> None:
        self.request = request

    def history(self):
        return self.request


class FakeService:
    def __init__(self, response: dict) -> None:
        self.request = FakeHistoryRequest(response)
        self.users_api = FakeUsers(self.request)

    def users(self):
        return self.users_api


@pytest.mark.asyncio
async def test_history_events_are_unique_and_return_a_cursor(settings):
    client = GmailClient(settings)
    service = FakeService(
        {
            "historyId": "105",
            "history": [
                {"id": "101", "messagesAdded": [{"message": {"id": "msg-1"}}]},
                {
                    "id": "102",
                    "messagesAdded": [
                        {"message": {"id": "msg-1"}},
                        {"message": {"id": "msg-2"}},
                    ],
                },
            ],
        }
    )
    client._service = service

    message_ids, cursor = await client.list_new_message_ids("100", limit=25)

    assert message_ids == ["msg-1", "msg-2"]
    assert cursor == "102"
    assert service.request.kwargs["startHistoryId"] == "100"
    assert service.request.kwargs["historyTypes"] == ["messageAdded"]
