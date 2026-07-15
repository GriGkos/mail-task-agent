from app.agent.routing import needs_approval
from tests.conftest import decision


def test_safe_mode_requires_approval(settings):
    settings.safe_mode = True

    assert needs_approval(decision(confidence=0.99), settings) is True
