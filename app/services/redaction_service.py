import hashlib
import re

from bs4 import BeautifulSoup

SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?[\w\-./+=]{8,}"),
    re.compile(r"\b(?:\d[ -]*?){13,19}\b"),
]


class RedactionService:
    def sanitize(self, text: str, max_chars: int) -> str:
        cleaned = (
            BeautifulSoup(text, "html.parser").get_text("\n")
            if "<" in text and ">" in text
            else text
        )
        cleaned = self.strip_quotes(cleaned)
        cleaned = self.mask_secrets(cleaned)
        return cleaned[:max_chars]

    def strip_quotes(self, text: str) -> str:
        lines = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(">") or re.match(r"(?i)^on .+ wrote:$", stripped):
                break
            if re.match(r"(?i)^from:\s|^sent:\s|^to:\s|^subject:\s", stripped):
                continue
            lines.append(line)
        return "\n".join(lines).strip()

    def mask_secrets(self, text: str) -> str:
        masked = text
        for pattern in SECRET_PATTERNS:
            masked = pattern.sub("[REDACTED_SECRET]", masked)
        return masked

    def hash_body(self, text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
