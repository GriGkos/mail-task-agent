from app.config import Settings
from app.integrations.deepseek import DeepSeekAnalyzer, EmailAnalyzer
from app.integrations.openmodel import OpenModelAnalyzer


def build_email_analyzer(settings: Settings) -> EmailAnalyzer:
    if settings.llm_provider == "openmodel":
        return OpenModelAnalyzer(settings)
    return DeepSeekAnalyzer(settings)
