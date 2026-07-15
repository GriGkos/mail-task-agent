from __future__ import annotations

import html
import json
import time
from datetime import datetime
from typing import Any

from app.config import Settings
from app.services.token_service import TokenCipher

SOURCE_LINK_TTL_SECONDS = 7 * 24 * 60 * 60


def create_source_email_token(settings: Settings, task_id: str, user_id: str) -> str:
    payload = {
        "task_id": task_id,
        "user_id": user_id,
        "expires_at": int(time.time()) + SOURCE_LINK_TTL_SECONDS,
    }
    return TokenCipher(settings).encrypt(json.dumps(payload, separators=(",", ":")))


def read_source_email_token(settings: Settings, token: str) -> dict[str, Any]:
    try:
        payload = json.loads(TokenCipher(settings).decrypt(token))
        if int(payload["expires_at"]) < int(time.time()):
            raise ValueError("source email link expired")
        if not payload.get("task_id") or not payload.get("user_id"):
            raise ValueError("source email link is incomplete")
        return payload
    except (KeyError, TypeError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid source email link") from exc


def source_email_html(
    *,
    sender: str,
    recipients: list[str],
    subject: str,
    received_at: datetime | None,
    body: str,
    truncated: bool = False,
) -> str:
    safe_body = html.escape(body).replace("\n", "<br>")
    if truncated:
        safe_body += "<br><br><em>Показана сокращённая версия письма.</em>"
    received = received_at.strftime("%d.%m.%Y %H:%M") if received_at else "-"
    recipients_text = ", ".join(recipients) or "-"
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(subject or "Письмо")}</title>
  <style>
    :root {{ color-scheme: light dark; }}
    body {{ font-family: system-ui, sans-serif; margin: 0; background: #101820; color: #eef3f7; }}
    main {{ max-width: 760px; margin: 24px auto; padding: 20px; }}
    article {{ background: #182735; border-radius: 10px; padding: 22px; }}
    h1 {{ margin-top: 0; font-size: 24px; overflow-wrap: anywhere; }}
    .meta {{ color: #aebdca; line-height: 1.6; overflow-wrap: anywhere; }}
    .body {{ margin-top: 22px; line-height: 1.6; overflow-wrap: anywhere; }}
  </style>
</head>
<body>
<main><article>
  <h1>{html.escape(subject or "Без темы")}</h1>
  <div class="meta">От: {html.escape(sender or "-")}<br>
  Кому: {html.escape(recipients_text)}<br>
  Получено: {received}</div>
  <div class="body">{safe_body or "Письмо не содержит текста."}</div>
</article></main>
</body>
</html>"""
