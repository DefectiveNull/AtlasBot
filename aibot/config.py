"""Explicit deployment configuration; no legacy integrations or implicit .env load."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

# Default system prompt; edit aibot/PERSONA.md, or override with PROMPT.
PERSONA = Path(__file__).with_name("PERSONA.md").read_text(encoding="utf-8").strip()

REMOVED_PREFIXES = (
    "GITEA_",
    "STUDIO_",
    "MEDIAGEN_",
    "HINDSIGHT_",
    "PRIVILEGED_",
    "RIVET_",
    "VISION_",
    "KOKORO_",
    "OMP_",
    "TURING_",
    "SEARXNG_",
)
REMOVED_NAMES = {
    "DATABASE_URL",
    "DB_BACKEND",
    "OWNER_PII_REDACTIONS",
    "WEATHER_API_KEY",
    "SAY_ALLOWED_USER_IDS",
}


def ids(value: str) -> frozenset[int]:
    result = frozenset(int(x.strip()) for x in value.split(",") if x.strip())
    if any(x <= 0 or x >= 2**64 for x in result):
        raise ValueError("Discord IDs must be positive snowflakes")
    return result


def flag(value: str) -> bool:
    if value.strip().lower() not in {"", "0", "false", "no", "1", "true", "yes"}:
        raise ValueError(f"Expected a boolean, got {value!r}")
    return value.strip().lower() in {"1", "true", "yes"}


@dataclass(frozen=True)
class Config:
    discord_token: str = field(repr=False)
    api_base: str
    api_key: str = field(repr=False)
    model: str
    guild_ids: frozenset[int]
    application_id: int = 1557107805977059368
    discord_proxy: str | None = None
    channel_ids: frozenset[int] = frozenset()
    owner_id: int | None = None
    admin_ids: frozenset[int] = frozenset()
    prefix: str = "atlas"
    name: str = "Atlas"
    prompt: str = PERSONA
    db_path: str = "/data/history.sqlite3"
    max_concurrency: int = 4
    max_pending: int = 32
    reply_timeout: float = 120
    history_chars: int = 24000
    history_messages: int = 40
    retention_days: int = 30
    max_output_tokens: int = 2048
    # Recent untriggered channel messages sent as context; 0 disables catch-up.
    catchup_messages: int = 0
    # Opt-in model tools (read_url; web_search when a Brave key is set) and image input.
    research_enabled: bool = False
    brave_api_key: str = field(default="", repr=False)
    web_proxy: str | None = None
    image_input: bool = False

    def __post_init__(self):
        url = urlsplit(self.api_base)
        if (
            url.scheme not in {"https", "http"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError(
                "API_BASE must be an HTTP(S) base URL without credentials, query, or fragment"
            )
        if not self.discord_token or not self.api_key or not self.model or not self.guild_ids:
            raise ValueError(
                "Discord token, API key/proxy placeholder, model, and GUILD_IDS are required"
            )
        if not 0 < self.application_id < 2**64:
            raise ValueError("Invalid APPLICATION_ID")
        if any(type(x) is not int or not 0 < x < 2**64 for x in self.admin_ids):
            raise ValueError("Invalid BOT_ADMIN_IDS")
        if self.owner_id is not None and not 0 < self.owner_id < 2**64:
            raise ValueError("Invalid BOT_OWNER_ID")
        if not 1 <= self.max_concurrency <= self.max_pending <= 128:
            raise ValueError("Require 1 <= LLM_MAX_CONCURRENCY <= MAX_PENDING <= 128")
        if not 1 <= self.reply_timeout <= 600 or not 1 <= self.retention_days <= 365:
            raise ValueError("Timeout or retention out of bounds")
        if not 1000 <= self.history_chars <= 100000 or not 2 <= self.history_messages <= 200:
            raise ValueError("History limits out of bounds")
        if not 1 <= self.max_output_tokens <= 16384 or len(self.prompt) > 12000:
            raise ValueError("Output or prompt limit out of bounds")
        if not 0 <= self.catchup_messages <= 50 or len(self.name) > 32:
            raise ValueError("CATCHUP_MESSAGES or BOT_NAME out of bounds")
        if self.web_proxy and urlsplit(self.web_proxy).scheme not in {"http", "https"}:
            raise ValueError("WEB_PROXY_URL must be an HTTP(S) proxy URL")

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        legacy = sorted(
            k
            for k, v in env.items()
            if v and (k.startswith(REMOVED_PREFIXES) or k in REMOVED_NAMES)
        )
        if legacy:
            raise ValueError("Removed integration configuration: " + ", ".join(legacy))
        return cls(
            discord_token=env.get("DISCORD_BOT_TOKEN", ""),
            api_base=env.get("API_BASE", ""),
            api_key=env.get("API_KEY", "proxy"),
            model=env.get("MODEL", ""),
            guild_ids=ids(env.get("GUILD_IDS", "")),
            channel_ids=ids(env.get("CHANNEL_IDS", "")),
            application_id=int(env.get("APPLICATION_ID", cls.application_id)),
            discord_proxy=env.get("DISCORD_PROXY_URL") or None,
            owner_id=int(env["BOT_OWNER_ID"]) if env.get("BOT_OWNER_ID") else None,
            admin_ids=ids(env.get("BOT_ADMIN_IDS", "")),
            prefix=env.get("BOT_PREFIX", "atlas"),
            name=env.get("BOT_NAME", cls.name),
            prompt=env.get("PROMPT", cls.prompt),
            db_path=env.get("DB_PATH", cls.db_path),
            max_concurrency=int(env.get("LLM_MAX_CONCURRENCY", 4)),
            max_pending=int(env.get("MAX_PENDING", 32)),
            reply_timeout=float(env.get("REPLY_TIMEOUT", 120)),
            history_chars=int(env.get("HISTORY_CHARS", 24000)),
            history_messages=int(env.get("HISTORY_MESSAGES", 40)),
            retention_days=int(env.get("RETENTION_DAYS", 30)),
            max_output_tokens=int(env.get("MAX_OUTPUT_TOKENS", 2048)),
            catchup_messages=int(env.get("CATCHUP_MESSAGES", 0)),
            research_enabled=flag(env.get("RESEARCH_ENABLED", "")),
            brave_api_key=env.get("BRAVE_SEARCH_API_KEY", ""),
            web_proxy=env.get("WEB_PROXY_URL") or None,
            image_input=flag(env.get("IMAGE_INPUT", "")),
        )
