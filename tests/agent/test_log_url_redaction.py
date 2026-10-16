"""Diagnostic logs scrub round-trip credentials without changing tool inputs."""

import logging

import pytest

from agent.redact import RedactingFormatter, redact_sensitive_text


@pytest.mark.parametrize(
    "url, secret",
    [
        ("https://example.test/callback?code=opaque-code-fixture", "opaque-code-fixture"),
        ("https://example.test/link?token=opaque-link-fixture", "opaque-link-fixture"),
        ("https://example.test/file?X-Amz-Signature=opaque-signature-fixture", "opaque-signature-fixture"),
        ("https://fixture-user:opaque-password-fixture@example.test/path", "opaque-password-fixture"),
        ("https://example.test/callback#access_token=opaque-fragment-fixture", "opaque-fragment-fixture"),
    ],
)
def test_strict_log_copy_masks_url_credentials_without_mutating_record(
    tmp_path, monkeypatch, url, secret
):
    monkeypatch.setattr("agent.redact._REDACT_ENABLED", False)
    path = tmp_path / "tool_calls.log"
    record = logging.LogRecord("tool", logging.INFO, "", 0, "preview: %s", (url,), None)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(RedactingFormatter("%(message)s", redact_url_credentials=True))
    try:
        handler.emit(record)
    finally:
        handler.close()

    assert secret not in path.read_text(encoding="utf-8")
    assert record.msg == "preview: %s"
    assert record.args == (url,)
    assert record.getMessage() == "preview: " + url


def test_default_formatter_and_tool_redaction_preserve_round_trip_url():
    url = "https://example.test/callback?code=opaque-code-fixture"
    record = logging.LogRecord("tool", logging.INFO, "", 0, "%s", (url,), None)

    assert RedactingFormatter("%(message)s").format(record) == url
    assert redact_sensitive_text(url) == url
