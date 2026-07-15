from cryptography.fernet import Fernet, InvalidToken

from app.config import Settings


class TokenCipher:
    def __init__(self, settings: Settings) -> None:
        if not settings.token_encryption_key:
            raise RuntimeError("TOKEN_ENCRYPTION_KEY is required to store OAuth tokens")
        self.fernet = Fernet(settings.token_encryption_key.encode("utf-8"))

    def encrypt(self, plaintext: str) -> str:
        return self.fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self.fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:
            raise RuntimeError("stored OAuth token cannot be decrypted") from exc
