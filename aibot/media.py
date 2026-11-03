"""Opt-in media understanding: MEDIA_MODEL turns images, video and audio into text.

Attachments and profile pictures are downloaded through Discord only. Audio and
video are decoded locally with PyAV (bundled FFmpeg), so no other network access
is needed. Descriptions reach the chat model as text, so the channel's model
does not need to accept images or audio itself.
"""

import asyncio
import base64
import io
import logging
import wave

import av
from openai import BadRequestError
from PIL import Image

log = logging.getLogger(__name__)

IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
MAX_ATTACHMENTS = 4
MAX_MEDIA_BYTES = 25 * 1024 * 1024
MAX_SECONDS = 180
VIDEO_FRAMES = 6
FRAME_EDGE = 768
AUDIO_RATE = 16000
DESCRIBE_TOKENS = 1500
DESCRIBE_PROMPT = (
    "You turn media into text for another assistant that cannot see or hear it. "
    "Describe what is actually there: subjects, setting, composition, style, colours, "
    "visible text (quoted exactly) and anything notable or unusual. Transcribe speech "
    "verbatim with [m:ss] timestamps and note the speakers' tone. Do not guess who a "
    "real person is from their face. If something is unclear, say so instead of guessing."
)


def kind(attachment) -> str | None:
    content = (attachment.content_type or "").split(";")[0].strip().lower()
    if content in IMAGE_TYPES:
        return "image"
    if content.startswith("video/"):
        return "video"
    if content.startswith("audio/"):
        return "audio"
    return None


def timestamp(seconds: float) -> str:
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def jpeg(image: Image.Image) -> bytes:
    image = image.convert("RGB")
    image.thumbnail((FRAME_EDGE, FRAME_EDGE))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=80)
    return buffer.getvalue()


def still(data: bytes) -> bytes:
    """First frame of an image (animated GIFs included), downscaled to JPEG."""
    with Image.open(io.BytesIO(data)) as image:
        return jpeg(image)


def audio_wav(data: bytes) -> tuple[bytes, float] | None:
    """Decode the first audio stream to 16 kHz mono WAV, at most MAX_SECONDS."""
    with av.open(io.BytesIO(data)) as container:
        if not container.streams.audio:
            return None
        resampler = av.AudioResampler(format="s16", layout="mono", rate=AUDIO_RATE)
        pcm, limit = bytearray(), MAX_SECONDS * AUDIO_RATE * 2
        for frame in container.decode(container.streams.audio[0]):
            for out in resampler.resample(frame):
                pcm += bytes(out.planes[0])[: out.samples * 2]
            if len(pcm) >= limit:
                break
        for out in resampler.resample(None):
            pcm += bytes(out.planes[0])[: out.samples * 2]
    pcm = pcm[:limit]
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(AUDIO_RATE)
        output.writeframes(pcm)
    return buffer.getvalue(), len(pcm) / (AUDIO_RATE * 2)


def video_frames(data: bytes) -> tuple[list[tuple[float, bytes]], float]:
    """Evenly spaced JPEG frames from the first MAX_SECONDS, with the video's duration."""
    with av.open(io.BytesIO(data)) as container:
        if not container.streams.video:
            return [], 0.0
        stream = container.streams.video[0]
        duration = float(container.duration / av.time_base) if container.duration else 0.0
        span = min(duration, MAX_SECONDS) if duration else MAX_SECONDS
        targets = [span * (i + 0.5) / VIDEO_FRAMES for i in range(VIDEO_FRAMES)]
        frames, next_target, first = [], 0, None
        for frame in container.decode(stream):
            moment = frame.time or 0.0
            if next_target >= len(targets) or moment > MAX_SECONDS:
                break
            if first is None:
                first = (moment, jpeg(frame.to_image()))
            if moment >= targets[next_target]:
                frames.append((moment, jpeg(frame.to_image())))
                while next_target < len(targets) and targets[next_target] <= moment:
                    next_target += 1
    # Clips shorter than the first target (or without a known duration) keep one frame.
    return frames or ([first] if first else []), duration


def image_part(data: bytes) -> dict:
    encoded = base64.b64encode(data).decode()
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}}


def audio_part(wav: bytes) -> dict:
    encoded = base64.b64encode(wav).decode()
    return {"type": "input_audio", "input_audio": {"data": encoded, "format": "wav"}}


class Media:
    def __init__(self, llm, model: str):
        self.llm = llm
        self.model = model

    async def describe(self, label: str, parts: list[dict]) -> str:
        response = await self.llm.chat.completions.create(
            model=self.model,
            max_tokens=DESCRIBE_TOKENS,
            messages=[
                {"role": "system", "content": DESCRIBE_PROMPT},
                {"role": "user", "content": [{"type": "text", "text": label}, *parts]},
            ],
        )
        text = response.choices[0].message.content
        if not isinstance(text, str) or not text.strip():
            raise ValueError("the media model returned no text")
        return text.strip()

    async def attachment(self, attachment, owner: str) -> str | None:
        """A bracketed description of one attachment, or a note saying why there isn't one."""
        media = kind(attachment)
        if media is None:
            return None
        name = getattr(attachment, "filename", "file")
        if attachment.size > MAX_MEDIA_BYTES:
            return f"[{media} {name} from {owner}: too large to examine (over 25 MB)]"
        try:
            data = await attachment.read()
            if media == "image":
                frame = await asyncio.to_thread(still, data)
                text = await self.describe(f"Image {name} shared by {owner}.", [image_part(frame)])
                return f"[image {name} from {owner}: {text}]"
            if media == "audio":
                return await self._audio(data, name, owner)
            return await self._video(data, name, owner)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Media description failed: %s", type(exc).__name__)
            return f"[{media} {name} from {owner}: could not be examined ({type(exc).__name__})]"

    async def avatar(self, user) -> str:
        asset = user.display_avatar.with_static_format("png").with_size(512)
        frame = await asyncio.to_thread(still, await asset.read())
        label = f"Discord profile picture of {user.display_name}."
        return await self.describe(label, [image_part(frame)])

    async def _audio(self, data: bytes, name: str, owner: str) -> str:
        decoded = await asyncio.to_thread(audio_wav, data)
        if decoded is None:
            return f"[audio {name} from {owner}: no audio track]"
        wav, seconds = decoded
        label = f"Audio {name} from {owner}, {timestamp(seconds)} long. Transcribe it."
        text = await self.describe(label, [audio_part(wav)])
        return f"[audio {name} from {owner}, {timestamp(seconds)}: {text}]"

    async def _video(self, data: bytes, name: str, owner: str) -> str:
        frames, duration = await asyncio.to_thread(video_frames, data)
        decoded = await asyncio.to_thread(audio_wav, data)
        stamps = ", ".join(timestamp(t) for t, _ in frames)
        label = (
            f"Video {name} from {owner}, {timestamp(duration)} long. "
            f"Frames at {stamps or 'none'}"
            + (", plus its audio track." if decoded else "; it has no audio.")
            + " Describe what happens and transcribe any speech."
        )
        parts = [image_part(jpeg_) for _, jpeg_ in frames]
        try:
            text = await self.describe(label, parts + ([audio_part(decoded[0])] if decoded else []))
        except BadRequestError:
            if not decoded or not parts:
                raise
            # The media model may accept images but not audio: describe frames only.
            text = await self.describe(label + " (Audio omitted.)", parts)
        return f"[video {name} from {owner}, {timestamp(duration)}: {text}]"
