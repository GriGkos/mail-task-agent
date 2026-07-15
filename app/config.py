from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    app_timezone: str = "Europe/Amsterdam"
    log_level: str = "INFO"
    app_base_url: str = "http://localhost:8000"

    database_url: str = "postgresql+asyncpg://postgres:postgres@postgres:5432/email_agent"
    token_encryption_key: str = ""

    mail_provider: str = "gmail"
    gmail_mode: str = "automatic"

    llm_provider: str = "deepseek"

    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_timeout_seconds: float = 60.0

    openmodel_api_key: str = ""
    openmodel_base_url: str = "https://api.openmodel.ai"
    openmodel_model: str = "deepseek-v4-flash"
    openmodel_timeout_seconds: float = 60.0
    openmodel_max_tokens: int = 2048

    google_client_secret_file: str = "/app/secrets/google_client_secret.json"
    google_token_file: str = "/app/secrets/google_token.json"
    google_oauth_scopes: str = "https://www.googleapis.com/auth/gmail.modify"
    gmail_query: str = "label:AI_TEST -label:AI/Processed"
    gmail_poll_interval_seconds: int = 120
    gmail_batch_size: int = 25

    microsoft_client_id: str = ""
    microsoft_client_secret: str = ""
    microsoft_tenant_id: str = "common"
    microsoft_token_cache_file: str = "/app/secrets/ms_token_cache.bin"
    microsoft_scopes: str = "User.Read Mail.ReadWrite offline_access"
    microsoft_graph_base_url: str = "https://graph.microsoft.com/v1.0"
    outlook_category: str = "AI_TEST"
    outlook_task_category: str = "AI_Task"
    outlook_waiting_category: str = "AI_Waiting"
    outlook_review_category: str = "AI_Review"
    outlook_info_category: str = "AI_Info"
    outlook_processed_category: str = "AI_Processed"
    outlook_error_category: str = "AI_Error"
    outlook_poll_interval_seconds: int = 120
    imap_poll_interval_seconds: int = 120
    imap_batch_size: int = 25
    imap_default_folder: str = "INBOX"

    telegram_bot_token: str = ""
    telegram_allowed_user_id: int | None = None
    telegram_api_ip: str = "149.154.167.220"

    admin_api_key: str = ""

    safe_mode: bool = True
    dry_run: bool = True
    store_email_body: bool = False

    auto_action_confidence: float = Field(default=0.90, ge=0, le=1)
    review_confidence: float = Field(default=0.70, ge=0, le=1)

    max_email_chars: int = 20_000
    max_thread_messages: int = 5

    daily_digest_enabled: bool = True
    daily_digest_time: str = "08:30"
    stale_task_days: int = 7

    @field_validator("app_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @field_validator("mail_provider")
    @classmethod
    def validate_mail_provider(cls, value: str) -> str:
        normalized = value.lower()
        if normalized not in {"gmail", "outlook", "imap"}:
            raise ValueError("MAIL_PROVIDER must be 'gmail', 'outlook', or 'imap'")
        return normalized

    @field_validator("llm_provider")
    @classmethod
    def validate_llm_provider(cls, value: str) -> str:
        normalized = value.lower()
        if normalized not in {"deepseek", "openmodel"}:
            raise ValueError("LLM_PROVIDER must be 'deepseek' or 'openmodel'")
        return normalized

    @field_validator("telegram_allowed_user_id", mode="before")
    @classmethod
    def empty_telegram_user_id_to_none(cls, value: object) -> object:
        if value == "":
            return None
        return value

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.app_timezone)

    @property
    def microsoft_scope_list(self) -> list[str]:
        return [scope for scope in self.microsoft_scopes.split() if scope]

    @property
    def google_oauth_scope_list(self) -> list[str]:
        return [scope for scope in self.google_oauth_scopes.split() if scope]


@lru_cache
def get_settings() -> Settings:
    return Settings()
