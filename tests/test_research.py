import io
from dataclasses import replace
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import BadRequestError
from pypdf import PdfWriter

from aibot.bot import TOOL_ROUNDS, Agent
from aibot.config import Config
from aibot.research import (
    Blocked,
    Research,
    check_public,
    html_text,
    pdf_text,
    youtube_id,
)


@pytest.fixture
def config(tmp_path):
    return Config(
        "fake-discord",
        "https://example.invalid/v1",
        "fake-api",
        "provider/model",
        frozenset({1}),
        db_path=str(tmp_path / "state.sqlite3"),
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/",
        "http://127.0.0.1/",
        "http://10.0.0.5/",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://discord-agent-model-gateway.discord-agent-egress.svc.cluster.local/",
        "http://gateway.discord-agent-egress.svc/",
        "http://intranet/",
        "https://8.8.8.8:8443/",
        "https://user:pass@8.8.8.8/",
        "ftp://8.8.8.8/",
        "file:///etc/passwd",
    ],
)
async def test_internal_and_unusual_urls_are_blocked(url):
    with pytest.raises(Blocked):
        await check_public(url)


async def test_public_address_is_allowed():
    await check_public("https://8.8.8.8/")


class Response:
    def __init__(self, status, headers=None, body=b""):
        self.status, self.headers, self.body = status, headers or {}, body
        self.content_length = len(body)
        self.content = NS(iter_chunked=self._chunks)

    async def _chunks(self, size):
        yield self.body

    def raise_for_status(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


async def test_redirects_are_rechecked():
    research = Research()
    research.session = NS(
        get=lambda *a, **k: Response(302, {"Location": "http://127.0.0.1/admin"}),
        close=AsyncMock(),
    )
    with pytest.raises(Blocked):
        await research._get("http://8.8.8.8/")


@pytest.mark.parametrize("target", ["https://1.1.1.1/result", "http://8.8.8.8/result"])
async def test_redirects_do_not_forward_credentials_to_another_origin(target):
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs.get("headers")))
        return Response(302, {"Location": target}) if len(calls) == 1 else Response(200)

    research = Research("fake-key")
    research.session = NS(get=get, close=AsyncMock())
    await research._get("https://8.8.8.8/", headers={"X-Subscription-Token": "fake-key"})
    assert calls[0][1] == {"X-Subscription-Token": "fake-key"}
    assert calls[1][1] is None


async def test_same_origin_redirect_retains_api_header():
    calls = []

    def get(url, **kwargs):
        calls.append(kwargs.get("headers"))
        return Response(302, {"Location": "/result"}) if len(calls) == 1 else Response(200)

    research = Research("fake-key")
    research.session = NS(get=get, close=AsyncMock())
    await research._get("https://8.8.8.8/", headers={"X-Subscription-Token": "fake-key"})
    assert calls == [{"X-Subscription-Token": "fake-key"}] * 2


async def test_downloads_are_bounded(monkeypatch):
    monkeypatch.setattr("aibot.research.MAX_DOWNLOAD", 10)
    research = Research()
    research.session = NS(get=lambda *a, **k: Response(200, body=b"x" * 11), close=AsyncMock())
    with pytest.raises(ValueError, match="larger"):
        await research._get("http://8.8.8.8/")


def test_html_pdf_and_youtube_parsing():
    text = html_text(
        "<html><head><title>On Truth</title><script>evil()</script></head>"
        "<body><nav>menu</nav><p>Hello   world</p><p>Second</p></body></html>"
    )
    assert text == "Title: On Truth\n\nHello world\nSecond"
    # Menus are skipped even with unclosed <li>, and <main> wins over page chrome.
    text = html_text(
        "<body><p>chrome<main><header><ul><li>English<li>Deutsch</ul></header>"
        "<div role=navigation>jump</div><p>Body text<p>More</main></body>"
    )
    assert text == "Body text\nMore"
    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    writer.add_blank_page(100, 100)
    buffer = io.BytesIO()
    writer.write(buffer)
    assert pdf_text(buffer.getvalue()).splitlines() == ["[page 1] ", "[page 2] "]
    for url in [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtube.com/watch?feature=share&v=dQw4w9WgXcQ&t=30",
        "https://youtu.be/dQw4w9WgXcQ?t=5",
        "https://www.youtube.com/shorts/dQw4w9WgXcQ",
    ]:
        assert youtube_id(url) == "dQw4w9WgXcQ"
    assert youtube_id("https://example.com/watch?v=dQw4w9WgXcQ") is None


