from app.services.source_email_service import (
    create_source_email_token,
    read_source_email_token,
    source_email_html,
)


def test_source_email_token_round_trip(settings):
    token = create_source_email_token(settings, "task-1", "user-1")

    payload = read_source_email_token(settings, token)

    assert payload["task_id"] == "task-1"
    assert payload["user_id"] == "user-1"


def test_source_email_html_escapes_message_content():
    html = source_email_html(
        sender="sender@example.com",
        recipients=["user@example.com"],
        subject="<Subject>",
        received_at=None,
        body="<script>alert('x')</script>",
    )

    assert "&lt;Subject&gt;" in html
    assert "&lt;script&gt;" in html
    assert "<script>alert" not in html
