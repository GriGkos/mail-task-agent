from app.config import Settings
from app.db.models import MailAccount
from app.integrations.gmail import GmailClient, GmailGateway
from app.integrations.imap import IMAPClient
from app.integrations.outlook import OutlookClient


def build_mail_gateway(
    settings: Settings, account: MailAccount | None = None, token_payload: str | None = None
) -> GmailGateway:
    provider = account.provider if account else settings.mail_provider
    if provider == "outlook":
        return OutlookClient(settings, token_cache_content=token_payload)
    if provider == "gmail":
        return GmailClient(settings, credentials_json=token_payload)
    if provider == "imap":
        account_id = account.id if account else "default"
        if not token_payload:
            raise ValueError("IMAP account credentials are missing")
        return IMAPClient(settings, token_payload=token_payload, account_id=account_id)
    raise ValueError(f"Unsupported mail provider: {provider}")
    return GmailClient(settings)
