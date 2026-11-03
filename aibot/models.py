"""Discover all models visible to this Bifrost identity; never query management APIs."""

import asyncio
import time


class ModelCatalog:
    def __init__(self, client, ttl=300):
        self.client, self.ttl = client, ttl
        self.ids: tuple[str, ...] = ()
        self.expires = 0.0
        self._lock = asyncio.Lock()

    async def refresh(self):
        async with self._lock:
            if time.monotonic() < self.expires:
                return self.ids
            page = await self.client.models.list()
            found = tuple(
                sorted({m.id for m in page.data if isinstance(m.id, str) and 0 < len(m.id) <= 256})
            )
            if not found:
                raise ValueError("Bifrost returned no available models")
            self.ids, self.expires = found, time.monotonic() + self.ttl
            return found
