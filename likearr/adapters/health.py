"""Health sinks: where a run's `HealthRecord` gets published.

Every sink is best-effort by contract (`HealthSink.publish` in `likearr.ports`): it must catch
everything and log a warning rather than raise, so a broken MQTT broker or webhook endpoint never
fails a run.
"""

from __future__ import annotations

import json
import logging
import sys

import httpx
import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from likearr.config import WEBHOOK_NOTIFY_PROBLEMS, HealthConfig, MqttSinkConfig, WebhookSinkConfig
from likearr.core.health import notification
from likearr.models import HealthRecord, RunStatus
from likearr.ports import HealthSink

logger = logging.getLogger(__name__)

_IDLE = frozenset({RunStatus.PAUSED, RunStatus.SKIPPED})

_MQTT_TIMEOUT_S = 10.0


class StdoutSink:
    """Prints one JSON line per run to stdout."""

    local = True
    """Never leaves the machine, so a dry run reaches it too (see `publish_all`)."""

    def publish(self, record: HealthRecord) -> None:
        try:
            sys.stdout.write(json.dumps(record.to_dict()) + "\n")
            sys.stdout.flush()
        except Exception:
            logger.warning("stdout health sink failed", exc_info=True)


class MqttSink:
    """Publishes a retained, QoS-1 JSON message to an MQTT broker (paho-mqtt v2 API)."""

    local = False
    """Retained on the broker, so a dry run must never reach it (see `publish_all`, issue #19)."""

    def __init__(self, config: MqttSinkConfig) -> None:
        self._config = config

    def publish(self, record: HealthRecord) -> None:
        try:
            client = mqtt.Client(CallbackAPIVersion.VERSION2)
            username = self._config.username
            password = self._config.password
            if username is not None:
                client.username_pw_set(username, password)
            client.connect(self._config.host, self._config.port, keepalive=int(_MQTT_TIMEOUT_S))
            client.loop_start()
            try:
                payload = json.dumps(record.to_dict())
                info = client.publish(self._config.topic, payload, qos=1, retain=self._config.retain)
                info.wait_for_publish(timeout=_MQTT_TIMEOUT_S)
            finally:
                client.loop_stop()
                client.disconnect()
        except Exception:
            logger.warning("mqtt health sink failed", exc_info=True)


def webhook_payload(record: HealthRecord, *, recovered: bool) -> dict[str, object]:
    """The webhook body: every key of `record.to_dict()`, unchanged, plus `title`, `body` and `type`
    (#112), so Apprise API and ntfy's templating have readable text to show. Additive only: a
    consumer of the old body keeps working, and no record field is named any of the three."""
    note = notification(record.status, record.message, recovered=recovered)
    return {**record.to_dict(), "title": note.title, "body": note.body, "type": note.type}


class WebhookSink:
    """POSTs the health record as JSON to a generic webhook URL, with a readable title and body."""

    local = False
    """A remote endpoint, so a dry run must never reach it (see `publish_all`, issue #19)."""

    def __init__(self, config: WebhookSinkConfig) -> None:
        self._config = config

    def publish(self, record: HealthRecord, *, notify: bool = True) -> None:
        """Post `record`, unless this is a `problems` webhook and the run is not news.

        `notify` is `core.health.should_notify`'s answer, worked out by the shell from the previous
        published run. An `always` webhook posts regardless, and uses it only to title an `ok`
        that clears a problem "back to ok".

        A non-2xx answer is logged with its status code and nothing else: the URL is left out,
        since an Apprise key or a private ntfy topic in it is as good as a password.
        """
        # `paused` and `skipped` are never news, whatever the caller passed: the CLI's
        # no-state-database path (#111) publishes a `paused` record without asking `should_notify`.
        if self._config.notify == WEBHOOK_NOTIFY_PROBLEMS and (not notify or record.status in _IDLE):
            return
        try:
            payload = webhook_payload(record, recovered=notify and record.status is RunStatus.OK)
            response = httpx.post(self._config.url, json=payload, timeout=self._config.timeout_s)
        except Exception:
            logger.warning("webhook health sink failed", exc_info=True)
            return
        if not response.is_success:
            logger.warning(
                "webhook health sink: the endpoint answered HTTP %d, so the notification was not delivered",
                response.status_code,
            )


def build_sinks(health: HealthConfig) -> list[HealthSink]:
    """Build the configured sinks in a stable order: stdout, mqtt, webhook."""
    sinks: list[HealthSink] = []
    if health.stdout:
        sinks.append(StdoutSink())
    if health.mqtt is not None:
        sinks.append(MqttSink(health.mqtt))
    if health.webhook is not None:
        sinks.append(WebhookSink(health.webhook))
    return sinks


def publish_all(
    sinks: list[HealthSink], record: HealthRecord, *, local_only: bool = False, notify: bool = True
) -> None:
    """Publish `record` to every sink - unless it is a dry run, which reaches only local sinks.

    ``local_only`` does the same for a record that is not a dry run but still changed nothing and
    is the user's own doing: a reviewed diff refused because its `[rules]`/`[guards]` moved since
    it was planned (`shell.run_types.ConfigStaleError`). That is a `stale` the user resolves by
    re-planning, not a fault Home Assistant should show.

    A dry run (`record.dry_run`) applies nothing, so publishing it to a retained sink would
    overwrite the last real run's result: a hand-run dry-run check would blank Home Assistant's
    view of the last scheduled apply and reset the `ts` dead-man's-switch built on it for a run
    that changed nothing (issue #19). `explain`, `doctor` and friends never build sinks at all, so
    this only changes `likearr run`. Every non-dry terminal status - `error`, `stale`, `guarded`,
    a scheduled run's `skipped` - still reaches every sink, exactly as before: those only ever
    arise with `dry_run = false` in normal operation (a scheduled cron line always carries
    `--apply`, and `stale`/`guarded` only occur mid-apply), except the configuration refusal above.

    ``notify`` is whether this run is news (`core.health.should_notify`, #112). Only the webhook
    reads it: a `notify = "problems"` webhook skips a run that is not, and every webhook titles a
    recovery. stdout and MQTT get every record exactly as before - MQTT is retained state that
    Home Assistant's `ts` dead-man's switch reads, so it needs every run.
    """
    for sink in sinks:
        if (record.dry_run or local_only) and not sink.local:
            continue
        if isinstance(sink, WebhookSink):
            sink.publish(record, notify=notify)
        else:
            sink.publish(record)
