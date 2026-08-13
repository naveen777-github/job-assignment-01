import asyncio
from datetime import datetime, timezone

from telemetry_gateway.models import (
    BootRegistrationResult,
    DeviceState,
    IngestResult,
    TelemetryInput,
)
from telemetry_gateway.service import TelemetryService


class FakeRepository:
    def __init__(self, state: DeviceState, result: IngestResult | None = None) -> None:
        self.state = state
        self.result = result if result is not None else IngestResult(False, True, state)
        self.ingest_calls = 0

    def register_boot(self, _event):
        return BootRegistrationResult("device-01", "boot-a", 1, True)

    def ingest(self, _event, _received_at):
        self.ingest_calls += 1
        return self.result

    def list_current_states(self):
        return []

    def list_events(self, _limit):
        return []

    def ping(self):
        return True


class FailingRepository:
    """Simulates a failed database transaction."""

    def __init__(self) -> None:
        self.ingest_calls = 0

    def register_boot(self, _event):
        return BootRegistrationResult("device-01", "boot-a", 1, True)

    def ingest(self, _event, _received_at):
        self.ingest_calls += 1
        raise RuntimeError("simulated transaction failure")

    def list_current_states(self):
        return []

    def list_events(self, _limit):
        return []

    def ping(self):
        return True


class RecordingPublisher:
    def __init__(self) -> None:
        self.states: list[DeviceState] = []

    async def publish(self, state: DeviceState) -> None:
        self.states.append(state)


def make_event() -> TelemetryInput:
    return TelemetryInput.model_validate(
        {
            "deviceId": "device-01",
            "bootId": "boot-a",
            "sequence": 1,
            "deviceTime": "2026-08-12T09:00:00Z",
            "metric": "temperature",
            "value": 21.4,
        }
    )


def make_state() -> DeviceState:
    return DeviceState(
        device_id="device-01",
        boot_id="boot-a",
        generation=1,
        sequence=1,
        device_time="2026-08-12T09:00:00+00:00",
        received_at="2026-08-12T09:00:01+00:00",
        metric="temperature",
        value=21.4,
    )


def test_service_publishes_a_state_during_ingestion() -> None:
    state = make_state()
    repository = FakeRepository(state)
    publisher = RecordingPublisher()
    service = TelemetryService(
        repository,
        publisher,
        now=lambda: datetime(2026, 8, 12, 9, 0, 1, tzinfo=timezone.utc),
    )

    result = asyncio.run(service.ingest(make_event()))

    assert result.current_changed is True
    assert publisher.states == [state]
    assert repository.ingest_calls == 1


def test_service_does_not_publish_when_current_state_did_not_change() -> None:
    # Duplicate or stale events must not produce a false state-change message.
    repository = FakeRepository(make_state(), result=IngestResult(True, False))
    publisher = RecordingPublisher()
    service = TelemetryService(repository, publisher)

    result = asyncio.run(service.ingest(make_event()))

    assert result.duplicate is True
    assert result.current_changed is False
    assert publisher.states == []


def test_service_does_not_publish_when_the_transaction_fails() -> None:
    # The database transaction must complete successfully before anything
    # is published to WebSocket clients.
    repository = FailingRepository()
    publisher = RecordingPublisher()
    service = TelemetryService(repository, publisher)

    try:
        asyncio.run(service.ingest(make_event()))
        raised = False
    except RuntimeError:
        raised = True

    assert raised is True
    assert repository.ingest_calls == 1
    assert publisher.states == []
