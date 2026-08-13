from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from fastapi import WebSocket

from telemetry_gateway.models import DeviceState

logger = logging.getLogger(__name__)

DEFAULT_CLIENT_QUEUE_LIMIT = 32


class StatePublisher(Protocol):
    async def publish(self, state: DeviceState) -> None: ...


class _Client:
    def __init__(self, websocket: WebSocket, queue_limit: int) -> None:
        self.websocket = websocket
        self.queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=queue_limit)
        self.sender_task: asyncio.Task[None] | None = None


class RealtimeHub:
    """Fans out state-change messages to connected dashboards.

    Each client gets its own bounded queue and a dedicated sender task, so a
    client that cannot keep up (a slow network, a stalled tab) only fills its
    own queue instead of blocking the broadcast loop or other clients. Once a
    client's queue is full, that client is dropped rather than allowed to
    grow memory use without bound.
    """

    def __init__(self, queue_limit: int = DEFAULT_CLIENT_QUEUE_LIMIT) -> None:
        self._clients: dict[WebSocket, _Client] = {}
        self._queue_limit = queue_limit

    async def connect(self, client: WebSocket) -> None:
        await client.accept()
        connection = _Client(client, self._queue_limit)
        connection.sender_task = asyncio.create_task(self._pump(connection))
        self._clients[client] = connection

    def disconnect(self, client: WebSocket) -> None:
        connection = self._clients.pop(client, None)
        if connection is not None and connection.sender_task is not None:
            connection.sender_task.cancel()

    async def publish(self, state: DeviceState) -> None:
        message = {"type": "device.state.changed", "data": state.to_api()}
        for connection in tuple(self._clients.values()):
            try:
                connection.queue.put_nowait(message)
            except asyncio.QueueFull:
                logger.warning("Dropping slow websocket client: queue limit exceeded")
                await self._drop(connection)

    async def _pump(self, connection: _Client) -> None:
        try:
            while True:
                message = await connection.queue.get()
                await connection.websocket.send_json(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._drop(connection)

    async def _drop(self, connection: _Client) -> None:
        self._clients.pop(connection.websocket, None)
        try:
            await connection.websocket.close()
        except Exception:
            pass
        if connection.sender_task is not None:
            connection.sender_task.cancel()

    @property
    def size(self) -> int:
        return len(self._clients)
