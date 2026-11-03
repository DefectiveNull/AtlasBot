# Working on discord-agent

This is the community fork of AIBot. Preserve LICENSE.

The network clients are Discord, an owner-managed Bifrost gateway and, when
enabled, the research tools below. Do not restore Rivet's MCP, operator API,
broker, Hindsight, Studio, media-generation, voice, or object-storage
integrations. The opt-in research tools in `aibot/research.py` (Brave search,
public page/PDF reading, YouTube transcripts) are the only model-reachable
network paths beyond the gateway and Discord. Image input and `aibot/media.py`
(MEDIA_MODEL descriptions of attachments and profile pictures) download only
from Discord and decode audio/video locally. Keep all of them bounded and off
by default, and research fetches public-address-only. DMs and passive ingestion
are intentionally absent. Infrastructure credentials and delivery policy belong
in a separate owner-controlled repository.

Models come from Bifrost's OpenAI-compatible model-list endpoint; do not use
management APIs, direct provider credentials, or a hard-coded model allowlist.
All discovered IDs are selectable. Discovery does not guarantee each model
supports chat completion parameters; failures must remain clear and bounded.

Run `uv run --frozen ruff check .`, `uv run --frozen ruff format --check .`,
and `uv run --frozen pytest`. Tests use fakes and temporary SQLite files.
Never use a production Discord token or make live inference calls in tests.
Runtime requirements must match the hash-bearing, frozen `uv export --no-dev
--no-emit-project` output. Docker builds require an owner-approved base digest.
