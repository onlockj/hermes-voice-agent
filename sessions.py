from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Dict, Optional

from fastapi import WebSocket


@dataclass
class VoiceSession:
    session_id: str
    user_id: Optional[int]
    client_ws: WebSocket
    upstream_ws: object = None  # websockets.ClientConnection
    upstream_task: Optional[asyncio.Task] = None
    closed: bool = False
    meta: Dict[str, str] = field(default_factory=dict)


class SessionStore:
    def __init__(self) -> None:
        self._sessions: Dict[str, VoiceSession] = {}
        self._lock = asyncio.Lock()

    async def add(self, session: VoiceSession) -> None:
        async with self._lock:
            self._sessions[session.session_id] = session

    async def remove(self, session_id: str) -> Optional[VoiceSession]:
        async with self._lock:
            return self._sessions.pop(session_id, None)

    async def get(self, session_id: str) -> Optional[VoiceSession]:
        async with self._lock:
            return self._sessions.get(session_id)

    def count(self) -> int:
        return len(self._sessions)


store = SessionStore()
