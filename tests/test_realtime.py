import asyncio

from telemetry_gateway.models import DeviceState
from telemetry_gateway.realtime import RealtimeHub


def make_state(sequence: int = 1) -> DeviceState:
    return DeviceState(
        device_id="device-01",
        boot_id="boot-a",
        generation=1,
        sequence=sequence,
        device_time="2026-08-12T09:00:00+00:00",
        received_at="2026-08-12T09:00:01+00:00",
        metric="temperature",
        value=21.4,
    )


class FakeWebSocket:
    """A minimal stand-in for fastapi.WebSocket that can simulate a client
    which never drains its socket (a "slow" client)."""

    def __init__(self, *, stall: asyncio.Event | None = None) -> None:
        self.sent: list[dict] = []
        self.closed = False
        self._stall = stall

    async def accept(self) -> None:
        return None

    async def send_json(self, message: dict) -> None:
        if self._stall is not None:
            await self._stall.wait()
        self.sent.append(message)

    async def close(self) -> None:
        self.closed = True


def test_a_slow_client_does_not_block_delivery_to_healthy_clients() -> None:
    async def scenario() -> None:
        hub = RealtimeHub(queue_limit=2)
        stall = asyncio.Event()
        slow = FakeWebSocket(stall=stall)
        healthy = FakeWebSocket()

        await hub.connect(slow)
        await hub.connect(healthy)

        for sequence in range(1, 6):
            await hub.publish(make_state(sequence))
            await asyncio.sleep(0)

        assert len(healthy.sent) == 5
        stall.set()
        hub.disconnect(healthy)
        await asyncio.sleep(0)

    asyncio.run(scenario())


def test_a_client_whose_queue_overflows_is_dropped_not_left_unbounded() -> None:
    async def scenario() -> None:
        hub = RealtimeHub(queue_limit=2)
        stall = asyncio.Event()
        slow = FakeWebSocket(stall=stall)

        await hub.connect(slow)
        assert hub.size == 1

        for sequence in range(1, 6):
            await hub.publish(make_state(sequence))
            await asyncio.sleep(0)

        # Once the bounded queue fills, the slow client is dropped rather
        # than allowed to accumulate an unbounded backlog.
        assert hub.size == 0
        assert slow.closed is True
        stall.set()

    asyncio.run(scenario())


def test_a_broken_client_is_isolated_and_healthy_clients_keep_receiving() -> None:
    async def scenario() -> None:
        hub = RealtimeHub(queue_limit=4)

        class BrokenWebSocket(FakeWebSocket):
            async def send_json(self, message: dict) -> None:
                raise RuntimeError("connection reset")

        broken = BrokenWebSocket()
        healthy = FakeWebSocket()
        await hub.connect(broken)
        await hub.connect(healthy)

        await hub.publish(make_state(1))
        await asyncio.sleep(0)

        assert hub.size == 1
        assert healthy.sent == [
            {"type": "device.state.changed", "data": make_state(1).to_api()}
        ]
        hub.disconnect(healthy)
        await asyncio.sleep(0)

    asyncio.run(scenario())
