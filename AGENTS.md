# Working on Atlas

This is the community fork of AIBot. Preserve LICENSE. The repository is public
on GitHub: never commit bot tokens, API keys or other credentials.

The network clients are Discord, the configured OpenAI-compatible endpoint
(`API_BASE`) and, when enabled, the research tools below. The opt-in research
tools in `aibot/research.py` (Brave search, public page/PDF reading, YouTube
transcripts) are the only model-reachable network paths beyond the endpoint and
Discord. Image input and `aibot/media.py` (MEDIA_MODEL descriptions of
attachments and profile pictures) download only from Discord and decode
audio/video locally. Keep all of them bounded and off by default, and research
fetches public-address-only. DMs and passive ingestion are intentionally absent.

Models are discovered from the endpoint's `/models` list. Discovery does not
guarantee each model supports chat completion parameters; failures must remain
clear and bounded.

Run `uv run --frozen ruff check .`, `uv run --frozen ruff format --check .`,
and `uv run --frozen pytest`. Tests use fakes and temporary SQLite files.
Never use a production Discord token or make live inference calls in tests.
Runtime requirements must match the hash-bearing, frozen `uv export --no-dev
--no-emit-project` output.
