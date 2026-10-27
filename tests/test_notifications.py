import json

from smart_p1_sc.config import HoldConfig, OpenClawConfig
from smart_p1_sc.models import Incident, IncidentOutcome, IncidentState, Severity, utcnow
from smart_p1_sc.notifications import (
    OpenClawNotifier,
    format_incident_text,
    parse_reply,
)


def incident(short_code="7F3A", outcome=None):
    return Incident(
        id="inc-1",
        short_code=short_code,
        kind="spaghetti",
        severity=Severity.P1,
        confidence=0.91,
        state=IncidentState.HOLDING,
        layer_num=5,
        total_layers=20,
        outcome=outcome,
        bbox=(0.1, 0.1, 0.3, 0.3),
        area_growth=0.2,
    )


def test_holding_message_contains_actions_and_hold_settings():
    text = format_incident_text(
        incident(),
        "holding",
        HoldConfig(nozzle_temperature_c=150, max_wait_minutes=60),
    )
    assert "7F3A" in text
    assert "炒面/拉丝" in text
    assert "150°C" in text
    assert "继续 7F3A" in text
    assert "停止 7F3A" in text
    assert "误报 7F3A" in text
    assert "查看 7F3A" in text


def test_observation_message_does_not_offer_stop():
    text = format_incident_text(incident(), "observation")
    assert "未自动暂停" in text
    assert "查看 7F3A" in text
    assert "继续 7F3A" not in text


def test_resolved_message_reports_outcome():
    text = format_incident_text(incident(outcome="auto_stopped"), "resolved")
    assert "超时自动停止" in text
    assert "继续 7F3A" not in text


def test_parse_reply_with_short_code():
    reply = parse_reply("继续 7F3A")
    assert reply.action == "resume"
    assert reply.short_code == "7F3A"
    assert reply.error is None


def test_parse_reply_lowercase_code_requires_digit():
    assert parse_reply("停止 7f3a").short_code == "7F3A"
    assert parse_reply("stop face").short_code is None


def test_parse_reply_infers_single_open_incident():
    reply = parse_reply("停止", [incident()])
    assert reply.action == "stop"
    assert reply.short_code == "7F3A"


def test_parse_reply_requires_clarification_for_multiple_open():
    reply = parse_reply("继续", [incident("1111"), incident("2222")])
    assert reply.error == "multiple_open_incidents"
    assert reply.short_code is None


def test_parse_reply_errors():
    assert parse_reply("hello").error == "unknown_action"
    assert parse_reply("继续").error == "missing_short_code"
    assert parse_reply("继续", []).error == "no_open_incident"
    assert parse_reply("继续 1111 2222").error == "multiple_codes"
    assert parse_reply("查看 7F3A").action == "view"


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, body, headers, timeout):
        self.calls.append((url, body, dict(headers), timeout))
        return self.responses.pop(0)


def notifier(transport, retries=3, token="secret"):
    config = OpenClawConfig(
        hook_url="http://127.0.0.1:18789/hooks/print-anomaly",
        token_env="TEST_OPENCLAW_TOKEN",
        retries=retries,
    )
    import os

    if token is None:
        os.environ.pop("TEST_OPENCLAW_TOKEN", None)
    else:
        os.environ["TEST_OPENCLAW_TOKEN"] = token
    sleeps = []
    instance = OpenClawNotifier(
        config,
        HoldConfig(),
        transport=transport,
        sleep=sleeps.append,
    )
    return instance, sleeps


def test_notifier_posts_json_with_token_and_idempotency():
    transport = FakeTransport([(200, b"ok")])
    instance, _ = notifier(transport)
    assert instance.notify_event("holding", incident()) is True
    url, body, headers, timeout = transport.calls[0]
    payload = json.loads(body.decode("utf-8"))
    assert payload["event"] == "holding"
    assert payload["incident"]["short_code"] == "7F3A"
    assert headers["Authorization"] == "Bearer secret"
    assert headers["X-Idempotency-Key"] == "inc-1:holding"
    assert timeout == 10.0


def test_notifier_retries_server_errors_then_succeeds():
    transport = FakeTransport([(500, b"no"), (200, b"ok")])
    instance, sleeps = notifier(transport)
    assert instance.notify_event("holding", incident()) is True
    assert len(transport.calls) == 2
    assert sleeps == [1]


def test_notifier_does_not_retry_client_error():
    transport = FakeTransport([(400, b"bad")])
    instance, sleeps = notifier(transport)
    assert instance.notify_event("holding", incident()) is False
    assert len(transport.calls) == 1
    assert sleeps == []


def test_notifier_retries_rate_limit():
    transport = FakeTransport([(429, b"slow"), (200, b"ok")])
    instance, _ = notifier(transport)
    assert instance.notify_event("holding", incident()) is True


def test_notifier_exhausts_retries():
    transport = FakeTransport([(500, b"no"), (500, b"no"), (500, b"no")])
    instance, sleeps = notifier(transport, retries=3)
    assert instance.notify_event("holding", incident()) is False
    assert len(transport.calls) == 3
    assert sleeps == [1, 2]
