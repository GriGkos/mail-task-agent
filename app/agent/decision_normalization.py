from typing import Any

ALLOWED_CATEGORIES = {
    "work",
    "personal",
    "notification",
    "newsletter",
    "advertising",
    "unknown",
}


def normalize_model_decision(payload: Any) -> Any:
    """Normalize harmless model omissions before strict schema validation."""
    if not isinstance(payload, dict):
        return payload

    normalized = dict(payload)
    if normalized.get("category") not in ALLOWED_CATEGORIES:
        normalized["category"] = "unknown"
    if not isinstance(normalized.get("reason"), str) or not normalized["reason"].strip():
        normalized["reason"] = "The model did not provide a reason."
    return normalized
