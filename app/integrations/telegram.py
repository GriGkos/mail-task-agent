from typing import Any, Protocol

import httpcore
import httpx

from app.config import Settings


class _PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, ip: str) -> None:
        self.ip = ip
        self.backend = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109
        local_address: str | None = None,
        socket_options=None,
    ):
        return await self.backend.connect_tcp(
            self.ip,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def sleep(self, seconds: float) -> None:
        await self.backend.sleep(seconds)


class _TelegramTransport(httpx.AsyncHTTPTransport):
    def __init__(self, ip: str) -> None:
        super().__init__(local_address="0.0.0.0", retries=3)
        self._pool._network_backend = _PinnedNetworkBackend(ip)


class TelegramGateway(Protocol):
    async def send_approval(
        self, approval_id: str, text: str, email_url: str | None = None
    ) -> str | None:
        pass

    async def send_message(self, text: str) -> str | None:
        pass


class TelegramClient:
    def __init__(self, settings: Settings, chat_id: str | int | None = None) -> None:
        self.settings = settings
        self.chat_id = chat_id if chat_id is not None else settings.telegram_allowed_user_id
        self.base_url = f"https://api.telegram.org/bot{settings.telegram_bot_token}"

    async def send_approval(
        self, approval_id: str, text: str, email_url: str | None = None
    ) -> str | None:
        keyboard: list[list[dict[str, Any]]] = [
            [
                {"text": "Подтвердить", "callback_data": f"approve:{approval_id}"},
                {"text": "Изменить", "callback_data": f"edit:{approval_id}"},
                {"text": "Отклонить", "callback_data": f"reject:{approval_id}"},
            ]
        ]
        if email_url:
            keyboard.append([{"text": "Открыть письмо", "url": email_url}])
        return await self._post_message(text, {"inline_keyboard": keyboard})

    async def send_message(self, text: str) -> str | None:
        return await self._post_message(text, None)

    async def send_menu(self, text: str, keyboard: list[list[dict[str, Any]]]) -> str | None:
        return await self._post_message(text, {"inline_keyboard": keyboard})

    async def edit_message(
        self,
        message_id: str | int,
        text: str,
        keyboard: list[list[dict[str, Any]]] | None = None,
    ) -> None:
        if not self.settings.telegram_bot_token or self.chat_id is None:
            return
        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if keyboard is not None:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        async with self._client() as client:
            response = await client.post(
                f"{self.base_url}/editMessageText",
                json=payload,
            )
            if response.status_code == 400:
                return
            response.raise_for_status()

    async def answer_callback(self, callback_query_id: str, text: str | None = None) -> None:
        if not self.settings.telegram_bot_token:
            return
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        async with self._client() as client:
            response = await client.post(f"{self.base_url}/answerCallbackQuery", json=payload)
            if response.status_code == 400:
                return
            response.raise_for_status()

    async def _post_message(self, text: str, reply_markup: dict[str, Any] | None) -> str | None:
        if not self.settings.telegram_bot_token or self.chat_id is None:
            return None
        async with self._client() as client:
            payload: dict[str, Any] = {
                "chat_id": self.chat_id,
                "text": text,
                "disable_web_page_preview": True,
            }
            if reply_markup is not None:
                payload["reply_markup"] = reply_markup
            response = await client.post(
                f"{self.base_url}/sendMessage",
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            return str(data.get("result", {}).get("message_id"))

    async def set_commands(self, commands: list[dict[str, str]]) -> None:
        if not self.settings.telegram_bot_token:
            return
        async with self._client() as client:
            response = await client.post(
                f"{self.base_url}/setMyCommands", json={"commands": commands}
            )
            response.raise_for_status()

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(25, connect=10),
            trust_env=False,
            transport=_TelegramTransport(self.settings.telegram_api_ip),
        )
