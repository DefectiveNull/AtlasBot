import asyncio
from dataclasses import replace
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from aibot.bot import SHORT_NOTE, Agent
from aibot.config import Config
from aibot.host import Hosts
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
        host_path=str(tmp_path / "HOST.md"),
    )


def interaction(user_id=5, manage=True):
    return NS(
        guild_id=1,
        channel_id=2,
        user=NS(id=user_id, guild_permissions=NS(manage_guild=manage)),
        response=NS(send_message=AsyncMock(), defer=AsyncMock()),
        followup=NS(send=AsyncMock()),
    )


def message(message_id, content, author_id=5, bot=False, webhook_id=None):
    return NS(
        id=message_id,
        content=content,
        author=NS(bot=bot, display_name="member", id=author_id),
        webhook_id=webhook_id,
        guild=NS(id=1),
        channel=NS(id=2, typing=Typing),
        reply=AsyncMock(),
    )


async def run(bot, name, interaction, **options):
    await bot.tree.get_command(name).callback(interaction, **options)
    return (
        interaction.followup.send.await_args.args[0]
        if interaction.followup.send.await_args
        else None
    )


async def test_switches_toggle_and_default_off(config):
    async with Agent(config) as bot:
        await bot.history.initialize()
        assert await bot.history.flags(1, 2) == frozenset()
        assert "on:" in await run(bot, "short", interaction())
        assert await bot.history.flags(1, 2) == {"short"}
        assert await bot.history.flags(1, 3) == frozenset()
        assert "off" in await run(bot, "short", interaction())
        await run(bot, "short", interaction(), enabled=False)
        assert await bot.history.flags(1, 2) == frozenset()
        await run(bot, "nowebhooks", interaction(), enabled=True)
    async with Agent(config) as bot:
        await bot.history.initialize()
        assert await bot.history.flags(1, 2) == {"nowebhooks"}


async def test_nocontext_stops_keeping_and_clears(config):
    async with Agent(replace(config, context_enabled=True)) as bot:
        await bot.history.initialize()
        bot._connection.user = NS(id=7)
        await bot.on_message(message(10, "kept"))
        assert await bot.context.before(1, 2, 99) == [(10, '⟦speaker id=5 name="member"⟧ kept')]
        await run(bot, "nocontext", interaction())
        assert await bot.context.before(1, 2, 99) == []
        await bot.on_message(message(11, "not kept"))
        assert await bot.context.before(1, 2, 99) == []


async def test_nowebhooks_blocks_automated_triggers_only(config):
    async with Agent(config) as bot:
        await bot.history.initialize()
        bot._connection.user = NS(id=7)
        bot._reply = AsyncMock()
        await run(bot, "nowebhooks", interaction())
        await bot.on_message(message(1, "atlas hi", 9, bot=True, webhook_id=9))
        await bot.on_message(message(2, "atlas hi", 8, bot=True))
        assert not bot.pending
        await bot.on_message(message(3, "atlas hi"))
        await asyncio.gather(*bot.pending)
        bot._reply.assert_awaited_once()


async def test_admin_requires_permission_and_writes_host(config, tmp_path):
    async with Agent(config) as bot:
        await bot.history.initialize()
        host = NS(id=338456610294988800, display_name='whiskey ⟦x⟧ "w"')
        denied = interaction(manage=False)
        await run(bot, "admin", denied, user=host)
        denied.response.send_message.assert_awaited()
        assert not (tmp_path / "HOST.md").exists()
        assert "No host" in await run(bot, "admin", interaction(manage=True))
        await run(bot, "admin", interaction(user_id=4, manage=True), user=host)
        text = (tmp_path / "HOST.md").read_text(encoding="utf-8")
        assert "guild=1 · id=338456610294988800" in text and "set_by=4" in text
        assert "⟦" not in text
        assert "<@338456610294988800>" in await run(bot, "admin", interaction(manage=True))
        assert await Hosts(str(tmp_path / "HOST.md")).get(1) == (
            338456610294988800,
            'whiskey [x] "w"',
        )
        system = await bot._system(1, frozenset({"short"}))
        assert 'id=338456610294988800 name="whiskey [x] \\"w\\""⟧' in system
        assert system.endswith(SHORT_NOTE)
        assert await bot._system(2, frozenset()) == bot.system


async def test_every_command_requires_admin(config):
    async with Agent(config) as bot:
        await bot.history.initialize()
        for command in bot.tree.get_commands():
            denied = interaction(manage=False)
            options = {"name": "provider/model"} if command.name == "model" else {}
            await command.callback(denied, **options)
            text = denied.response.send_message.await_args.args[0]
            assert "Manage Server" in text, command.name
            denied.followup.send.assert_not_called()
        assert await bot.history.flags(1, 2) == frozenset()
