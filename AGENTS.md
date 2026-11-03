# Working on Atlas

This is the community fork of AIBot. Preserve LICENSE. The repository is public
on GitHub: never commit bot tokens, API keys or other credentials.

The network clients are Discord, the configured OpenAI-compatible endpoint
(`API_BASE`) and, when enabled, the research tools below. The opt-in research
tools in `aibot/research.py` (Brave search, public page/PDF reading, YouTube
transcripts) and inline image input are the only model-reachable network paths
beyond the endpoint; keep them bounded, public-address-only and off by default.
DMs are intentionally absent. The only passive ingestion is the opt-in,
off-by-default `CONTEXT.md` buffer in `aibot/context.py`; keep it bounded by
age, count and size, consumed once, and honoring Discord deletes.

Models are discovered from the endpoint's `/models` list. Discovery does not
guarantee each model supports chat completion parameters; failures must remain
clear and bounded.

Run `uv run --frozen ruff check .`, `uv run --frozen ruff format --check .`,
and `uv run --frozen pytest`. Tests use fakes and temporary SQLite files.
Never use a production Discord token or make live inference calls in tests.
Runtime requirements must match the hash-bearing, frozen `uv export --no-dev
--no-emit-project` output.
