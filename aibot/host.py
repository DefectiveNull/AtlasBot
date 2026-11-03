"""HOST.md: the server admin running the bot in each guild, set with /admin."""

import asyncio
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

ENTRY = re.compile(r'^- guild=(\d+) · id=(\d+) · name=("(?:[^"\\]|\\.)*") · ')


class Hosts:
    def __init__(self, path: str):
        self.path = Path(path)
        self._lock = asyncio.Lock()
        self._entries: dict[int, str] | None = None

    def _load(self) -> dict[int, str]:
        if not self.path.exists():
            return {}
        return {
            int(match.group(1)): line
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if (match := ENTRY.match(line))
        }

    async def get(self, guild: int) -> tuple[int, str] | None:
        """The host's user id and display name for this guild, if one was set."""
        async with self._lock:
            if self._entries is None:
                self._entries = await asyncio.to_thread(self._load)
            match = ENTRY.match(self._entries.get(guild, ""))
        return (int(match.group(2)), json.loads(match.group(3))) if match else None

    async def set(self, guild: int, user_id: int, name: str, by: int):
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        name = json.dumps(" ".join(name.split())[:64], ensure_ascii=False)
        line = f"- guild={guild} · id={user_id} · name={name} · set_by={by} · {stamp}"
        async with self._lock:
            entries = await asyncio.to_thread(self._load)
            entries[guild] = line
            text = "\n".join(
                [
                    "# Hosts",
                    "",
                    "The server admin running Atlas in each server.",
                    "",
                    *entries.values(),
                ]
            )
            await asyncio.to_thread(self._write, text + "\n")
            self._entries = entries

    def _write(self, text: str):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, self.path)
