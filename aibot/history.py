"""Bounded, guild/channel-scoped SQLite history; no remote memory."""

import asyncio
import sqlite3
import time
from pathlib import Path


class History:
    def __init__(self, path: str, messages: int, chars: int, retention_days: int):
        self.path = Path(path)
        self.messages, self.chars = messages, chars
        self.retention_seconds = retention_days * 86400
        self._lock = asyncio.Lock()
        self._flags: dict[tuple[int, int], frozenset[str]] = {}
        self._locks: dict[int, int | None] = {}

    async def _run(self, function):
        async with self._lock:
            return await asyncio.to_thread(self._transaction, function)

    def _transaction(self, function):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=5) as db:
            return function(db)

    async def initialize(self):
        def create(db):
            # A fresh fork must never silently adopt Rivet's unrelated schema.
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables - {"messages", "models", "flags", "locks"}:
                raise ValueError("Use a fresh discord-agent history database")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY, guild INTEGER NOT NULL, channel INTEGER NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('user','assistant')), content TEXT NOT NULL,
                    created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS channel_messages ON messages(guild,channel,id);
                CREATE TABLE IF NOT EXISTS models (
                    guild INTEGER NOT NULL, channel INTEGER NOT NULL, model TEXT NOT NULL,
                    PRIMARY KEY(guild,channel));
                CREATE TABLE IF NOT EXISTS flags (
                    guild INTEGER NOT NULL, channel INTEGER NOT NULL, flag TEXT NOT NULL,
                    PRIMARY KEY(guild,channel,flag));
                CREATE TABLE IF NOT EXISTS locks (
                    guild INTEGER PRIMARY KEY, channel INTEGER NOT NULL);
            """)

        await self._run(create)
        await self.cleanup()

    async def context(self, guild: int, channel: int):
        cutoff = time.time() - self.retention_seconds
        rows = await self._run(
            lambda db: db.execute(
                "SELECT role,content FROM messages WHERE guild=? AND channel=? AND created>=? ORDER BY id DESC LIMIT ?",
                (guild, channel, cutoff, self.messages),
            ).fetchall()
        )
        result, size = [], 0
        for role, content in rows:
            if size + len(content) > self.chars:
                break
            result.append({"role": role, "content": content})
            size += len(content)
        return list(reversed(result))

    async def exchange(self, guild: int, channel: int, user: str, reply: str):
        def write(db):
            db.executemany(
                "INSERT INTO messages(guild,channel,role,content,created) VALUES(?,?,?,?,?)",
                [
                    (guild, channel, role, content, time.time())
                    for role, content in [("user", user), ("assistant", reply)]
                ],
            )
            db.execute(
                "DELETE FROM messages WHERE guild=? AND channel=? AND id NOT IN "
                "(SELECT id FROM messages WHERE guild=? AND channel=? ORDER BY id DESC LIMIT ?)",
                (guild, channel, guild, channel, self.messages),
            )

        await self._run(write)

    async def model(self, guild: int, channel: int, default: str):
        row = await self._run(
            lambda db: db.execute(
                "SELECT model FROM models WHERE guild=? AND channel=?", (guild, channel)
            ).fetchone()
        )
        return row[0] if row else default

    async def set_model(self, guild: int, channel: int, model: str):
        await self._run(
            lambda db: db.execute(
                "INSERT INTO models VALUES(?,?,?) ON CONFLICT(guild,channel) DO UPDATE SET model=excluded.model",
                (guild, channel, model),
            )
        )

    async def flags(self, guild: int, channel: int) -> frozenset[str]:
        """Per-channel switches such as "nocontext"; cached since every message checks them."""
        key = (guild, channel)
        if key not in self._flags:
            rows = await self._run(
                lambda db: db.execute(
                    "SELECT flag FROM flags WHERE guild=? AND channel=?", key
                ).fetchall()
            )
            self._flags[key] = frozenset(row[0] for row in rows)
        return self._flags[key]

    async def set_flag(self, guild: int, channel: int, flag: str, on: bool):
        query = (
            "INSERT OR IGNORE INTO flags VALUES(?,?,?)"
            if on
            else "DELETE FROM flags WHERE guild=? AND channel=? AND flag=?"
        )
        await self._run(lambda db: db.execute(query, (guild, channel, flag)))
        self._flags.pop((guild, channel), None)

    async def lock(self, guild: int) -> int | None:
        """The one channel the bot is restricted to in a server, if any; cached like flags."""
        if guild not in self._locks:
            row = await self._run(
                lambda db: db.execute(
                    "SELECT channel FROM locks WHERE guild=?", (guild,)
                ).fetchone()
            )
            self._locks[guild] = row[0] if row else None
        return self._locks[guild]

    async def set_lock(self, guild: int, channel: int | None):
        if channel is None:
            await self._run(lambda db: db.execute("DELETE FROM locks WHERE guild=?", (guild,)))
        else:
            await self._run(
                lambda db: db.execute(
                    "INSERT INTO locks VALUES(?,?) ON CONFLICT(guild) DO UPDATE SET channel=excluded.channel",
                    (guild, channel),
                )
            )
        self._locks.pop(guild, None)

    async def clear(self, guild: int, channel: int):
        await self._run(
            lambda db: db.execute(
                "DELETE FROM messages WHERE guild=? AND channel=?", (guild, channel)
            )
        )

    async def cleanup(self):
        cutoff = time.time() - self.retention_seconds
        await self._run(lambda db: db.execute("DELETE FROM messages WHERE created<?", (cutoff,)))
