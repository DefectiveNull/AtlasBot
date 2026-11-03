# Atlas (discord-agent)

Private community bot at `git.srvlab.io/whiskey.works/discord-agent`, based on
`whiskey/aibot@899f8cb0c533a5e42bcc3e45f04eff5ef42a548c`. The repository is
published with protected main and Actions disabled. Atlas runs as one bounded
replica on LKE, with separate history and credentials from the source repository.

This fork keeps Discord mention/prefix text replies, bounded inference
concurrency, per-channel model selection, and isolated SQLite history. It
removes the infrastructure-specific integrations and their executable code.
The inference concurrency gateway is retained from AIBot; the smaller runtime,
history schema, and command surface are new. This is not drop-in compatible
with Rivet's database. Start with a fresh volume, never a copy of Rivet's data.

## Behavior

- Guilds must be explicitly listed in `GUILD_IDS`; optionally restrict exact
  channel/thread IDs with `CHANNEL_IDS`. DMs and the bot's own messages are ignored.
- Only text messages mentioning the bot, replying to one of its messages,
  starting with `BOT_PREFIX`, or containing `BOT_NAME` as a whole word trigger
  replies. Attachments are not ingested. Untriggered conversation is not stored;
  it is sent to the model only if `CATCHUP_MESSAGES` is set above its default 0,
  in which case up to that many recent messages other than its own (8,000 characters at
  most) accompany each reply.
- `CONTEXT_ENABLED=true` instead keeps untriggered messages other than its own in a
  per-channel `CONTEXT_DIR/<guild>/<channel>/CONTEXT.md`. The next reply in that
  channel sends them to the model and then drops them; otherwise they expire
  after `CONTEXT_MINUTES` (default 120). Each file holds at most
  `CONTEXT_MESSAGES` entries (50) and `CONTEXT_CHARS` characters (8,000), each
  entry at most 500 characters, keeping the newest. Messages deleted on Discord
  are removed, `/clear` empties the channel's file, and expired entries are
  swept every 10 minutes. Edits are not tracked. It replaces catch-up when on.
- Webhooks and other apps (bots, integrations) trigger replies like people do,
  unless `AUTOMATED_TRIGGERS=false`. To stop two bots looping, they get at most 5
  replies per channel per 10 minutes and never receive error replies. Their text
  includes embed titles, descriptions and fields, and their header carries
  `kind=webhook` or `kind=app`.
