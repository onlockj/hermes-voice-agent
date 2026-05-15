from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from fastapi import WebSocket


@dataclass
class VoiceSession:
    session_id: str
    user_id: Optional[int]
    client_ws: WebSocket
    history: List[dict] = field(default_factory=list)
    capture_buf: bytearray = field(default_factory=bytearray)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    turn_task: Optional[asyncio.Task] = None
    closed: bool = False


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
