from __future__ import annotations

import html
import re
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.repositories import MailAccountRepository, MailSetupSessionRepository, UserRepository
from app.integrations.imap import (
    IMAPAccountConfig,
    IMAPAuthenticationError,
    IMAPClient,
    known_imap_preset,
)
from app.integrations.telegram import TelegramClient
from app.services.token_service import TokenCipher


class IMAPSetupService:
    def __init__(self, settings: Settings, session: AsyncSession) -> None:
        self.settings = settings
        self.session = session
        self.users = UserRepository(session)
        self.accounts = MailAccountRepository(session)
        self.setup_sessions = MailSetupSessionRepository(session)
        self.cipher = TokenCipher(settings)

    async def create_setup_session(self, user_id: str) -> str:
        setup = await self.setup_sessions.create(
            user_id=user_id,
            provider="imap",
            data={},
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )
        await self.session.commit()
        return setup.id

    async def connect(
        self,
        setup_id: str,
        email_address: str,
        host: str,
        port: int | None,
        username: str,
        password: str,
        folder: str,
        security: str,
    ) -> str:
        setup = await self.setup_sessions.get_valid(setup_id, "imap")
        user = await self.users.get_user(setup.user_id)
        if user is None:
            raise LookupError("user not found")
        email_address = email_address.strip()
        host = host.strip()
        username = username.strip()
        folder = folder.strip() or self.settings.imap_default_folder
        username = username or email_address
        preset = known_imap_preset(email_address)
        if preset:
            preset_host, preset_port, preset_security = preset
            host = host or preset_host
            port = port or preset_port
            security = security if security != "auto" else preset_security
        else:
            port = port or 993
            security = "ssl" if security == "auto" else security
        if not _looks_like_email(email_address):
            raise ValueError("Укажи корректный адрес электронной почты.")
        if not host or not username or not password:
            raise ValueError("Нужно заполнить сервер, логин и пароль приложения.")
        if not host:
            raise ValueError("Не удалось определить IMAP-сервер. Открой дополнительные настройки.")
        if not 1 <= port <= 65535:
            raise ValueError("Порт должен быть от 1 до 65535.")
        if security not in {"ssl", "starttls", "none"}:
            raise ValueError("Выбери корректный способ защиты соединения.")

        config = IMAPAccountConfig(
            host=host,
            port=port,
            username=username,
            password=password,
            folder=folder,
            security=security,
        )
        client = IMAPClient(self.settings, config.to_json(), account_id="setup")
        try:
            await client.verify()
        except IMAPAuthenticationError as exc:
            raise ValueError(
                "Не удалось войти в почту. Используй пароль приложения и проверь IMAP-настройки."
            ) from exc
        except (OSError, TimeoutError) as exc:
            raise ValueError("Не удалось подключиться к IMAP-серверу.") from exc

        encrypted = self.cipher.encrypt(config.to_json())
        await self.accounts.upsert(
            user_id=user.id,
            provider="imap",
            email_address=email_address,
            encrypted_token=encrypted,
            scopes=["imap", "ssl"],
            imap_uidvalidity=None,
            imap_last_uid=None,
        )
        await self.setup_sessions.delete(setup)
        await self.session.commit()
        identity = await self.users.get_telegram_identity_by_user_id(user.id)
        if identity:
            try:
                await TelegramClient(self.settings, chat_id=identity.chat_id).send_message(
                    f"IMAP-почта подключена: {email_address}\n"
                    "Старые письма не обрабатываются. Бот будет проверять только новые."
                )
            except Exception:
                # A Telegram notification failure must not undo a successful mailbox connection.
                pass
        return email_address