- When a trigger replies to another message (not one of the bot's own) in the
  same channel, that message and its images (with `IMAGE_INPUT`) are quoted in
  the request as `[replying to ⟦speaker …⟧ text]`, up to 4,000 characters. If it
  is still in `CONTEXT.md` it is left out of the context block and dropped with
  the rest after the reply.
- Each person's message and catch-up line reaches the model with a header
  built from Discord's records, `⟦speaker id=… name="…"⟧`, and the system
  prompt says only that id identifies someone. `⟦` and `⟧` are replaced with
  `[` and `]` in all user-controlled text (messages, display names, catch-up,
  tool results) and in the stored reply, so the header
  cannot be forged. `aibot/PERSONA.md` names trusted people by id only.
- `/models` lists every model returned by Bifrost's `/models` API, cached for
  five minutes. `/model` selects one per channel. Both long and short model IDs
  work; Discord autocomplete shows up to 25 matching IDs.
- Every slash command requires Manage Server or the configured bot owner. Commands
  are also registered with Manage Server as their default permission, so Discord
  hides them from other members; server admins can grant access per user or role
  under Server Settings → Integrations (needed for `BOT_ADMIN_IDS` or
  `BOT_OWNER_ID` users without Manage Server). The bot still checks every call.
- `/nocontext`, `/nowebhooks` and `/short` are per-channel switches, all off by
  default. Each takes an optional
  `enabled` true/false and toggles without it. `/nocontext` stops keeping and
  sending recent channel messages and clears the channel's `CONTEXT.md`.
  `/nowebhooks` stops webhooks and other apps triggering replies; their
  messages still count as context. `/short` asks for at most two short
  paragraphs. Switches are stored in the history database.
- `/lock channel` restricts Atlas in that server to one channel, given by its
  ID (a pasted `#channel` mention also works). Outside it Atlas neither replies
  nor keeps messages as context, and locking clears the `CONTEXT.md` of the
  server's other channels; slash commands still work in every enabled
  channel. `/lock` alone shows the lock and `/unlock` removes it. The lock is
  stored in the history database.
- `/admin user` records the server admin running
  the bot in `HOST_PATH` (default `/data/HOST.md`), one line per server with
  the user's id, display name, who set it and when. `/admin` alone shows it. The
  host is added to the system prompt by id, like the speaker header.
- Only successful bot exchanges are stored. History is bounded by message
  count, character count, and retention; default retention is 30 days.
- Research tools are off unless `RESEARCH_ENABLED=true`. Then the model may
  call `read_url` (public web pages, PDFs up to 30 pages, YouTube transcripts)
  and, when `BRAVE_SEARCH_API_KEY` is set, `web_search` (Brave). At most 4
  rounds of 4 calls per reply; each result is capped at 12,000 characters and
  each download at 10 MB. Fetches go through `WEB_PROXY_URL` when set and refuse
  internal names (`*.local`, `*.svc`, single-label hosts), non-public addresses,
  credentials and ports other than 80/443, re-checking each redirect. That check
  cannot stop DNS rebinding, so egress policy remains the enforcing control.
  Tool exchanges are not stored; only the final reply is.
- `IMAGE_INPUT=true` sends up to 4 PNG/JPEG/WebP/GIF attachments (5 MB each),
  downloaded through Discord, to the model inline. A model that rejects images
  gets one retry with text only.
- No other tool execution, passive/lurk replies, DMs, voice, operator routes,
  or external memory are present.
- `/livez` and `/readyz` on port 8080 return status only, for cluster probes.

## Local development

Use Python 3.13+ and `uv sync --frozen`. Run `uv run --frozen pytest` and
`uv run --frozen ruff check .`. Unit tests never connect to Discord or Bifrost.
Configuration names are documented in `.env.example`; the app deliberately
does not load `.env` files. Export configuration explicitly when running
`uv run --frozen python aibot.py` with a separately approved test bot.

For Discord, create a separate bot application, enable Message Content Intent,
and grant only View Channels, Send Messages, Read Message History and Attach
Files (the model list is a text attachment) in approved channels. Add Send
Messages in Threads only if threads are enabled. No Administrator, member,
presence, voice, moderation, webhook, or role-management privileges are needed.

`API_BASE` points directly to the owner's Bifrost OpenAI-compatible endpoint.
A limited key is mounted from a Kubernetes Secret; it is currently shared with
Rivet, so their quota is shared. There is no separate inference proxy service.
All models visible to that key are offered. The default model is configurable.

## Delivery

The owner-controlled delivery configuration lives separately from this fork.
There is intentionally no privileged publishing workflow here. Member edits,
including workflow edits, cannot authorize production promotion. A PR to protected
`main` is an input to the external build/scan controller; only its trusted
passing verdict for the exact image digest can update deployment.
Keep Gitea Actions disabled on this application repository, so adding a workflow
cannot claim the organization's existing trusted runner. The external controller
polls open PRs every five minutes, evaluating at most one per run.

The Dockerfile requires `PYTHON_BASE` to be supplied as a pinned image digest.
It installs hash-verified runtime requirements and runs UID/GID 10001. The
deployment supplies a writable `/data`, read-only root, and restricted network.

## Limitations and rollout checks

Atlas has connected to Discord and answered a user mention. Further model
compatibility and backup/restore checks remain separate operational work.
Model discovery is authorization-aware
metadata, not a promise all models support the same chat parameters.

Use one replica with Recreate for the SQLite writer. Back up with a trusted
SQLite backup operation, never by copying a live database file alone. Keep
backup credentials outside this container. Model changes persist; incompatible
schema changes require a separate migration/restore decision, not just an
image rollback. Do not expose this bot to another tenant's state.

## Contributions and release boundary

This private repository preserves AIBot ancestry; it is not registered as a
public Gitea fork. Submit ordinary code changes as PRs to protected `main`. The
owner-controlled service runs the sanity scan and automated review,
then automatically merges and deploys the exact approved image digest. Ordinary
code does not require routine human review. CI, build, deployment and security
control changes require current owner approval. A failed or inconclusive gate
holds the change; it never substitutes a warning for release authorization.

Gitea Actions remain disabled on this repository. Do not enable them or add
production credentials here: existing organization runners have host access.
Required status names alone are not proof of a trusted result; the external
merger must verify evidence identity and current PR/base/image/policy hashes.
The separate owner Flux repository controls namespace, egress, secrets, storage
and deployment policy. Contributor access never includes those controls.

## Discord identity

Bot name: **Atlas**. Application ID: `1557107805977059368`. Startup verifies that
the configured token belongs to this application before registering commands.
The default trigger is `atlas` (mentions also work), and the default prompt names
Atlas. Repository name remains `whiskey.works/discord-agent`.

The supplied interactions public key is not a bot token and is not needed here.
The outbound Discord gateway handles
commands/events, so this implementation needs no interactions HTTP endpoint,
public ingress or request-signature key configuration. No bot token is present.

The owner installed the bot token directly into a protected Kubernetes Secret.
Credentials never belong in chat, Git, or contributor build jobs. Discord and
Bifrost use direct outbound TLS with restricted runtime egress; no separate
Discord proxy or private CA is required.

Atlas runtime administrators are explicitly configured by numeric `BOT_ADMIN_IDS`:
Ian `338456610294988800` (verified existing Rivet owner configuration) and Null
`187259462782943233` (user supplied). When set, this exact set controls `/model`
and `/clear`; Manage Server alone does not add other bot administrators. This
runtime permission grants no CI, repository-security, secret or deployment
control. The legacy single owner/Manage Server fallback applies only when the
explicit set is empty.
