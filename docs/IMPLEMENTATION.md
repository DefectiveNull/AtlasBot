# Local implementation and verification — 2026-10-06

Candidate branch: `candidate/discord-agent`, based on
`899f8cb0c533a5e42bcc3e45f04eff5ef42a548c`. This directory is an isolated local
clone; `/Users/whiskey/code/aibot` and existing Rivet services are unchanged.
The candidate is being published to the private `whiskey.works/discord-agent`
repository, preserving the source ancestry and MIT license. Gitea Actions were
disabled before the first push. No contributor grant, credentials, live
infrastructure, CI or deployment is included. Local tests use fakes and temporary
SQLite files only.

## Implemented

The fork is deliberately reduced to a text-only community bot. Most of the
monolithic Rivet runtime was replaced with a smaller runtime rather than
leaving disabled privileged paths behind. Retained AIBot's cancellation-safe
priority inference gateway and its tests; preserved LICENSE byte-for-byte.

New runtime has guild/channel allowlists, no DMs/passive ingestion/bot replies,
bounded pending work and generation deadlines, per-channel serialized replies,
no allowed mentions, SQLite history and model preference persistence, logical
retention cleanup, minimal probe endpoints, explicit Discord proxy support,
and Bifrost model discovery. Model selection and history clearing enforce bot
owner or Manage Server authorization. Old integration configuration fails
startup. Existing unrelated database tables are rejected.

Removed: all tool/MCP dispatch, Gitea/Studio, Hindsight/privileged memory,
operator HTTP/MCP API, media generation, visual ingestion/object storage,
Kokoro/voice, web/research/weather/termpad, personas/Rivet identity/lurk,
legacy admin commands, broker/mint wiring, Compose stack, inherited workflow,
infra docs/scripts, and their unused dependencies/tests. No old data migrated.

This intentionally also removes generic attachment parsing and tool use. They
can be proposed later with their own bounded dependencies and network policy.

## Verification

- Ruff check and formatting passed; git diff whitespace check passed.
- 32 application/concurrency tests passed.
- 4 external owner contract tests passed with repository pytest configuration,
  conftest, and plugin auto-loading disabled.
- 5 owner gate/backup tests passed: stale/mismatched/missing/warning/failed
  evidence is rejected; a WAL-mode synthetic SQLite snapshot restores.
- pip-audit: 26 runtime packages, no known vulnerabilities at scan time.
- Gitleaks 8.30.1 official release archive checksum verified.
- Main history scan: 367 scanned commits, no findings. Expanded all-branch
  history scan: 485 scanned commits across the fetched source refs, no findings.
  Candidate working-tree scan: no findings. Reports are fully redacted.
- Frozen uv.lock and hash-bearing runtime requirements match export.
- LICENSE SHA-256 remains
  `1689b34f432727d4bddab31aa03fbae89b1b8c6c4b7f1f8276dfed465ce00ca9`.

These are local checks, not proof of deployment isolation or malicious-code
absence. No Linux image build/image vulnerability scan, live Discord test,
real Bifrost inference, deployed policy test or live backup restore was run.

## Verified identities and remaining work

Gitea user `defectivenull` exists (ID 97, active); organization `whiskey.works`
exists (ID 76). Private target repository ID 125 was created after discovery; inherited access
is only the Owners team, whose sole member is whiskey. Collaborator grant remains
pending protected-main controls.
Full remote history/branches were fetched only into the review snapshot.

Current Rivet key fingerprint and model discovery are recorded in the separate
owner delivery README. It returned 29 models. Actual quota/rate metadata and
the currently unused unrestricted main key's identity remain unverified;
authenticated administrative metadata is required before credential binding.
The user intends current Rivet limited key → new bot and unused unrestricted
main key → Rivet. No monetary budget assumptions or Luna changes are included.

The sibling `discord-agent-delivery` contains owner-controlled gate/network/
Flux/deployment sketches and exact authorization bundle. Its receipt validator
is only a consistency check after authenticating trusted evidence, not a full
CI controller. Gateways, isolated worker, controller, registry, credentials,
and first deployment remain to be implemented/provisioned after review.

Missing deployment inputs: new Discord application/token/owner and guild/channel
IDs, approved immutable image references, verified
key metadata, gateway enforcement details, and backup destination/retention.
Source Actions must remain disabled to avoid inherited organization runner access.
