from types import SimpleNamespace

import pytest

from app.integrations.telegram import TelegramClient
from app.workers.telegram_bot import _menu, _task_keyboard, _task_text


class FakeResponse:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {"result": {"message_id": 7}}


class FakeHttpClient:
    def __init__(self):
        self.payload = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, json):
        self.payload = json
        return FakeResponse()


@pytest.mark.asyncio
async def test_send_message_does_not_send_null_reply_markup(monkeypatch, settings):
    fake = FakeHttpClient()
    monkeypatch.setattr(
        "app.integrations.telegram.httpx.AsyncClient",
        lambda **kwargs: fake,
    )

    result = await TelegramClient(settings, chat_id=42).send_message("hello")

    assert result == "7"
    assert fake.payload == {
        "chat_id": 42,
        "text": "hello",
        "disable_web_page_preview": True,
    }


def test_task_menu_and_card_actions_are_user_facing():
    buttons = [button["text"] for row in _menu() for button in row]
    assert "Привязать почту" in buttons
    assert "Универсальная почта" not in buttons
    assert "Выполненные" in buttons

    task = SimpleNamespace(
        id="task-1",
        title="Позвонить поставщику",
        status="inbox",
        priority="high",
        description="Обсудить сроки поставки.",
        project="Закупки",
        due_at=None,
        assignee=None,
        waiting_for=None,
        next_action="Позвонить сегодня",
        requires_reply=True,
    )
    keyboard = _task_keyboard(task)
    labels = [button["text"] for row in keyboard for button in row]
    assert labels == ["Выполнено", "Отменить"]
    task_text = _task_text(task)
    assert task_text.startswith("Позвонить поставщику")
    assert "Описание" in task_text
    assert "Обсудить сроки поставки." in task_text

    source = SimpleNamespace(
        sender="supplier@example.com",
        subject="Delivery date",
        received_at=None,
    )
    source_text = _task_text(task, source)
    assert "Письмо" in source_text
    assert "supplier@example.com" in source_text
    assert "Открыть письмо" in [
        button["text"]
        for row in _task_keyboard(task, "https://mail.example/source")
        for button in row
    ]


def test_task_text_removes_repeated_title_from_description():
    task = SimpleNamespace(
        id="task-2",
        title="Организовать встречу с клиентом",
        status="inbox",
        priority="medium",
        description="Организовать встречу с клиентом. Желательно до пятницы.",
        project=None,
        due_at=None,
        assignee=None,
        waiting_for=None,
        next_action=None,
        requires_reply=False,
    )

    text = _task_text(task)

    assert "Дополнение" in text
    assert "Организовать встречу с клиентом. Желательно до пятницы." not in text
    assert "Желательно до пятницы." in text
