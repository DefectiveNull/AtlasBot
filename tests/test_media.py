import base64
import io
import json
import wave
from dataclasses import replace
from fractions import Fraction
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import av
import httpx
import numpy as np
import pytest
from openai import BadRequestError
from PIL import Image

from aibot.bot import Agent
from aibot.config import Config
from aibot.media import Media, audio_wav, kind, still, video_frames


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


def tone(container, rate=48000, seconds=2.0, codec="libopus"):
    stream = container.add_stream(codec, rate=rate)
    stream.layout = "mono"
    samples = int(rate * seconds)
    signal = (np.sin(np.arange(samples) * 440 * 2 * np.pi / rate) * 8000).astype(np.int16)
    frame_size, pts = 960, 0
    for start in range(0, samples, frame_size):
        frame = av.AudioFrame.from_ndarray(
            signal[None, start : start + frame_size], format="s16", layout="mono"
        )
        frame.rate, frame.pts = rate, pts
        pts += frame.samples
        for packet in stream.encode(frame):
            container.mux(packet)
    for packet in stream.encode(None):
        container.mux(packet)


def voice_message(seconds=2.0) -> bytes:
    buffer = io.BytesIO()
    with av.open(buffer, "w", format="ogg") as container:
        tone(container, seconds=seconds)
    return buffer.getvalue()


def clip(seconds=4, fps=10, audio=True) -> bytes:
    buffer = io.BytesIO()
    with av.open(buffer, "w", format="mp4") as container:
        video = container.add_stream("mpeg4", rate=fps)
        video.width, video.height, video.pix_fmt = 64, 48, "yuv420p"
        video.time_base = Fraction(1, fps)
        audio_stream = None
        if audio:
            audio_stream = container.add_stream("aac", rate=44100)
            audio_stream.layout = "mono"
        for index in range(seconds * fps):
            pixels = np.full((48, 64, 3), (index * 5) % 255, dtype=np.uint8)
            frame = av.VideoFrame.from_ndarray(pixels, format="rgb24")
            frame.pts = index
            for packet in video.encode(frame):
                container.mux(packet)
        for packet in video.encode(None):
            container.mux(packet)
        if audio_stream:
            rate, samples = 44100, 44100 * seconds
            signal = (np.sin(np.arange(samples) * 2 * np.pi * 220 / rate) * 8000).astype(np.int16)
            for start in range(0, samples, 1024):
                frame = av.AudioFrame.from_ndarray(
                    signal[None, start : start + 1024], format="s16", layout="mono"
                )
                frame.rate, frame.pts = rate, start
                for packet in audio_stream.encode(frame):
                    container.mux(packet)
            for packet in audio_stream.encode(None):
                container.mux(packet)
    return buffer.getvalue()


def gif() -> bytes:
    frames = [Image.new("RGB", (40, 30), color) for color in ("red", "blue")]
    buffer = io.BytesIO()
    frames[0].save(buffer, "GIF", save_all=True, append_images=frames[1:], duration=100)
    return buffer.getvalue()


def attachment(content_type, data=b"", size=None, filename="file"):
    return NS(
        content_type=content_type,
        size=len(data) if size is None else size,
        filename=filename,
        read=AsyncMock(return_value=data),
    )


def answer(content=None, tool_calls=None):
    return NS(choices=[NS(message=NS(content=content, tool_calls=tool_calls))])


def rejected():
    return BadRequestError(
        "unsupported",
        response=httpx.Response(400, request=httpx.Request("POST", "https://x.invalid")),
        body=None,
    )


def fake_llm(*results):
    create = AsyncMock(side_effect=list(results))
    return NS(chat=NS(completions=NS(create=create))), create


def test_kinds():
    assert kind(NS(content_type="image/png")) == "image"
    assert kind(NS(content_type="audio/ogg; codecs=opus")) == "audio"
    assert kind(NS(content_type="video/mp4")) == "video"
    assert kind(NS(content_type="image/svg+xml")) is None
    assert kind(NS(content_type=None)) is None


def test_voice_message_decodes_to_16k_mono_wav():
    wav, seconds = audio_wav(voice_message(2.0))
    assert 1.9 < seconds < 2.2
    with wave.open(io.BytesIO(wav)) as decoded:
        assert (decoded.getnchannels(), decoded.getsampwidth(), decoded.getframerate()) == (
            1,
            2,
            16000,
        )


def test_audio_is_capped(monkeypatch):
    monkeypatch.setattr("aibot.media.MAX_SECONDS", 1)
    _, seconds = audio_wav(voice_message(3.0))
    assert seconds == 1


def test_video_frames_audio_and_gif_stills():
    data = clip(seconds=4)
    frames, duration = video_frames(data)
    assert 3.5 < duration < 4.5
    assert len(frames) == 6
    assert [t for t, _ in frames] == sorted(t for t, _ in frames)
    assert all(Image.open(io.BytesIO(jpeg)).format == "JPEG" for _, jpeg in frames)
    assert audio_wav(data) is not None
    assert audio_wav(clip(seconds=1, audio=False)) is None
    assert Image.open(io.BytesIO(still(gif()))).size == (40, 30)


