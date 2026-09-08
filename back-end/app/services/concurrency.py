import asyncio
from contextlib import asynccontextmanager

from app.core.errors import ApiError


class Capacity:
    """Admission guard for the supported single-worker, single-replica topology."""

    def __init__(self, maximum: int):
        self.maximum = maximum
        self._users: set[str] = set()
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def acquire(self, user_id: str):
        async with self._lock:
            if user_id in self._users:
                raise ApiError(409, "generation_active", "A generation is already active for you.")
            if len(self._users) >= self.maximum:
                raise ApiError(429, "server_busy", "The server is at capacity. Try again.", 5)
            self._users.add(user_id)
        try:
            yield
        finally:
            async with self._lock:
                self._users.remove(user_id)
