import json
from datetime import datetime
from typing import Protocol

from openai import AsyncOpenAI
from pydantic import ValidationError

from app.agent.decision_normalization import normalize_model_decision
from app.agent.prompts import SYSTEM_PROMPT, build_user_prompt
from app.agent.schemas import EmailDecision, EmailForAnalysis
from app.config import Settings


class EmailAnalyzer(Protocol):
    async def analyze(self, email: EmailForAnalysis) -> EmailDecision:
        pass


class DeepSeekAnalyzer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = AsyncOpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            timeout=settings.deepseek_timeout_seconds,
        )

    async def analyze(self, email: EmailForAnalysis) -> EmailDecision:
        if not self.settings.deepseek_api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is required")
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
            response = await self.client.chat.completions.create(
                model=self.settings.deepseek_model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content or "{}"
            try:
                data = normalize_model_decision(json.loads(content))
                return EmailDecision.model_validate(data)
            except (json.JSONDecodeError, ValidationError) as exc:
                last_error = str(exc)
                if attempt == 1:
                    raise
        raise RuntimeError("unreachable DeepSeek validation path")