async def test_tool_dispatch_is_closed_and_failures_are_text():
    research = Research()
    assert [t["function"]["name"] for t in research.tools()] == ["read_url"]
    assert "nothing was run" in await research.call("web_search", '{"query": "x"}')
    assert "nothing was run" in await research.call("mcp_gitea_delete", "{}")
    assert "Tool failed" in await research.call("read_url", '{"url": "http://127.0.0.1/"}')
    assert "Tool failed" in await research.call("read_url", "not json")
    searchable = Research("key")
    assert [t["function"]["name"] for t in searchable.tools()] == ["web_search", "read_url"]


def test_research_config_is_opt_in_and_hides_key(config):
    assert not config.research_enabled and not config.image_input
    env = {
        "DISCORD_BOT_TOKEN": "fake-discord",
        "API_BASE": "https://example.invalid/v1",
        "MODEL": "provider/model",
        "GUILD_IDS": "1",
        "RESEARCH_ENABLED": "true",
        "BRAVE_SEARCH_API_KEY": "secret-brave",
    }
    enabled = Config.from_env(env)
    assert enabled.research_enabled and "secret-brave" not in repr(enabled)
    with pytest.raises(ValueError, match="boolean"):
        Config.from_env({**env, "RESEARCH_ENABLED": "sure"})


def call(name, arguments, id_="c1"):
    return NS(id=id_, function=NS(name=name, arguments=arguments))


def answer(content=None, tool_calls=None):
    return NS(choices=[NS(message=NS(content=content, tool_calls=tool_calls))])


async def test_tool_loop_runs_tools_then_answers(config):
    async with Agent(replace(config, research_enabled=True)) as bot:
        assert "read_url" in bot.system
        bot.research.call = AsyncMock(return_value="Source: https://8.8.8.8/\n\nfacts")
        create = AsyncMock(
            side_effect=[
                answer(tool_calls=[call("read_url", '{"url": "https://8.8.8.8/"}')]),
                answer("Per https://8.8.8.8/ (facts), yes."),
            ]
        )
        bot.llm = NS(chat=NS(completions=NS(create=create)))
        messages = [{"role": "system", "content": bot.system}, {"role": "user", "content": "q"}]
        assert (
            await bot._complete("provider/model", messages) == "Per https://8.8.8.8/ (facts), yes."
        )
        bot.research.call.assert_awaited_once_with("read_url", '{"url": "https://8.8.8.8/"}')
        assert messages[-1] == {
            "role": "tool",
            "tool_call_id": "c1",
            "content": "Source: https://8.8.8.8/\n\nfacts",
        }


async def test_tool_rounds_are_bounded(config):
    async with Agent(replace(config, research_enabled=True)) as bot:
        bot.research.call = AsyncMock(return_value="more")
        looping = answer("partial", [call("read_url", "{}")])
        create = AsyncMock(return_value=looping)
        bot.llm = NS(chat=NS(completions=NS(create=create)))
        result = await bot._complete("provider/model", [{"role": "user", "content": "q"}])
        assert result == "partial"
        assert create.await_count == TOOL_ROUNDS + 1
        assert create.await_args.kwargs["tool_choice"] == "none"
        assert bot.research.call.await_count == TOOL_ROUNDS


async def test_models_without_tool_support_still_answer(config):
    async with Agent(replace(config, research_enabled=True)) as bot:
        rejected = BadRequestError(
            "tools unsupported",
            response=httpx.Response(400, request=httpx.Request("POST", "https://x.invalid")),
            body=None,
        )
        create = AsyncMock(side_effect=[rejected, answer("from memory")])
        bot.llm = NS(chat=NS(completions=NS(create=create)))
        assert await bot._complete("provider/model", [{"role": "user", "content": "q"}]) == (
            "from memory"
        )
        assert "tools" not in create.await_args.kwargs


async def test_offline_prompt_without_research(config):
    async with Agent(config) as bot:
        assert bot.research is None
        assert "can't browse" in bot.system


async def test_images_are_opt_in_and_fall_back_for_text_models(config):
    image = NS(content_type="image/png", size=4, read=AsyncMock(return_value=b"\x89PNG"))
    other = NS(content_type="application/zip", size=4, read=AsyncMock())
    message = NS(attachments=[image, other])
    async with Agent(config) as bot:
        assert await bot._images(message) == []
    async with Agent(replace(config, image_input=True)) as bot:
        [part] = await bot._images(message)
        assert part["image_url"]["url"] == "data:image/png;base64,iVBORw=="
        other.read.assert_not_called()
        rejected = BadRequestError(
            "no images",
            response=httpx.Response(400, request=httpx.Request("POST", "https://x.invalid")),
            body=None,
        )
        create = AsyncMock(side_effect=[rejected, answer("text only")])
        bot.llm = NS(chat=NS(completions=NS(create=create)))
        messages = [{"role": "user", "content": [{"type": "text", "text": "a: look"}, part]}]
        assert await bot._complete("provider/model", messages, "a: look") == "text only"
        assert messages[0]["content"].startswith("a: look (The images could not be shown")