async def test_image_audio_and_failures_become_notes():
    llm, create = fake_llm(answer("A red square."), answer("[0:00] hello there"))
    media = Media(llm, "vision/model")
    image = attachment("image/gif", gif(), filename="art.gif")
    assert await media.attachment(image, "ana") == "[image art.gif from ana: A red square.]"
    assert create.await_args_list[0].kwargs["model"] == "vision/model"
    voice = attachment("audio/ogg", voice_message(), filename="voice-message.ogg")
    note = await media.attachment(voice, "ana")
    assert note.startswith("[audio voice-message.ogg from ana, 0:02: [0:00] hello there")
    part = create.await_args.kwargs["messages"][1]["content"][1]
    assert part["type"] == "input_audio" and part["input_audio"]["format"] == "wav"
    assert base64.b64decode(part["input_audio"]["data"])[:4] == b"RIFF"
    assert await media.attachment(attachment("application/zip", b"x"), "ana") is None
    huge = attachment("video/mp4", size=30 * 1024 * 1024)
    assert "too large" in await media.attachment(huge, "ana")
    huge.read.assert_not_called()
    broken = attachment("video/mp4", b"not a video", filename="v.mp4")
    assert "could not be examined" in await media.attachment(broken, "ana")


async def test_video_falls_back_to_frames_when_audio_is_rejected():
    llm, create = fake_llm(rejected(), answer("A fading grey screen."))
    note = await Media(llm, "vision/model").attachment(attachment("video/mp4", clip()), "bo")
    assert note.endswith("A fading grey screen.]")
    first, second = (call.kwargs["messages"][1]["content"] for call in create.await_args_list)
    assert any(part["type"] == "input_audio" for part in first)
    assert not any(part["type"] == "input_audio" for part in second)
    assert sum(part["type"] == "image_url" for part in second) == 6


def user(id_, name, display=None, avatar=b""):
    asset = NS(read=AsyncMock(return_value=avatar))
    asset.with_static_format = lambda fmt: asset
    asset.with_size = lambda size: asset
    return NS(id=id_, name=name, display_name=display or name, display_avatar=asset, bot=False)


async def test_media_is_opt_in_and_reads_replied_message(config):
    async with Agent(config) as bot:
        assert bot.media is None and "view_profile_picture" not in bot.system
        assert await bot._media_notes(NS(attachments=[attachment("image/png", gif())])) == []
    async with Agent(replace(config, media_model="vision/model")) as bot:
        assert "view_profile_picture" in bot.system
        bot.media.attachment = AsyncMock(side_effect=lambda a, owner: f"[{a.filename} by {owner}]")
        original = NS(
            author=user(2, "bo"), attachments=[attachment("image/png", filename="art.png")]
        )
        message = NS(
            author=user(1, "ana"),
            attachments=[attachment("audio/ogg", filename="voice.ogg")],
            reference=NS(resolved=original),
        )
        assert await bot._media_notes(message) == ["[art.png by bo]", "[voice.ogg by ana]"]


async def test_bare_voice_message_reply_triggers_only_with_media(config):
    def voice_reply():
        return NS(
            author=NS(bot=False),
            guild=NS(id=1),
            channel=NS(id=2),
            content="",
            attachments=[attachment("audio/ogg", filename="voice-message.ogg")],
            reference=NS(resolved=NS(author=NS(id=7))),
            reply=AsyncMock(),
        )

    async with Agent(config) as bot:
        bot._connection.user = NS(id=7)
        message = voice_reply()
        await bot.on_message(message)
        assert "text question" in message.reply.await_args.args[0]
    async with Agent(replace(config, media_model="vision/model")) as bot:
        bot._connection.user = NS(id=7)
        bot._reply = AsyncMock()
        await bot.on_message(voice_reply())
        for task in list(bot.pending):
            await task
        bot._reply.assert_awaited_once()


async def test_profile_pictures_are_limited_to_the_conversation(config):
    async with Agent(replace(config, media_model="vision/model")) as bot:
        bot._connection.user = user(7, "Atlas")
        ana, bo, cy = user(1, "ana", "Ana B"), user(2, "bo"), user(3, "cy")
        message = NS(author=ana, mentions=[bo], reference=None)
        people = bot._people(message)
        assert {p.id for p in people} == {1, 2, 7}
        bot.media.avatar = AsyncMock(return_value="A cat in sunglasses.")
        result = await bot._tool("view_profile_picture", json.dumps({"user": "@bo"}), people)
        assert result == "Profile picture of bo: A cat in sunglasses."
        assert "Ana B" in await bot._tool("view_profile_picture", '{"user": "ana b"}', people)
        missing = await bot._tool("view_profile_picture", json.dumps({"user": "cy"}), people)
        assert missing.startswith("No one called 'cy'")
        bot.media.avatar.assert_awaited()
        assert all(call.args[0] is not cy for call in bot.media.avatar.await_args_list)
        llm, create = fake_llm(answer("Nice."))
        bot.llm = llm
        await bot._complete("provider/model", [{"role": "user", "content": "q"}], None, people)
        names = [t["function"]["name"] for t in create.await_args.kwargs["tools"]]
        assert names == ["view_profile_picture"]


async def test_avatar_is_described_from_discord_asset():
    llm, create = fake_llm(answer("Pixel art fox."))
    media = Media(llm, "vision/model")
    png = io.BytesIO()
    Image.new("RGB", (8, 8), "orange").save(png, "PNG")
    assert await media.avatar(user(1, "ana", avatar=png.getvalue())) == "Pixel art fox."
    assert create.await_args.kwargs["messages"][1]["content"][1]["type"] == "image_url"
