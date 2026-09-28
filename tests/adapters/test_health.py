from __future__ import annotations

import json
import logging
from dataclasses import replace
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

from likearr.adapters.health import MqttSink, StdoutSink, WebhookSink, build_sinks, publish_all
from likearr.config import HealthConfig, MqttSinkConfig, WebhookSinkConfig
from likearr.models import HealthRecord, RunStatus


def _record(**overrides: object) -> HealthRecord:
    defaults: dict[str, object] = dict(
        ts=1234567890,
        version="0.1.0",
        resolver_version=1,
        exit_code=0,
        status=RunStatus.OK,
        spotify_ok=True,
        spotify_schema_ok=True,
        mb_ok=True,
        lidarr_ok=True,
        lidarr_metadata_ok=True,
        counts={"followed_artists": 3},
        unmapped=0,
        pending_album=0,
        message="",
        dry_run=True,
    )
    defaults.update(overrides)
    return HealthRecord(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------- StdoutSink


def test_stdout_sink_prints_one_json_line(capsys: pytest.CaptureFixture[str]) -> None:
    record = _record()

    StdoutSink().publish(record)

    out = capsys.readouterr().out
    lines = out.splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed == record.to_dict()


def test_stdout_sink_swallows_errors(capsys: pytest.CaptureFixture[str]) -> None:
    record = _record()
    with patch("sys.stdout.write", side_effect=OSError("broken pipe")):
        StdoutSink().publish(record)  # must not raise

    assert True


# ---------------------------------------------------------------- WebhookSink


HOOK = "https://example.invalid/hook"


def _posted(route: respx.Route) -> dict[str, object]:
    return json.loads(route.calls.last.request.content)


def test_webhook_sink_posts_json() -> None:
    record = _record()
    config = WebhookSinkConfig(url=HOOK, timeout_s=5.0)

    with respx.mock:
        route = respx.post(HOOK).mock(return_value=httpx.Response(200))
        WebhookSink(config).publish(record)

    assert route.called
    sent = _posted(route)
    assert {k: v for k, v in sent.items() if k not in ("title", "body", "type")} == record.to_dict()


def test_webhook_sink_swallows_failure() -> None:
    record = _record()
    config = WebhookSinkConfig(url=HOOK, timeout_s=5.0)

    with respx.mock:
        respx.post(HOOK).mock(side_effect=httpx.ConnectError("boom"))
        WebhookSink(config).publish(record)  # must not raise

    assert True


# ---------------------------------------------------------------- webhook as a notification


def test_the_webhook_body_adds_title_body_and_type_and_keeps_every_record_key() -> None:
    record = _record(status=RunStatus.ERROR, exit_code=1, message="lidarr GET /artist: HTTP 500", dry_run=False)

    with respx.mock:
        route = respx.post(HOOK).mock(return_value=httpx.Response(200))
        WebhookSink(WebhookSinkConfig(url=HOOK)).publish(record)

    sent = _posted(route)
    base = record.to_dict()
    assert set(sent) == set(base) | {"title", "body", "type"}
    assert {k: sent[k] for k in base} == base, "every existing key keeps its name and value"
    assert sent["title"] == "likearr: run failed"
    assert sent["body"] == "lidarr GET /artist: HTTP 500"
    assert sent["type"] == "failure"


def test_the_new_fields_never_collide_with_a_record_field() -> None:
    assert not {"title", "body", "type"} & set(_record().to_dict())


def test_the_webhook_body_text_is_the_records_message_and_never_empty() -> None:
    """`body` is `record.message` verbatim - already redacted where the record was built - or a
    fixed line when the message is empty, as it is on every clean run."""
    with respx.mock:
        route = respx.post(HOOK).mock(return_value=httpx.Response(200))
        WebhookSink(WebhookSinkConfig(url=HOOK)).publish(_record(dry_run=False, message=""))

    sent = _posted(route)
    assert isinstance(sent["body"], str) and sent["body"].strip()
    assert sent["message"] == ""


def test_stdout_and_mqtt_payloads_carry_no_notification_fields(capsys: pytest.CaptureFixture[str]) -> None:
    """Only the webhook body changes: stdout and MQTT stay byte-for-byte `record.to_dict()`."""
    record = _record(status=RunStatus.ERROR, exit_code=1, message="boom", dry_run=False)
    fake_client = MagicMock()

    StdoutSink().publish(record)
    with patch("likearr.adapters.health.mqtt.Client", return_value=fake_client):
        MqttSink(MqttSinkConfig(host="h", topic="t")).publish(record)

    assert capsys.readouterr().out == json.dumps(record.to_dict()) + "\n"
    assert fake_client.publish.call_args[0][1] == json.dumps(record.to_dict())


def test_a_recovery_is_titled_back_to_ok() -> None:
    with respx.mock:
        route = respx.post(HOOK).mock(return_value=httpx.Response(200))
        publish_all([WebhookSink(WebhookSinkConfig(url=HOOK))], _record(dry_run=False), notify=True)

    assert _posted(route)["title"] == "likearr: back to ok"
    assert _posted(route)["type"] == "success"


def test_an_ok_that_is_not_a_recovery_is_titled_run_ok() -> None:
    """Only an `always` webhook ever sees one."""
    with respx.mock:
        route = respx.post(HOOK).mock(return_value=httpx.Response(200))
        publish_all([WebhookSink(WebhookSinkConfig(url=HOOK))], _record(dry_run=False), notify=False)

    assert _posted(route)["title"] == "likearr: run ok"


@pytest.mark.parametrize("status", [502, 400, 404, 301])
def test_a_non_2xx_response_logs_one_warning_with_the_status_and_does_not_raise(
    status: int, caplog: pytest.LogCaptureFixture
) -> None:
    with respx.mock, caplog.at_level(logging.WARNING, logger="likearr.adapters.health"):
        respx.post(HOOK).mock(return_value=httpx.Response(status))
        WebhookSink(WebhookSinkConfig(url=HOOK)).publish(_record(dry_run=False))

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert str(status) in warnings[0].getMessage()
    assert HOOK not in warnings[0].getMessage(), "the URL may carry a key or a private topic"


def test_a_2xx_response_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    with respx.mock, caplog.at_level(logging.WARNING, logger="likearr.adapters.health"):
        respx.post(HOOK).mock(return_value=httpx.Response(204))
        WebhookSink(WebhookSinkConfig(url=HOOK)).publish(_record(dry_run=False))

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.parametrize(
    ("notify", "posted"),
    [(True, True), (False, False)],
)
def test_a_problems_webhook_posts_only_what_is_worth_a_notification(notify: bool, posted: bool) -> None:
    sink = WebhookSink(WebhookSinkConfig(url=HOOK, notify="problems"))

    with respx.mock:
        route = respx.post(HOOK).mock(return_value=httpx.Response(200))
        publish_all([sink], _record(dry_run=False, status=RunStatus.ERROR, exit_code=1, message="x"), notify=notify)

    assert route.called is posted


@pytest.mark.parametrize("status", [RunStatus.PAUSED, RunStatus.SKIPPED])
def test_a_problems_webhook_never_posts_a_paused_or_skipped_run_even_unasked(status: RunStatus) -> None:
    """The CLI's no-state-database path publishes `paused` with the default `notify=True`."""
    sink = WebhookSink(WebhookSinkConfig(url=HOOK, notify="problems"))

    with respx.mock:
        route = respx.post(HOOK).mock(return_value=httpx.Response(200))
        publish_all([sink], _record(dry_run=False, status=status))

    assert not route.called


@pytest.mark.parametrize("notify", [True, False])
def test_an_always_webhook_posts_every_non_dry_record_as_before(notify: bool) -> None:
    sink = WebhookSink(WebhookSinkConfig(url=HOOK))

    with respx.mock:
        route = respx.post(HOOK).mock(return_value=httpx.Response(200))
        for status in RunStatus:
            publish_all([sink], _record(dry_run=False, status=status), notify=notify)

    assert route.call_count == len(RunStatus)


@pytest.mark.parametrize("notify_mode", ["always", "problems"])
def test_no_webhook_ever_sees_a_dry_run_or_a_local_only_record(notify_mode: str) -> None:
    sink = WebhookSink(WebhookSinkConfig(url=HOOK, notify=notify_mode))
    error = _record(status=RunStatus.ERROR, exit_code=1, message="x")

    with respx.mock:
        route = respx.post(HOOK).mock(return_value=httpx.Response(200))
        publish_all([sink], error, notify=True)  # dry run
        publish_all([sink], replace(error, dry_run=False, status=RunStatus.STALE), local_only=True, notify=True)

    assert not route.called


def test_notify_false_never_holds_back_the_other_sinks() -> None:
    """MQTT is retained state Home Assistant's `ts` dead-man's switch reads, so it needs every run."""
    record = _record(dry_run=False)
    local = MagicMock()
    local.local = True
    remote = MagicMock()
    remote.local = False

    publish_all([local, remote], record, notify=False)

    local.publish.assert_called_once_with(record)
    remote.publish.assert_called_once_with(record)


# ---------------------------------------------------------------- MqttSink


def test_mqtt_sink_publishes_retained_qos1(monkeypatch: pytest.MonkeyPatch) -> None:
    record = _record()
    config = MqttSinkConfig(host="mqtt.invalid", topic="likearr/health", port=1883, retain=True)

    fake_client = MagicMock()
    fake_info = MagicMock()
    fake_client.publish.return_value = fake_info

    with patch("likearr.adapters.health.mqtt.Client", return_value=fake_client) as client_cls:
        MqttSink(config).publish(record)

    client_cls.assert_called_once()
    fake_client.connect.assert_called_once()
    topic, payload = fake_client.publish.call_args[0]
    assert topic == "likearr/health"
    assert json.loads(payload) == record.to_dict()
    assert fake_client.publish.call_args.kwargs["qos"] == 1
    assert fake_client.publish.call_args.kwargs["retain"] is True
    fake_info.wait_for_publish.assert_called_once()
    fake_client.disconnect.assert_called_once()


def test_mqtt_sink_sets_credentials_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    record = _record()
    config = MqttSinkConfig(host="mqtt.invalid", topic="t")
    monkeypatch.setenv("LIKEARR_MQTT_USERNAME", "bob")
    monkeypatch.setenv("LIKEARR_MQTT_PASSWORD", "hunter2")

    fake_client = MagicMock()

    with patch("likearr.adapters.health.mqtt.Client", return_value=fake_client):
        MqttSink(config).publish(record)

    fake_client.username_pw_set.assert_called_once_with("bob", "hunter2")


def test_mqtt_sink_swallows_connection_error() -> None:
    record = _record()
    config = MqttSinkConfig(host="mqtt.invalid", topic="t")

    with patch("likearr.adapters.health.mqtt.Client", side_effect=OSError("no broker")):
        MqttSink(config).publish(record)  # must not raise

    assert True


# ---------------------------------------------------------------- build_sinks / publish_all


def test_build_sinks_stdout_only() -> None:
    sinks = build_sinks(HealthConfig(stdout=True))

    assert len(sinks) == 1
    assert isinstance(sinks[0], StdoutSink)


def test_build_sinks_all_three() -> None:
    health = HealthConfig(
        stdout=True,
        mqtt=MqttSinkConfig(host="h", topic="t"),
        webhook=WebhookSinkConfig(url="https://example.invalid"),
    )

    sinks = build_sinks(health)

    kinds = [type(s) for s in sinks]
    assert kinds == [StdoutSink, MqttSink, WebhookSink]


def test_build_sinks_none() -> None:
    sinks = build_sinks(HealthConfig(stdout=False))

    assert sinks == []


def test_publish_all_calls_every_sink() -> None:
    record = _record(dry_run=False)
    sink1 = MagicMock()
    sink2 = MagicMock()

    publish_all([sink1, sink2], record)

    sink1.publish.assert_called_once_with(record)
    sink2.publish.assert_called_once_with(record)


# ---------------------------------------------------------------- dry runs stay local


def test_a_dry_run_reaches_only_local_sinks() -> None:
    """A dry-run record must never reach a retained/remote sink (MQTT, webhook)."""
    record = _record(dry_run=True)
    local = MagicMock()
    local.local = True
    remote = MagicMock()
    remote.local = False

    publish_all([local, remote], record)

    local.publish.assert_called_once_with(record)
    remote.publish.assert_not_called()


def test_an_apply_reaches_every_sink_local_or_not() -> None:
    record = _record(dry_run=False)
    local = MagicMock()
    local.local = True
    remote = MagicMock()
    remote.local = False

    publish_all([local, remote], record)

    local.publish.assert_called_once_with(record)
    remote.publish.assert_called_once_with(record)


def test_stdout_sink_is_local() -> None:
    assert StdoutSink.local is True


def test_mqtt_and_webhook_sinks_are_not_local() -> None:
    assert MqttSink.local is False
    assert WebhookSink.local is False
