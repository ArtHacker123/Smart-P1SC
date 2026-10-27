import json
import logging

from smart_p1_sc.logging_setup import (
    REDACTED,
    JsonFormatter,
    SecretRedactor,
    TextFormatter,
    setup_logging,
)


def make_record(message, **extra):
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_formatter_redacts_registered_secret():
    redactor = SecretRedactor(["abcd1234"])
    payload = json.loads(JsonFormatter(redactor).format(make_record("code abcd1234")))
    assert payload["message"] == "code " + REDACTED
    assert "abcd1234" not in json.dumps(payload)


def test_json_formatter_redacts_sensitive_keys():
    redactor = SecretRedactor()
    payload = json.loads(
        JsonFormatter(redactor).format(
            make_record("ok", access_code="1234", openclaw_token="tok", nested={"password": "x"})
        )
    )
    assert payload["access_code"] == REDACTED
    assert payload["openclaw_token"] == REDACTED
    assert payload["nested"]["password"] == REDACTED


def test_json_formatter_preserves_ordinary_extra():
    payload = json.loads(JsonFormatter(SecretRedactor()).format(make_record("ok", incident_id="abc")))
    assert payload["incident_id"] == "abc"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test"


def test_bytes_are_summarized():
    redactor = SecretRedactor()
    assert redactor.redact_value(b"1234") == "<bytes:4>"


def test_text_formatter_redacts():
    redactor = SecretRedactor(["secret"])
    text = TextFormatter(redactor).format(make_record("value secret"))
    assert "secret" not in text
    assert REDACTED in text


def test_setup_logging_is_idempotent():
    setup_logging("DEBUG", json_output=True)
    setup_logging("INFO", json_output=False)
    assert len(logging.getLogger().handlers) == 1
    assert logging.getLogger().level == logging.INFO


def test_setup_logging_writes_redacted_file(tmp_path):
    log_path = tmp_path / "nested" / "daemon.log"
    setup_logging("INFO", json_output=True, secrets=["secret"], file_path=str(log_path))
    logging.getLogger("test").info("value secret")
    for handler in logging.getLogger().handlers:
        handler.flush()
    content = log_path.read_text(encoding="utf-8")
    assert REDACTED in content
    assert "secret" not in content
    assert len(logging.getLogger().handlers) == 2
    setup_logging("INFO", json_output=False)
    assert len(logging.getLogger().handlers) == 1