def imap_onboarding_html(
    setup_id: str,
    app_base_url: str,
    error: str | None = None,
) -> str:
    base = app_base_url.rstrip("/")
    error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Подключение почты</title>
  <style>
    :root {{ color-scheme: light dark; }}
    body {{ font-family: system-ui, sans-serif; margin: 0; background: #101820; color: #eef3f7; }}
    main {{ max-width: 560px; margin: 32px auto; padding: 24px; }}
    form {{ display: grid; gap: 14px; background: #182735; padding: 22px; border-radius: 12px; }}
    label {{ display: grid; gap: 6px; font-size: 14px; }}
    input, select {{ box-sizing: border-box; width: 100%; padding: 11px 12px;
      border: 1px solid #536575;
      border-radius: 7px; background: #0f1a24; color: inherit; font: inherit; }}
    button {{ padding: 12px; border: 0; border-radius: 7px; background: #baf36b; color: #101820;
      font-weight: 700; cursor: pointer; }}
    .muted {{ color: #aebdca; line-height: 1.5; }}
    .error {{ padding: 10px 12px; border-radius: 7px; background: #5b2630; color: #ffdce1; }}
    .warning {{ padding: 10px 12px; border-left: 3px solid #f0b35a; color: #f7d7a5; }}
  </style>
</head>
<body>
<main>
  <h1>Подключение почты</h1>
  <p class="muted">Данные передаются по HTTPS и сохраняются на сервере в зашифрованном виде.</p>
  {error_html}
  <form method="post" action="{base}/onboarding/imap/{html.escape(setup_id)}">
    <label>Адрес почты
      <input id="email" name="email_address" type="email" autocomplete="username"
        oninput="detectProvider()" required>
    </label>
    <p id="detected" class="muted">Введите адрес, и настройки сервера подставятся автоматически.</p>
    <label>Пароль приложения
      <input name="password" type="password" autocomplete="current-password" required>
    </label>
    <p id="password-hint" class="muted">Введите пароль приложения, а не обычный пароль от почты.</p>
    <details id="advanced">
      <summary>Дополнительные настройки</summary>
      <label>IMAP-сервер
        <input id="host" name="host" placeholder="imap.example.com">
      </label>
      <label>Порт
        <input id="port" name="port" type="number" value="993" min="1" max="65535">
      </label>
      <label>Логин, если отличается от адреса
        <input id="username" name="username" autocomplete="username">
      </label>
      <label>Папка
        <input name="folder" value="INBOX">
      </label>
      <label>Защита соединения
        <select id="security" name="security">
          <option value="auto" selected>Автоматически</option>
          <option value="ssl">SSL/TLS</option>
          <option value="starttls">STARTTLS</option>
          <option value="none">Без шифрования</option>
        </select>
      </label>
    </details>
    <button type="submit">Проверить и подключить</button>
  </form>
</main>
<script>
const presets = {{
  "gmail.com": ["imap.gmail.com", 993, "ssl", "Gmail"],
  "googlemail.com": ["imap.gmail.com", 993, "ssl", "Gmail"],
  "outlook.com": ["imap-mail.outlook.com", 993, "ssl", "Outlook.com"],
  "hotmail.com": ["imap-mail.outlook.com", 993, "ssl", "Outlook.com"],
  "live.com": ["imap-mail.outlook.com", 993, "ssl", "Outlook.com"],
  "yandex.ru": ["imap.yandex.ru", 993, "ssl", "Яндекс"],
  "ya.ru": ["imap.yandex.ru", 993, "ssl", "Яндекс"],
  "mail.ru": ["imap.mail.ru", 993, "ssl", "Mail.ru"],
  "inbox.ru": ["imap.mail.ru", 993, "ssl", "Mail.ru"],
  "list.ru": ["imap.mail.ru", 993, "ssl", "Mail.ru"],
  "bk.ru": ["imap.mail.ru", 993, "ssl", "Mail.ru"],
  "yahoo.com": ["imap.mail.yahoo.com", 993, "ssl", "Yahoo"],
  "icloud.com": ["imap.mail.me.com", 993, "ssl", "iCloud"],
  "me.com": ["imap.mail.me.com", 993, "ssl", "iCloud"]
}};
const providerHints = {{
  "gmail.com": "В Gmail нужен пароль приложения после включения двухэтапной аутентификации.",
  "googlemail.com": "В Gmail нужен пароль приложения после включения двухэтапной аутентификации.",
  "outlook.com": "Для Outlook.com используйте пароль приложения, "
    + "если включена двухэтапная проверка.",
  "hotmail.com": "Для Outlook.com используйте пароль приложения, "
    + "если включена двухэтапная проверка.",
  "live.com": "Для Outlook.com используйте пароль приложения, "
    + "если включена двухэтапная проверка.",
  "yandex.ru": "В Яндекс.Почте включите IMAP и создайте пароль приложения в настройках аккаунта.",
  "ya.ru": "В Яндекс.Почте включите IMAP и создайте пароль приложения в настройках аккаунта.",
  "mail.ru": "В Почте Mail создайте пароль для внешнего приложения в настройках безопасности.",
  "inbox.ru": "В Почте Mail создайте пароль для внешнего приложения в настройках безопасности.",
  "list.ru": "В Почте Mail создайте пароль для внешнего приложения в настройках безопасности.",
  "bk.ru": "В Почте Mail создайте пароль для внешнего приложения в настройках безопасности.",
  "yahoo.com": "Для Yahoo нужен пароль приложения.",
  "icloud.com": "Для iCloud нужен пароль приложения.",
  "me.com": "Для iCloud нужен пароль приложения."
}};
let customUsername = false;
document.getElementById("username").addEventListener("input", () => customUsername = true);
function detectProvider() {{
  const email = document.getElementById("email").value.trim().toLowerCase();
  const domain = email.split("@").pop();
  const preset = presets[domain];
  const advanced = document.getElementById("advanced");
  const detected = document.getElementById("detected");
  const passwordHint = document.getElementById("password-hint");
  const username = document.getElementById("username");
  username.value = customUsername ? username.value : email;
  if (!preset) {{
    detected.textContent = "Сервис не определён. Откройте дополнительные настройки "
      + "и укажите IMAP-сервер.";
    passwordHint.textContent = "Введите пароль приложения, а не обычный пароль от почты.";
    advanced.open = true;
    return;
  }}
  document.getElementById("host").value = preset[0];
  document.getElementById("port").value = preset[1];
  document.getElementById("security").value = preset[2];
  detected.textContent = `Сервис: ${{preset[3]}}. Сервер настроен автоматически.`;
  passwordHint.textContent = providerHints[domain]
    || "Введите пароль приложения, а не обычный пароль от почты.";
  advanced.open = false;
}}
</script>
</body>
</html>"""


def _looks_like_email(value: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value))
