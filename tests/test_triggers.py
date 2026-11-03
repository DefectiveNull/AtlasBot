import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from aibot.bot import AUTOMATED_REPLIES, Agent, author_kind, said, speaker
from aibot.config import Config
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


def message(message_id, content, author_id=5, bot=False, webhook_id=None, **extra):
    return NS(
        id=message_id,
        content=content,
        author=NS(bot=bot, display_name="member", id=author_id),
        webhook_id=webhook_id,
        guild=NS(id=1),
        channel=NS(id=2, typing=Typing),
        reply=AsyncMock(),
        **extra,
    )


def embed(title, description, *fields):
    return NS(title=title, description=description, fields=[NS(name=n, value=v) for n, v in fields])


def test_automated_speakers_are_labelled_and_embeds_read():
    hook = message(1, "", author_id=9, bot=True, webhook_id=9)
    app = message(2, "hi", author_id=8, bot=True)
    assert author_kind(hook) == "webhook"
    assert author_kind(app) == "app"
    assert author_kind(message(3, "hi")) is None
    assert speaker(app.author, "app") == '⟦speaker id=8 name="member" kind=app⟧'
    hook.embeds = [embed("Alert", "Disk full", ("Host", "db1"))]
    assert said(hook) == "Alert\nDisk full\nHost: db1"


async def test_webhooks_and_apps_trigger_with_a_loop_limit(config):
    async with Agent(config) as bot:
        await bot.history.initialize()
        bot._connection.user = NS(id=7)
        bot._reply = AsyncMock()
        await bot.on_message(message(1, "atlas hello", author_id=7, bot=True))
        bot._reply.assert_not_called()
        for message_id in range(AUTOMATED_REPLIES + 2):
            await bot.on_message(message(message_id, "atlas hello", 9, bot=True, webhook_id=9))
        await asyncio.gather(*bot.pending)
        assert bot._reply.await_count == AUTOMATED_REPLIES
        # People are never limited, and automated senders get no error replies.
        await bot.on_message(message(50, "atlas human"))
        await asyncio.gather(*bot.pending)
        assert bot._reply.await_count == AUTOMATED_REPLIES + 1
        bot.automated[(1, 2)] = [time.monotonic() - 3600]
        empty = message(51, "atlas", 9, bot=True)
        await bot.on_message(empty)
        empty.reply.assert_not_called()
    async with Agent(replace(config, automated_triggers=False)) as bot:
        await bot.history.initialize()
        bot._connection.user = NS(id=7)
        bot._reply = AsyncMock()
        await bot.on_message(message(1, "atlas hello", 9, bot=True))
        assert not bot.pending


async def test_replied_to_message_is_quoted_and_taken_from_context(config):
    async with Agent(replace(config, context_enabled=True)) as bot:
        await bot.history.initialize()
        bot._connection.user = NS(id=7)
        original = message(10, "", author_id=11, bot=True, webhook_id=11)
        original.embeds = [embed("Claim", "the moon is cheese")]
        await bot.on_message(original)
        await bot.on_message(message(11, "unrelated chatter", author_id=12))
        bot.catalog.refresh = AsyncMock(return_value=(config.model,))
        create = AsyncMock(return_value=NS(choices=[NS(message=NS(content="No."))]))
        bot.llm = NS(chat=NS(completions=NS(create=create)))
        reference = NS(message_id=10, channel_id=2, resolved=original)
        trigger = message(12, "<@7> true?", reference=reference)
        await bot._reply(trigger, "true?")
        sent = create.await_args.kwargs["messages"]
        assert sent[-1]["content"] == (
            '⟦speaker id=5 name="member"⟧ [replying to ⟦speaker id=11 name="member" '
            "kind=webhook⟧ Claim\nthe moon is cheese]\ntrue?"
        )
        assert "cheese" not in sent[-2]["content"]
        assert "unrelated chatter" in sent[-2]["content"]
        assert await bot.context.before(1, 2, 99) == []


async def test_quote_skips_own_deleted_and_other_channel_messages(config):
    async with Agent(config) as bot:
        bot._connection.user = NS(id=7)
        own = message(10, "my earlier answer", author_id=7, bot=True)
        for reference in [
            NS(message_id=10, channel_id=2, resolved=own),
            NS(message_id=10, channel_id=2, resolved=NS(id=10)),
            NS(message_id=10, channel_id=3, resolved=message(10, "elsewhere")),
        ]:
            assert await bot._quoted(message(12, "hi", reference=reference)) is None
        fetched = message(10, "fetched text", author_id=11)
        trigger = message(12, "hi", reference=NS(message_id=10, channel_id=2, resolved=None))
        trigger.channel.fetch_message = AsyncMock(return_value=fetched)
        assert await bot._quoted(trigger) is fetched
