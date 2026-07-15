import json
from datetime import datetime

from anthropic import AsyncAnthropic
from pydantic import ValidationError

from app.agent.decision_normalization import normalize_model_decision
from app.agent.prompts import SYSTEM_PROMPT, build_user_prompt
from app.agent.schemas import EmailDecision, EmailForAnalysis
from app.config import Settings


class OpenModelAnalyzer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = AsyncAnthropic(
            api_key=settings.openmodel_api_key,
            base_url=settings.openmodel_base_url,
            timeout=settings.openmodel_timeout_seconds,
        )

    async def analyze(self, email: EmailForAnalysis) -> EmailDecision:
        if not self.settings.openmodel_api_key:
            raise RuntimeError("OPENMODEL_API_KEY is required")
        payload = email.model_dump_json()
        now = datetime.now(self.settings.timezone)
        last_error: str | None = None
        for attempt in range(2):
            prompt = build_user_prompt(now, self.settings.app_timezone, payload)
            if last_error:
                prompt += (
                    f"\nPrevious response failed validation: {last_error}\n"
                    "Return corrected JSON only."
                )
            response = await self.client.messages.create(
                model=self.settings.openmodel_model,
                max_tokens=self.settings.openmodel_max_tokens,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            content = _extract_text(response)
            try:
                data = normalize_model_decision(json.loads(content))
                return EmailDecision.model_validate(data)
            except (json.JSONDecodeError, ValidationError) as exc:
                last_error = str(exc)
                if attempt == 1:
                    raise
        raise RuntimeError("unreachable OpenModel validation path")


def _extract_text(response) -> str:
    parts: list[str] = []
    for block in getattr(response, "content", []):
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "\n".join(parts).strip() or "{}"
