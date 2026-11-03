"""Opt-in, short-lived channel context kept in one CONTEXT.md per channel.

Untriggered messages wait here until the bot uses them in a reply or they
expire. Each channel's file is bounded by age, entry count and characters, and
a message deleted on Discord is deleted here too.
"""

import asyncio
import os
import time
from datetime import UTC, datetime
from pathlib import Path

MAX_ENTRY = 500
SEPARATOR = " · "


def _parse(text: str) -> list[tuple[float, int, str]]:
    entries = []
    for line in text.splitlines():
        if not line.startswith("- "):
            continue
        try:
            stamp, message_id, body = line[2:].split(SEPARATOR, 2)
            entries.append((datetime.fromisoformat(stamp).timestamp(), int(message_id), body))
        except ValueError:
            continue
    return entries


def _render(guild: int, channel: int, minutes: int, entries) -> str:
    lines = [
        f"# Channel context ({guild}/{channel})",
        "",
        f"Untriggered messages from the last {minutes} minutes, oldest first.",
        "",
    ]
    for created, message_id, body in entries:
        stamp = datetime.fromtimestamp(created, UTC).isoformat(timespec="seconds")
        lines.append(f"- {stamp}{SEPARATOR}{message_id}{SEPARATOR}{body}")
    return "\n".join(lines) + "\n"


class ChannelContext:
    def __init__(self, directory: str, minutes: int, messages: int, chars: int):
        self.directory = Path(directory)
        self.minutes, self.messages, self.chars = minutes, messages, chars
        self._lock = asyncio.Lock()

    def path(self, guild: int, channel: int) -> Path:
        return self.directory / str(guild) / str(channel) / "CONTEXT.md"

    async def _edit(self, guild: int, channel: int, change=None):
        """Load, prune, optionally change and save one channel's entries."""
        async with self._lock:
            return await asyncio.to_thread(self._edit_sync, guild, channel, change)

    def _edit_sync(self, guild, channel, change):
        path = self.path(guild, channel)
        entries = _parse(path.read_text(encoding="utf-8")) if path.exists() else []
        before = list(entries)
        cutoff = time.time() - self.minutes * 60
        entries = [entry for entry in entries if entry[0] >= cutoff]
        result = change(entries) if change else None
        # Keep the newest entries that fit both caps.
        kept, size = [], 0
        for entry in reversed(entries[-self.messages :]):
            if size + len(entry[2]) > self.chars:
                break
            kept.append(entry)
            size += len(entry[2])
        kept.reverse()
        if kept != before:
            if kept:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix(".tmp")
                temporary.write_text(_render(guild, channel, self.minutes, kept), encoding="utf-8")
                os.replace(temporary, path)
            else:
                path.unlink(missing_ok=True)
        return result if result is not None else kept

    async def add(self, guild: int, channel: int, message_id: int, created: float, line: str):
        body = " ".join(line.split())
        if len(body) > MAX_ENTRY:
            body = body[: MAX_ENTRY - 1] + "…"
        await self._edit(
            guild, channel, lambda entries: entries.append((created, message_id, body))
        )

    async def before(self, guild: int, channel: int, message_id: int) -> list[tuple[int, str]]:
        entries = await self._edit(guild, channel)
        return [(entry_id, body) for _, entry_id, body in entries if entry_id < message_id]

    async def drop(self, guild: int, channel: int, message_ids):
        ids = set(message_ids)

        def remove(entries):
            entries[:] = [entry for entry in entries if entry[1] not in ids]

        await self._edit(guild, channel, remove)

    async def clear(self, guild: int, channel: int):
        await self._edit(guild, channel, lambda entries: entries.clear())

    async def clear_others(self, guild: int, channel: int):
        """Clear every channel file in a server except one, for a channel lock."""
        paths = await asyncio.to_thread(lambda: list(self.directory.glob(f"{guild}/*/CONTEXT.md")))
        for path in paths:
            try:
                other = int(path.parent.name)
            except ValueError:
                continue
            if other != channel:
                await self.clear(guild, other)

    async def sweep(self):
        """Prune expired entries in every channel file, removing empty ones."""
        for path in await asyncio.to_thread(lambda: list(self.directory.glob("*/*/CONTEXT.md"))):
            try:
                await self._edit(int(path.parent.parent.name), int(path.parent.name))
            except ValueError:
                continue
