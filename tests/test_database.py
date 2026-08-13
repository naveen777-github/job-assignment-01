from telemetry_gateway.database import TelemetryStore
from telemetry_gateway.models import BootRegistrationInput, TelemetryInput


def telemetry(**overrides) -> TelemetryInput:
    values = {
        "deviceId": "device-01",
        "bootId": "boot-a",
        "sequence": 1,
        "deviceTime": "2026-08-12T09:00:00+00:00",
        "metric": "temperature",
        "value": 21.4,
    }
    values.update(overrides)
    return TelemetryInput.model_validate(values)


def test_registers_a_boot_idempotently() -> None:
    store = TelemetryStore(":memory:")
    try:
        event = BootRegistrationInput(deviceId="device-01", bootId="boot-a")

        first = store.register_boot(event)
        second = store.register_boot(event)

        assert first.to_api() == {
            "deviceId": "device-01",
            "bootId": "boot-a",
            "generation": 1,
            "created": True,
        }
        assert second.to_api() == {
            "deviceId": "device-01",
            "bootId": "boot-a",
            "generation": 1,
            "created": False,
        }
    finally:
        store.close()


def test_stores_a_basic_event_and_calculates_current_state() -> None:
    store = TelemetryStore(":memory:")
    try:
        store.register_boot(BootRegistrationInput(deviceId="device-01", bootId="boot-a"))

        result = store.ingest(telemetry(), "2026-08-12T09:00:01+00:00")

        assert result.duplicate is False
        assert result.current_changed is True
        assert store.list_current_states()[0].to_api() == {
            "deviceId": "device-01",
            "bootId": "boot-a",
            "generation": 1,
            "sequence": 1,
            "deviceTime": "2026-08-12T09:00:00+00:00",
            "receivedAt": "2026-08-12T09:00:01+00:00",
            "metric": "temperature",
            "value": 21.4,
        }
        assert len(store.list_events(10)) == 1
    finally:
        store.close()


def test_repeated_event_from_same_boot_is_a_duplicate() -> None:
    store = TelemetryStore(":memory:")
    try:
        store.register_boot(BootRegistrationInput(deviceId="device-01", bootId="boot-a"))
        store.ingest(telemetry(), "2026-08-12T09:00:01+00:00")

        duplicate = store.ingest(telemetry(), "2026-08-12T09:00:02+00:00")

        assert duplicate.to_api() == {
            "accepted": True,
            "duplicate": True,
            "currentChanged": False,
        }
        assert len(store.list_events(10)) == 1
    finally:
        store.close()


def test_a_delayed_event_does_not_move_current_state_backward() -> None:
    # sequence=2 arrives first, then a late sequence=1 shows up. Even though
    # the late event carries a later deviceTime, it must not overwrite the
    # newer (by sequence) current state.
    store = TelemetryStore(":memory:")
    try:
        store.register_boot(BootRegistrationInput(deviceId="device-01", bootId="boot-a"))
        store.ingest(
            telemetry(sequence=2, deviceTime="2026-08-12T09:00:02+00:00"),
            "2026-08-12T09:00:02+00:00",
        )

        delayed = store.ingest(
            telemetry(sequence=1, deviceTime="2026-08-12T09:00:05+00:00"),
            "2026-08-12T09:00:06+00:00",
        )

        assert delayed.duplicate is False
        assert delayed.current_changed is False
        assert store.list_current_states()[0].to_api()["sequence"] == 2
    finally:
        store.close()


def test_a_wrong_device_clock_does_not_block_a_later_valid_reading() -> None:
    # boot-a's clock reports a device_time far in the past, but its sequence
    # is legitimately newer. Current state must still advance.
    store = TelemetryStore(":memory:")
    try:
        store.register_boot(BootRegistrationInput(deviceId="device-01", bootId="boot-a"))
        store.ingest(
            telemetry(sequence=1, deviceTime="2026-08-12T09:00:00+00:00"),
            "2026-08-12T09:00:00+00:00",
        )

        skewed = store.ingest(
            telemetry(sequence=2, deviceTime="2000-01-01T00:00:00+00:00"),
            "2026-08-12T09:00:01+00:00",
        )

        assert skewed.current_changed is True
        assert store.list_current_states()[0].to_api()["sequence"] == 2
    finally:
        store.close()


def test_a_newer_boot_generation_wins_even_with_a_lower_sequence() -> None:
    # boot-b (a fresh restart, higher generation) starts back at sequence=1.
    # It must supersede boot-a's higher sequence numbers.
    store = TelemetryStore(":memory:")
    try:
        store.register_boot(BootRegistrationInput(deviceId="device-01", bootId="boot-a"))
        store.ingest(
            telemetry(bootId="boot-a", sequence=9, deviceTime="2026-08-12T09:00:09+00:00"),
            "2026-08-12T09:00:09+00:00",
        )

        store.register_boot(BootRegistrationInput(deviceId="device-01", bootId="boot-b"))
        restarted = store.ingest(
            telemetry(bootId="boot-b", sequence=1, deviceTime="2026-08-12T09:01:00+00:00"),
            "2026-08-12T09:01:00+00:00",
        )

        assert restarted.current_changed is True
        state = store.list_current_states()[0].to_api()
        assert state["bootId"] == "boot-b"
        assert state["sequence"] == 1
    finally:
        store.close()


def test_same_sequence_from_a_new_boot_is_not_mistaken_for_a_duplicate() -> None:
    # Event identity is (deviceId, bootId, sequence). Sequence restarts at 1
    # on every boot, so boot-b's sequence=1 must not collide with boot-a's.
    store = TelemetryStore(":memory:")
    try:
        store.register_boot(BootRegistrationInput(deviceId="device-01", bootId="boot-a"))
        first = store.ingest(telemetry(bootId="boot-a"), "2026-08-12T09:00:01+00:00")

        store.register_boot(BootRegistrationInput(deviceId="device-01", bootId="boot-b"))
        second = store.ingest(
            telemetry(bootId="boot-b", deviceTime="2026-08-12T09:05:00+00:00"),
            "2026-08-12T09:05:00+00:00",
        )

        assert first.duplicate is False
        assert second.duplicate is False
        assert second.current_changed is True
        assert len(store.list_events(10)) == 2
    finally:
        store.close()
