# GameWatch

Per-game server status and patch notes cog for [Red-DiscordBot](https://github.com/Cog-Creators/Red-DiscordBot) (V3.5+, Python 3.10+), posted to dedicated per-game channels.

| Game | Key | Status | Patch notes |
|---|---|---|---|
| League of Legends | `lol` | Riot CDN (`eun1` default) | Riot PCS v2.0 JSON (`en-us`, `tags/patch-notes`) + HTML fallback |
| Teamfight Tactics | `tft` | Riot CDN (`eun1` default) | Riot PCS v2.0 JSON (`en-us`, `tags/patch-notes`) + HTML fallback |
| VALORANT | `valorant` | Riot CDN (`eu` default) | Riot PCS v2.0 JSON (`en-us`, `tags/patch-notes`) + HTML fallback |
| Counter-Strike 2 | `cs2` | Steam Web API *(optional Steam API key)* | Steam Partner Events (appid `730`) |
| Escape from Tarkov | `eft` | BSG status API (services + datacenters + global + messages) | BSG official patch-notes JSON API (`escapefromtarkov.com`) |
| ARC Raiders | `arc` | — | Steam Partner Events (appid `1808500`) |
| Arena Breakout: Infinite | `abi` | — | Level Infinite GPTS CMS JSON API (`arenabreakoutinfinite.com`) |
| ROB RUN | `robrun` | — | Steam Partner Events (appid `4262460`) |

No extra `pip` dependencies are required, and no API keys are required for patch notes or Riot/EFT status. Counter-Strike 2 server status is optionally unlocked with a free Steam Web API key via Red's `[p]set api`.

## Installation

Add this repository with Red's Downloader cog and install `gamewatch`:

```text
[p]load downloader
[p]repo add gamewatch https://github.com/ZeD3v/gamewatch
[p]cog install gamewatch gamewatch
[p]load gamewatch
```

To enable `/gamewatch` slash commands (Red 3.5 keeps third-party cog slash commands disabled until enabled by the bot owner):

```text
[p]slash enablecog gamewatch
[p]slash sync
```

To update later:

```text
[p]cog update gamewatch
```

## Quick Setup

Configure channels and optional role pings per game (you can also use the `[p]gw` shorthand alias for text commands):

```text
[p]gamewatch news lol #lol-patch-notes
[p]gamewatch status lol #lol-status
[p]gamewatch role lol @League
```

Review your configuration or check available game keys:

```text
[p]gamewatch settings
[p]gamewatch games
```

Test immediately without waiting for the next polling cycle:

```text
[p]gamewatch latest lol
[p]gamewatch check lol
```

> **Note on first setup:** On the first news poll for a game, currently visible patch notes are recorded silently so your channel is not flooded with old patches. Only **new** updates published after setup are announced automatically.

### Regions

Riot status feeds require a region ID and default to **`eun1`** (EUNE) for LoL/TFT and **`eu`** for VALORANT. Change per server with `[p]gamewatch region <game> <region>` — the command offers per-game region autocomplete, catches retired shards (`ph2`/`th2` → `sg2`), and validates live against Riot before saving. Status embeds link directly to your configured region on `status.riotgames.com`.

### Optional: Steam Web API Key (CS2 Status)

Patch notes for CS2 work without any key. To also enable live Counter-Strike 2 server status (`[p]gamewatch status cs2` and `[p]gamewatch check cs2`), the bot owner can register a free key at [steamcommunity.com/dev/apikey](https://steamcommunity.com/dev/apikey) and store it with Red's built-in shared API tokens (**run in DM with the bot** or use `[p]set api steam` for a private modal):

```text
[p]set api steam api_key,<your_steam_web_api_key>
```

Once configured, CS2 status monitors Valve's core services (`SessionsLogon`, `SteamCommunity`, `Matchmaking Scheduler`), live matchmaking metrics, and global datacenters immediately — no cog reload required. Keys are never logged or exposed in error messages.

### Discord Permissions

In each configured channel or thread, grant the bot:

- **View Channel** and **Send Messages** (or **Send Messages in Threads** for threads) — required to post updates.
- **Embed Links** — recommended for rich embeds (falls back cleanly to plain text if missing or disabled via `[p]embedset`).
- **Role pings** — either make the configured role mentionable or grant the bot **Mention Everyone** (`@everyone` itself cannot be set as a ping role).

`news` and `status` warn you immediately during setup if the bot is missing permissions in the chosen channel.

## Commands

All commands are server-only and hybrid — they work as `[p]gamewatch ...`, `[p]gw ...`, or `/gamewatch ...` (with autocomplete and parameter descriptions once slash commands are enabled).

| Command | Access | Description |
|---|---|---|
| `[p]gamewatch games` | Everyone | List supported games and their keys |
| `[p]gamewatch settings` | Everyone | Show the current configuration for this server |
| `[p]gamewatch check <game>` | Everyone | Show live server status on demand (10s channel cooldown) |
| `[p]gamewatch latest <game>` | Everyone | Show the most recent patch notes on demand (10s channel cooldown) |
| `[p]gamewatch news <game> [channel]` | Admin / Manage Server | Set the patch-notes channel or thread (omit channel to disable) |
| `[p]gamewatch status <game> [channel]` | Admin / Manage Server | Set the status-alerts channel or thread (omit channel to disable) |
| `[p]gamewatch role <game> [role]` | Admin / Manage Server | Set the role pinged for that game (omit role to clear) |
| `[p]gamewatch region <game> <region>` | Admin / Manage Server | Set the Riot region (`lol`, `tft`, `valorant`; validated live) |
| `[p]gamewatch locations <game> [add\|remove\|clear\|reset] [location]` | Admin / Manage Server | Manage the opt-in status location allowlist (`lol`, `tft`, `valorant`, `cs2`) |
| `[p]gamewatch pingseverity <info\|warning\|critical>` | Admin / Manage Server | Minimum status incident severity that pings the role (default: `warning`) |
| `[p]gamewatch diag` | Bot Owner | Show fetch health, HTTP status, latency, API key status, and validator cache |
| `[p]gamewatch forcecheck` | Bot Owner | Run both status and news loops immediately |
| `[p]gamewatch resetseen <game>` | Bot Owner | Clear dedupe & status state for a game (news silently re-seeds on next poll) |

Notes:
- `news` and `status` accept text channels, announcement channels, and threads.
- `pingseverity` applies only to status incident alerts (default: `warning`). Patch notes always ping the configured role when set; outage recovery messages never ping.

## Behaviour

- **Bounded concurrent polling**: status loop every 90s, news loop every 15min,
  fetching independent sources concurrently (`Semaphore(6)`) before applying
  state updates sequentially so one slow upstream never stalls other games.
- **Resilient HTTP transport**: supports both `ETag` (`If-None-Match`) and
  `Last-Modified` (`If-Modified-Since`) conditional requests, bounded 4 MiB
  response reads, and transient retries with exponential backoff, jitter, and
  capped RFC 9110 `Retry-After` handling on network/timeout/429/5xx errors.
- **Debounce & scheduled maintenance**: active incidents and in-progress
  maintenances require 2 consecutive bad polls before announcing, and recovery
  is suppressed for blips under 2 minutes. Scheduled future maintenances are
  debounced independently as informational notices without falsely marking the
  region down or emitting bogus recovery messages when they clear.
- **Cold-start & upgrade migration guard**: on first run per game, currently
  visible items are recorded as seen without posting. Upgrading from older
  source UID namespaces silently migrates (`lol` `/en-gb/` → `/en-us/` in-place;
  disjoint namespaces silently re-seed once) so upgrades never spam old patches.
- **Dedupe** by stable uid, capped at 80 entries per game per namespace
  (`news:{game}` / `status:{game}`).
- **Failure alerting**: after 12 consecutive failures for one source the bot
  owner is DMed, so a silently-dead source is not mistaken for "no news".
- Respects Red's `[p]embedset` settings and falls back cleanly to plain text
  where embeds are disabled or the bot lacks Embed Links permission.

## Privacy & External Services

- **End-User Data**: GameWatch does not persistently store any user data or
  metadata. It stores only per-guild channel, role, region, and location
  allowlist configuration plus global feed dedupe state via Red's `Config`.
- **External Requests**: The cog makes requests to official public game
  endpoints only (no Discord user or server data is transmitted externally;
  only the optional CS2 status endpoint uses the bot owner's Steam Web API
  key from Red's `[p]set api`):
  - `GET` to Riot CDN status (`*.secure.dyn.riotcdn.net`), Riot Publishing
    Content Service (`content.publishing.riotgames.com`), and Riot news HTML
    fallback (`leagueoflegends.com`, `teamfighttactics.leagueoflegends.com`,
    `playvalorant.com`).
  - `GET` to Steam Store Partner Events (`store.steampowered.com/events/ajaxgetpartnereventspageable/`)
    and, when a Steam Web API key is configured, Valve's CS2 status endpoint
    (`api.steampowered.com/ICSGOServers_730/GetGameServersStatus/v1/`).
  - `GET` to Battlestate Games' public status API (`status.escapefromtarkov.com/api/*`)
    and official patch-notes API (`www.escapefromtarkov.com/site/api/v1/news-list/2/en`).
  - `POST` (read-only JSON query) to Level Infinite's public GPTS CMS API
    (`sg-community.playerinfinite.com`) for Arena Breakout: Infinite announcements.
- **Support**: Report bugs or request games via
  [GitHub Issues](https://github.com/ZeD3v/gamewatch/issues).

## Location filter (optional status noise control)

Riot's VALORANT `eu` deployment covers all of EMEA and reports individual
game-server incidents for locations such as Dubai, Bahrain, Cape Town, and
Tokyo alongside European servers (London, Paris, Frankfurt, Madrid, Warsaw,
Stockholm, Istanbul). Counter-Strike 2's status feed likewise reports ~30
global datacenters (`EU North`, `EU West`, `Poland`, `US Northeast`, `Peru`,
etc.).

Location filtering is **disabled by default** so servers in every region
receive all status alerts out of the box. If you only care about nearby
servers or datacenters, add an allowlist — incidents naming a location outside
your allowlist are suppressed, while region-wide/core-service incidents with
no specific location always announce (fail-open).

```text
[p]gamewatch locations valorant
[p]gamewatch locations valorant add warsaw, stockholm, frankfurt, london, paris
[p]gamewatch locations cs2 add eu north, eu east, eu west, poland
[p]gamewatch locations valorant add madrid
[p]gamewatch locations valorant remove paris
[p]gamewatch locations valorant clear
[p]gamewatch locations valorant reset
```

Multiple locations can be added or removed at once separated by commas. A
region degraded only by filtered locations counts as healthy for your server,
so recovery messages still work cleanly while distant incidents linger.

## Embeds

Patch notes are rendered as full Discord embeds, not link dumps:

- **Author line** — game name + official icon, linking to the source site.
- **Title** — the patch title, hyperlinked to the canonical article.
- **Description** — Steam BBCode and BSG/Level Infinite CMS HTML are converted
  to Discord markdown (`##`/`###` headings, nested bullets, emphasis, tables),
  using stdlib `html.parser` with zero extra dependencies. CS2's `\[ MAPS ]`
  section markers are promoted to headings too.
- **Image** — Riot article hero art (1200x675 Sanity CDN crop), Steam 800x450
  16:9 Partner Event capsule art, or official BSG/ABI article artwork; falls
  back to inline article images and then the store header.
- **Accent colour** — Riot publish a per-article primary colour in their CMS,
  so each patch post matches its own artwork. Other games use a fixed brand
  colour.
- **Fields** — version, published (as a native `<t:...:R>` relative timestamp
  that renders in each member's own locale/timezone), author.

Status embeds encode severity in **both** colour and a leading emoji, distinguish
`Scheduled Maintenance` from `Maintenance in Progress`, and display both EFT
services and regional datacenters. Recovery posts include the outage duration.

### Limits

`embeds.py` enforces every documented Discord limit client-side (title 256,
description 4096, field value 1024, 25 fields, 6000 total) and `fit_embed()`
trims before sending. If a send still fails with `50035 Invalid Form Body`,
`_send()` retries once as plain text so an update is never silently lost.

## Source notes

These were established by probing the live APIs and inspecting official frontend
bundles; they are the non-obvious parts.

**Riot status** uses the exact public CDN feed that `status.riotgames.com`
consumes (`https://{host}.secure.dyn.riotcdn.net/channels/public/x/status/{region}.json`,
`Cache-Control: max-age=60`, true `304` on both `If-None-Match` and
`If-Modified-Since`). Matching Riot's frontend bundle, updates are filtered to
`publish != false` and `"riotstatus" in publish_locations` (dropping in-client
tips and `riotclient`-only banners), expired `archive_at` and `"complete"`
maintenances are dropped, updates are ordered explicitly by timestamp descending,
and `uid` keys off the selected update rather than `raw.updated_at`. Region IDs
differ between LoL (`kr1`, `ru1`, `pbe`) and TFT (`kr`, `ru`, `pbe1`), while
`ph2`/`th2` were retired into `sg2`.

**Riot patch notes** use Riot's public Publishing Content Service v2.0 JSON API
(`content.publishing.riotgames.com/publishing-content/v2.0/public/channel/...`),
cutting wire transfer by 58–85% over Netlify's Next.js HTML pages (which ignore
`If-None-Match`) with 100% item parity and automatic Next.js HTML fallback. LoL
uses `en-us` (avoiding `en-gb` 404s and stray TFT cross-posts) and TFT uses
`tags/patch-notes/` (recovering patches evicted by `game-updates/`'s 200-card
cap).

**Steam Partner Events (`cs2`, `arc`, `robrun`)** uses
`store.steampowered.com/events/ajaxgetpartnereventspageable/` — the same backend
powering the Steam Store News Hub and `counter-strike.net/news/updates`. Unlike
`ISteamNews/GetNewsForApp/v2` (which edge-caches for 30–56 minutes, mixes in
third-party press unless `&feeds=steam_community_announcements` is passed, and
only auto-tags `event_type == 12` as `patchnotes`), Partner Events exposes
Valve's full `event_type` enum (`12` Small Update, `13` Regular Update, `14`
Major Update), 800×450 16:9 capsule art, and direct canonical `/news/app/{appid}/view/{event_gid}`
URLs. Items are sorted client-side by `announcement_body.posttime` DESC because
the server orders by `rtime32_start_time` DESC. On ARC Raiders, `"Store Update"`
titles use a body-aware guard so real 7–11 KB gameplay patches pass while pure
cosmetic shop rotations (0.2–2.7 KB) stay excluded.

**Escape from Tarkov** fetches all four `status.escapefromtarkov.com/api/`
endpoints (`/services`, `/global/status`, `/message/list`, `/datacenter/list`)
in one parallel `asyncio.gather()`, maps `message.type` (`0..3`) and ignores
BSG's `"Access denied"` `global.message` quirk. Patch notes come directly from
BSG's public JSON API (`escapefromtarkov.com/site/api/v1/news-list/2/en?page=1`,
`type_id=2` = Patch Notes), delivering notes 6–20 hours ahead of Steam cross-posts
with full HTML inline. (`api.tarkov.dev/graphql` returns HTTP 422.)

**Arena Breakout: Infinite** uses Level Infinite's official GPTS CMS JSON API
(`sg-community.playerinfinite.com`, `X-GameId: 30048`, `X-Source: pc_web`,
`secondary_label_id: 1318` Announcement) with lazy `GetContentInfoById` detail
fetches for unseen items only — MoreFun Studios stopped cross-posting routine
Live Updates to Steam after July 2026.

**Counter-Strike 2 status (optional Steam Web API key)** uses Valve's official
`ICSGOServers_730/GetGameServersStatus/v1/` endpoint (configured via Red's
`[p]set api steam api_key,<key>`). Valve's `capacity: "full"` means full
provisioned capacity is online and healthy (not overloaded), and normal traffic
loads (`idle`/`low`/`medium`/`high`) are healthy too. Alerts fire on degraded
or offline core services (`SessionsLogon`, `SteamCommunity`,
`Matchmaking Scheduler`) and offline datacenters (which support per-guild
location filtering via `[p]gamewatch locations cs2`), while peak-hour capacity
or load dips surface on `[p]gamewatch check cs2` alongside live matchmaking
stats without spamming channels.

**No public status API exists for ARC Raiders, Arena Breakout: Infinite, or
ROB RUN.** Embark, MoreFun Studios, and Kefir publish no official status hosts
or Steam Web API status interfaces (`GetSDRConfig` is static PoP topology only,
and `steamstat.us` explicitly forbids third-party scraping).

## Development & Tests

Set up a local Python 3.11 environment with [uv](https://docs.astral.sh/uv/):

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pytest -v
```

Live upstream smoke tests (hit Riot, Steam, BSG, and Level Infinite directly;
optionally set `STEAM_API_KEY` to also test keyed CS2 status):

```bash
uv run python scripts/selftest.py      # also works with aiohttp alone (no redbot/discord.py)
uv run python scripts/rendertest.py
STEAM_API_KEY=... uv run python scripts/selftest.py
```

- `tests/` — offline pytest suite validating Red Downloader `info.json` schemas,
  HTTP transport/retry/size-guard, cog lifecycle, hybrid commands & autocomplete,
  parser fixtures for all 8 games, BBCode/HTML embed limits, UID migration, and
  the debounce/scheduled-maintenance/recovery/location-filter state machine.
- `scripts/selftest.py` — hits every live upstream and exits non-zero on HTTP
  errors or zero filtered items (catches upstream redesigns).
- `scripts/rendertest.py` — builds real embeds from live data and asserts every
  Discord embed limit, including synthetic overflow and BBCode/HTML ReDoS guards.
