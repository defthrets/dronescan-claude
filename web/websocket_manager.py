"""
web/websocket_manager.py
Tracks active WebSocket connections and provides a broadcast helper.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Set

from fastapi import WebSocket

logger = logging.getLogger("drone_detect.ws")


class WebSocketManager:

    def __init__(self):
        self._conns: Set[WebSocket] = set()
        self._lock  = asyncio.Lock()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        async with self._lock:
            self._conns.add(ws)
        logger.debug("WS connected — total: %d", len(self._conns))

    async def disconnect(self, ws: WebSocket):
        async with self._lock:
            self._conns.discard(ws)
        logger.debug("WS disconnected — total: %d", len(self._conns))

    async def broadcast(self, data: dict):
        if not self._conns:
            return
        message = json.dumps(data, default=str)
        dead: Set[WebSocket] = set()

        async with self._lock:
            targets = set(self._conns)

        for ws in targets:
            try:
                await ws.send_text(message)
            except Exception:
                dead.add(ws)

        if dead:
            async with self._lock:
                self._conns -= dead

    @property
    def count(self) -> int:
        return len(self._conns)
