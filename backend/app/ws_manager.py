import asyncio

from fastapi import WebSocket
from loguru import logger


class ConnectionManager:
    def __init__(self, send_timeout: float = 5.0):
        self.active: list[WebSocket] = []
        self.send_timeout = send_timeout

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)
        logger.info(f"WS connected, total={len(self.active)}")

    def disconnect(self, ws: WebSocket):
        try:
            self.active.remove(ws)
        except ValueError:
            pass
        logger.info(f"WS disconnected, total={len(self.active)}")

    async def broadcast(self, payload: dict):
        connections = tuple(self.active)
        if not connections:
            return

        results = await asyncio.gather(
            *(self._send(ws, payload) for ws in connections)
        )
        failed_ids = {
            id(ws) for ws, succeeded in zip(connections, results) if not succeeded
        }
        if failed_ids:
            self.active[:] = [ws for ws in self.active if id(ws) not in failed_ids]

    async def _send(self, ws: WebSocket, payload: dict) -> bool:
        try:
            await asyncio.wait_for(ws.send_json(payload), timeout=self.send_timeout)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(f"WS send failed ({type(exc).__name__}: {exc})")
            return False


manager = ConnectionManager()
