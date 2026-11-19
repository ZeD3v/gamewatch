# GameWatch

Per-game server status and patch notes cog for [Red-DiscordBot](https://github.com/Cog-Creators/Red-DiscordBot) (V3.5+), posted to dedicated per-game channels.

| Game | Key | Status | Patch notes |
|---|---|---|---|
| League of Legends | `lol` | Riot CDN (EUNE default) | leagueoflegends.com |
| Teamfight Tactics | `tft` | Riot CDN (EUNE default) | teamfighttactics.leagueoflegends.com |
| VALORANT | `valorant` | Riot CDN (EU default) | playvalorant.com |
| Counter-Strike 2 | `cs2` | — | Steam news (appid 730) |
| Escape from Tarkov | `eft` | BSG status API | Steam news (appid 3932890) |
| ARC Raiders | `arc` | — | Steam news (appid 1808500) |
| Arena Breakout: Infinite | `abi` | — | Steam news (appid 2073620) |

No API keys are required for any source.

## Installation

Add this repository with Red's Downloader cog and install `gamewatch`:

```text
[p]load downloader
[p]repo add gamewatch https://github.com/ZeD3v/gamewatch
[p]cog install gamewatch gamewatch
[p]load gamewatch
```

To update later:

```text
[p]cog update gamewatch
```

## Setup

```text
[p]gamewatch news lol      #lol-patch-notes
[p]gamewatch status lol    #lol-status
[p]gamewatch role lol      @League

[p]gamewatch settings      # review everything
[p]gamewatch games         # list keys
```

Regions default to **EUNE** (`eun1`) for LoL and TFT and `eu` for VALORANT.
Change with `[p]gamewatch region <game> <region>`; the command validates the
region against Riot before saving.

## Commands

- `[p]gamewatch games` — supported games and their keys
- `[p]gamewatch news <game> [channel]` — set/clear the patch-notes channel
- `[p]gamewatch status <game> [channel]` — set/clear the status channel
- `[p]gamewatch role <game> [role]` — role pinged for that game
- `[p]gamewatch region <game> <region>` — Riot region (validated)
- `[p]gamewatch locations <game> [add|remove|clear|reset] [location]` — manage Riot status location allowlist
- `[p]gamewatch pingseverity <info|warning|critical>` — ping threshold
- `[p]gamewatch settings` — current configuration
- `[p]gamewatch check <game>` — live status on demand
- `[p]gamewatch latest <game>` — most recent patch notes
- `[p]gamewatch diag` — (owner) fetch health per source
- `[p]gamewatch forcecheck` — (owner) run both loops immediately
- `[p]gamewatch resetseen <game>` — (owner) clear dedupe history

All commands are hybrid — they work as both `[p]` text commands and slash
commands (with autocomplete and parameter descriptions) after `[p]slash sync`.
`news` and `status` accept text channels, announcement channels, and threads.

## Behaviour

- **Status loop** every 90s, **news loop** every 15min, both with ETag /
  `If-None-Match` conditional requests.
- **Debounce**: an incident must appear in 2 consecutive polls before it is
  announced, and a recovery is suppressed if the outage lasted under 2 minutes.
  Riot flips on brief blips.
- **Cold-start guard**: on first run per game, everything currently visible is
  recorded as seen without posting, so installing or restarting never dumps the
  last 20 patch notes into a channel.
- **Dedupe** by stable uid, capped at 80 entries per game per namespace
  (`news:{game}` / `status:{game}`).
- **Failure alerting**: after 12 consecutive failures for one source the bot
  owner is DMed, so a silently-dead scraper is not mistaken for "no news".
- Respects Red's `[p]embedset` settings and falls back cleanly to plain text
  where embeds are disabled or the bot lacks Embed Links permission.

## Privacy & External Services

- **End-User Data**: GameWatch does not persistently store any user data or
  metadata. It stores only per-guild channel, role, region, and location
  allowlist configuration plus global feed dedupe state via Red's `Config`.
- **External Requests**: The cog makes read-only HTTPS GET requests to public
  Riot CDN/news pages, the Steam Web API (`api.steampowered.com`), and
  Battlestate Games' public status API (`status.escapefromtarkov.com`). No API
  keys are required and no Discord user or server data is transmitted externally.
- **Support**: Report bugs or request games via
  [GitHub Issues](https://github.com/ZeD3v/gamewatch/issues).

## Location filter (status noise control)

Riot's VALORANT `eu` deployment is really EMEA and reports incidents for
Dubai, Bahrain, Cape Town and Tokyo alongside the European servers. Incidents
that name a game-server location outside the allowlist are suppressed
(deduped, never re-announced); incidents with no recognisable location always
announce (fail-open). Default allowlist (closest servers to Latvia): `warsaw`,
`stockholm`, `frankfurt`, `london`, `paris`.

```text
[p]gamewatch locations valorant              show
[p]gamewatch locations valorant add madrid
[p]gamewatch locations valorant remove paris
[p]gamewatch locations valorant clear        disable filtering
[p]gamewatch locations valorant reset        back to default
```

A region degraded only by filtered locations counts as healthy, so recovery
messages keep working while far-away incidents linger.

## Embeds

Patch notes are rendered as full Discord embeds, not link dumps:

- **Author line** — game name + official icon, linking to the source site.
- **Title** — the patch title, hyperlinked to the article.
- **Description** — Steam BBCode is *converted* to Discord markdown, so
  `[h2]`/`[list]` become real `##` headings and nested `-` bullets. CS2's
  `\[ MAPS ]` section markers are promoted to headings too.
- **Image** — Riot article hero art (requested as a 1200x675 crop from their
  Sanity CDN) or the first inline image from a Steam announcement; falls back
  to the Steam store header.
- **Accent colour** — Riot publish a per-article primary colour in their CMS,
  so each patch post matches its own artwork. Steam games use a fixed brand
  colour.
- **Fields** — version, published (as a native `<t:...:R>` relative timestamp
  that renders in each member's own locale/timezone), author.

Status embeds encode severity in **both** colour and a leading emoji, so
alerts stay readable for colour-blind members. Recovery posts include the
outage duration.

### Limits

`embeds.py` enforces every documented Discord limit client-side (title 256,
description 4096, field value 1024, 25 fields, 6000 total) and `fit_embed()`
trims before sending. If a send still fails with `50035 Invalid Form Body`,
`_send()` retries once as plain text so an update is never silently lost.

### Steam API trap

`maxlength` **must be 0**. Any non-zero value makes Steam pre-flatten the
announcement server-side: it strips every BBCode tag *and* joins lines with no
separator, turning "Cache" + "Fixed various gaps" into "CacheFixed various
gaps". Only `maxlength=0` returns the structured original.

## Source notes

These were established by probing the live APIs; they are the non-obvious parts.

**Riot status** uses the public CDN feed that Riot's own status page consumes:
`https://{host}.secure.dyn.riotcdn.net/channels/public/x/status/{region}.json`.
The documented `*.api.riotgames.com/lol/status/v4/platform-data` endpoint needs
a dev key that expires every 24h and is unusable for a long-running bot.
Region identifiers are **not** consistent between games: LoL/TFT take platform
IDs (`eun1`), VALORANT takes deployments (`eu`). The TFT host is the full word
`teamfighttactics`; `tft` 404s.

**Riot patch notes** have no RSS. The article list is parsed out of the
`__NEXT_DATA__` hydration blob on each news page. The `/_next/data/{buildId}/…`
JSON route is faster but `buildId` rotates on every site deploy, so the HTML
parse is used as the stable path.

**Steam `&tags=patchnotes` is lossy and must not be used as the only filter.**
Verified by diffing tagged vs untagged feeds:

- CS2 — tag reliable (30/30).
- ARC Raiders — **inverted**: cosmetic "Store Update" posts are tagged, the
  real "Live Update" gameplay patches are not.
- Escape from Tarkov — lossy; "Patch 1.1.5.0" and "Patch 1.1.0.0" untagged.
- Arena Breakout — publisher applies no tags at all.

So the cog fetches the untagged feed and filters client-side: keep if
`tag matches OR title matches`, then veto with `exclude_filter`.

**Escape from Tarkov status** comes from BSG's own undocumented but public
endpoints under `status.escapefromtarkov.com/api/`. Responses are served as
`text/html` despite being JSON, so parsing must not gate on content type.
Status integers map `0=OK, 1=Updating, 2=Unstable, 3=Down` (per
the-hideout/tarkov-data-manager). tarkov.dev's GraphQL `status` query returns
`GraphQL server unavailable` (HTTP 422) and is not used.

**No status API exists for ARC Raiders or Arena Breakout: Infinite.**
`status.arcraiders.com` and `status.embark-studios.com` are NXDOMAIN;
`arcraiders.statuspage.io` and `embarkstudios.statuspage.io` are inactive
placeholders. These two are news-only by design rather than guessing at
outages from player counts and producing false alarms.

## Development & Tests

Set up a local Python 3.11 environment with [uv](https://docs.astral.sh/uv/):

```bash
uv sync
uv run ruff check .
uv run pytest -v
```

Live upstream smoke tests (hit Riot, Steam, and BSG directly):

```bash
uv run python scripts/selftest.py      # also works with aiohttp alone (no redbot/discord.py)
uv run python scripts/rendertest.py
```

- `tests/` — offline pytest suite validating Red Downloader `info.json` schemas,
  cog lifecycle, hybrid commands & autocomplete, parser fixtures, embed limits,
  and the debounce/recovery/location-filter state machine.
- `scripts/selftest.py` — hits every live upstream and exits non-zero on HTTP
  errors or zero filtered items (catches upstream redesigns).
- `scripts/rendertest.py` — builds real embeds from live data and asserts every
  Discord embed limit, including synthetic overflow and ReDoS guards.
