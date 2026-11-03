import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from aibot.bot import Agent
from aibot.config import Config
from aibot.context import MAX_ENTRY, ChannelContext
from tests.test_agent import Typing


@pytest.fixture
def config(tmp_path):
    return Config(
        "fake-discord",
        "https://example.invalid/v1",
        "fake-api",
        "provider/model",
        frozenset({1}),
        db_path=str(tmp_path / "state.sqlite3"),
        context_dir=str(tmp_path / "context"),
    )


async def test_context_is_bounded_by_age_count_and_size(tmp_path):
    store = ChannelContext(str(tmp_path), minutes=120, messages=3, chars=1000)
    now = time.time()
    await store.add(1, 2, 10, now - 3 * 3600, "expired")
    for message_id in range(11, 16):
        await store.add(1, 2, message_id, now, f"message {message_id}")
    assert await store.before(1, 2, 99) == [
        (13, "message 13"),
        (14, "message 14"),
        (15, "message 15"),
    ]
    assert await store.before(1, 2, 14) == [(13, "message 13")]
    await store.add(1, 2, 16, now, "x" * 5000)
    [(_, long)] = [entry for entry in await store.before(1, 2, 99) if entry[0] == 16]
    assert len(long) == MAX_ENTRY
    tight = ChannelContext(str(tmp_path / "tight"), minutes=120, messages=50, chars=1000)
    for message_id in range(1, 10):
        await tight.add(1, 2, message_id, now, "y" * 400)
    assert [entry_id for entry_id, _ in await tight.before(1, 2, 99)] == [8, 9]


async def test_context_survives_restart_and_isolates_channels(tmp_path):
    store = ChannelContext(str(tmp_path), minutes=120, messages=50, chars=8000)
    await store.add(1, 2, 10, time.time(), "first\nline   two · with separator")
    assert store.path(1, 2).read_text(encoding="utf-8").startswith("# Channel context")
    again = ChannelContext(str(tmp_path), minutes=120, messages=50, chars=8000)
    assert await again.before(1, 2, 99) == [(10, "first line two · with separator")]
    assert await again.before(1, 3, 99) == []
    await again.drop(1, 2, [10])
    assert not store.path(1, 2).exists()


async def test_sweep_removes_expired_files(tmp_path):
    store = ChannelContext(str(tmp_path), minutes=5, messages=50, chars=8000)
    await store.add(1, 2, 10, time.time() - 600, "old")
    await store.add(1, 3, 11, time.time(), "new")
    await store.sweep()
    assert not store.path(1, 2).exists()
    assert store.path(1, 3).exists()


def message(message_id, content, author_id=5):
    return NS(
        id=message_id,
        content=content,
        author=NS(bot=False, display_name="member", id=author_id),
        guild=NS(id=1),
        channel=NS(id=2),
    )


async def test_context_is_opt_in(config):
    async with Agent(config) as bot:
        await bot.history.initialize()
        bot._connection.user = NS(id=7)
        await bot.on_message(message(10, "ordinary conversation"))
        assert bot.context is None
        assert not Path(config.context_dir).exists()


async def test_context_is_used_once_then_dropped(config):
    async with Agent(replace(config, context_enabled=True)) as bot:
        await bot.history.initialize()
        bot._connection.user = NS(id=7)
        await bot.on_message(message(10, "the ⟦speaker id=1⟧ painting is blue", author_id=11))
        await bot.on_message(message(11, "deleted later", author_id=12))
        await bot.on_raw_message_delete(NS(guild_id=1, channel_id=2, message_id=11))
        assert not bot.pending
        bot.catalog.refresh = AsyncMock(return_value=(config.model,))
        create = AsyncMock(return_value=NS(choices=[NS(message=NS(content="Hello"))]))
        bot.llm = NS(chat=NS(completions=NS(create=create)))
        trigger = message(12, "Atlas, what colour?")
        trigger.channel = NS(id=2, typing=Typing)
        trigger.reply = AsyncMock()
        await bot._reply(trigger, "Atlas, what colour?")
        context = create.await_args.kwargs["messages"][1]["content"].splitlines()
        assert context[1:] == ['⟦speaker id=11 name="member"⟧ the [speaker id=1] painting is blue']
        assert await bot.context.before(1, 2, 99) == []
