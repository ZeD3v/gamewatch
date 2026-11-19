"""Static game definitions for GameWatch.

Every endpoint referenced here was probed live before being added. Notes on the
quirks are kept next to the definition they apply to, because they are the kind
of thing that silently rots.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Riot status (public CDN, no API key)
# ---------------------------------------------------------------------------
# This is the feed riotgames' own status page consumes. The official
# platform-data endpoint on *.api.riotgames.com requires a dev API key which
# expires every 24h, so it is unusable for a long-running bot.
#
# Region identifiers are NOT consistent between games:
#   LoL / TFT -> platform ids  (eun1, euw1, na1, ...)
#   VALORANT  -> deployments   (eu, na, ap, br, kr, latam, pbe)
# Requesting teamfighttactics/.../eu.json 404s; it must be eun1.json.
RIOT_STATUS_URL = "https://{host}.secure.dyn.riotcdn.net/channels/public/x/status/{region}.json"

RIOT_SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}

# ---------------------------------------------------------------------------
# Steam news (public, no API key)
# ---------------------------------------------------------------------------
# WARNING: maxlength must be 0. Any non-zero value makes Steam pre-flatten the
# announcement server-side - it strips every BBCode tag AND joins lines with no
# separator, so "Cache" + "Fixed various gaps" becomes "CacheFixed various gaps".
# Only maxlength=0 returns the structured BBCode we convert to markdown.
STEAM_NEWS_URL = (
    "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
    "?appid={appid}&count={count}&maxlength=0"
)

# ---------------------------------------------------------------------------
# Escape from Tarkov status (BSG, undocumented but public)
# ---------------------------------------------------------------------------
# Responses are served with Content-Type: text/html despite being JSON, so the
# fetcher must not gate on content type.
# Status ints map per the-hideout/tarkov-data-manager's statusMap.
EFT_SERVICES_URL = "https://status.escapefromtarkov.com/api/services"
EFT_GLOBAL_URL = "https://status.escapefromtarkov.com/api/global/status"
EFT_MESSAGES_URL = "https://status.escapefromtarkov.com/api/message/list"
EFT_STATUS_MAP = {0: "OK", 1: "Updating", 2: "Unstable", 3: "Down"}

# ---------------------------------------------------------------------------
# Game table
# ---------------------------------------------------------------------------
# news_kind:   "steam" | "riot"
# status_kind: "riot" | "eft" | None   (None = provider publishes no status API)
#
# Steam filtering, learned the hard way by diffing tagged vs untagged feeds:
# the server-side &tags=patchnotes filter is LOSSY and must never be used as
# the only signal.
#   CS2  - tag is reliable (30/30 updates tagged).
#   ARC  - INVERTED: "Store Update" (cosmetics) is tagged, "Live Update" (the
#          real gameplay patch) is not. Tag-only filtering posts shop rotations
#          and drops every actual patch.
#   EFT  - lossy: "Patch 1.1.5.0" and "Patch 1.1.0.0" are untagged.
#   ABI  - publisher applies no tags whatsoever.
# So: always fetch the untagged feed, then include an item if the tag matches
# OR the title matches, and drop it if the exclude pattern matches.
# steam_tag:     optional OR-signal, checked client-side.
# title_filter:  optional OR-signal.
# exclude_filter: veto, applied after the OR-signals.
# ---------------------------------------------------------------------------
# Branding assets. Every URL below was fetched and confirmed to return a real
# image with a 200 before being hardcoded.
#   - Riot icons come from their own Sanity CDN <link rel="icon">. LoL's is an
#     SVG, so ?fm=png is appended: Discord does not render image/svg+xml in an
#     embed icon slot.
#   - Steam community icons are the small square app icons; the header art is
#     the 460x215 store banner. EFT (appid 3932890) has NO plain header.jpg -
#     it 404s - so its full alt-asset URL is used instead.
STEAM_ICON = "https://shared.fastly.steamstatic.com/community_assets/images/apps/{appid}/{h}.jpg"
STEAM_HEADER = (
    "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/{appid}/header.jpg"
)
_RGPUB = "https://cmsassets.rgpub.io/sanity/images/dsfx7636"
ICON_LOL = f"{_RGPUB}/news_live/d3b7bd9decb1e1672dcb80be4f8bc1aa05490dc1-110x70.svg?fm=png&w=128"
ICON_VAL = (
    f"{_RGPUB}/news/cbf4460132cdfeb2a97fad5f9dd25ba0bc058f76-128x128.png?w=128&h=128&fit=crop"
)
ICON_TFT = (
    f"{_RGPUB}/news/dd3cb71804eae400eb69fa6560254878beb1417f-2105x2190.png?w=128&h=128&fit=crop"
)

GAMES = {
    "lol": {
        "name": "League of Legends",
        "emoji": "\U0001f5e1\ufe0f",
        "colour": 0xC89B3C,
        "status_kind": "riot",
        "riot_host": "lol",
        "default_region": "eun1",
        "news_kind": "riot",
        "news_url": "https://www.leagueoflegends.com/en-gb/news/tags/patch-notes/",
        "news_base": "https://www.leagueoflegends.com",
        "title_filter": r"(?i)patch.*notes",
        "site": "https://www.leagueoflegends.com/en-gb/news/tags/patch-notes/",
        "status_url": "https://status.riotgames.com/lol?region=eun1",
        # Sanity CDN favicon. ?fm=png converts the SVG, because Discord will
        # not render an image/svg+xml embed icon.
        "icon": ICON_LOL,
        "art": None,
        "source_label": "Riot Games",
        "status_label": "Riot status",
    },
    "tft": {
        "name": "Teamfight Tactics",
        "emoji": "\u265f\ufe0f",
        "colour": 0x1E90FF,
        "status_kind": "riot",
        "riot_host": "teamfighttactics",
        "default_region": "eun1",
        "news_kind": "riot",
        "news_url": "https://teamfighttactics.leagueoflegends.com/en-us/news/game-updates/",
        "news_base": "https://teamfighttactics.leagueoflegends.com",
        # The TFT game-updates feed mixes in YouTube videos; title filter plus
        # the internal-link check in the parser keeps it to real patch notes.
        "title_filter": r"(?i)patch",
        "site": "https://teamfighttactics.leagueoflegends.com/en-us/news/game-updates/",
        "status_url": "https://status.riotgames.com/teamfighttactics?region=eun1",
        "icon": ICON_TFT,
        "art": None,
        "source_label": "Riot Games",
        "status_label": "Riot status",
    },
    "valorant": {
        "name": "VALORANT",
        "emoji": "\U0001f3af",
        "colour": 0xFF4655,
        "status_kind": "riot",
        "riot_host": "valorant",
        "default_region": "eu",
        "news_kind": "riot",
        "news_url": "https://playvalorant.com/en-us/news/tags/patch-notes/",
        "news_base": "https://playvalorant.com",
        "title_filter": r"(?i)patch notes",
        "site": "https://playvalorant.com/en-us/news/tags/patch-notes/",
        "status_url": "https://status.riotgames.com/valorant?region=eu",
        "icon": ICON_VAL,
        "art": None,
        "source_label": "Riot Games",
        "status_label": "Riot status",
    },
    "cs2": {
        "name": "Counter-Strike 2",
        "emoji": "\U0001f52b",
        "colour": 0xF0A202,
        "status_kind": None,
        "news_kind": "steam",
        "appid": 730,
        "steam_tag": "patchnotes",
        "title_filter": r"(?i)(counter-strike 2 update|release notes)",
        "exclude_filter": None,
        "site": "https://store.steampowered.com/news/app/730",
        "status_url": None,
        "icon": STEAM_ICON.format(appid=730, h="8dbc71957312bbd3baea65848b545be9eae2a355"),
        "art": STEAM_HEADER.format(appid=730),
        "source_label": "Steam \u2022 Valve",
        "status_label": "Status",
    },
    "eft": {
        "name": "Escape from Tarkov",
        "emoji": "\U0001fa96",
        "colour": 0x9A8866,
        "status_kind": "eft",
        # BSG's status API is global; there is no region selector. The key must
        # still exist because the status loop groups targets by region.
        "default_region": "global",
        "news_kind": "steam",
        "appid": 3932890,
        "steam_tag": "patchnotes",
        # Catches "Patch 1.1.5.0" and "Technical update"; drops surveys,
        # Twitch drops, TarkovTV and sale announcements.
        "title_filter": r"(?i)(patch|hotfix|technical update|wipe|server)",
        "exclude_filter": r"(?i)(survey|twitch drops|sale|tarkovtv|chinajoy)",
        "site": "https://store.steampowered.com/news/app/3932890",
        "status_url": "https://status.escapefromtarkov.com/",
        "icon": STEAM_ICON.format(appid=3932890, h="807fdb2b66eacb5e550b9043197cb7fd2b11b505"),
        # This app has no plain header.jpg (404); it only ships alt assets.
        "art": (
            "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/"
            "3932890/4dd12ea3b00a4f8b67bfd629f3b9abbf8d5cab05/header_alt_assets_2.jpg"
        ),
        "source_label": "Steam \u2022 Battlestate Games",
        "status_label": "BSG status",
    },
    "arc": {
        "name": "ARC Raiders",
        "emoji": "\U0001f6f0\ufe0f",
        "colour": 0xE87722,
        "status_kind": None,
        "news_kind": "steam",
        "appid": 1808500,
        # Deliberately NOT using the tag here: on this title the tag marks
        # cosmetic store rotations, not patches.
        "steam_tag": None,
        "title_filter": r"(?i)(live update|hotfix|patch|expedition|server)",
        # Weekly cosmetic shop refresh - noise for a patch-notes channel.
        "exclude_filter": r"(?i)store update",
        "site": "https://store.steampowered.com/news/app/1808500",
        "status_url": None,
        "icon": STEAM_ICON.format(appid=1808500, h="c284e73b6f3321864805d66f99924a0da9f0b219"),
        "art": STEAM_HEADER.format(appid=1808500),
        "source_label": "Steam \u2022 Embark Studios",
        "status_label": "Status",
    },
    "abi": {
        "name": "Arena Breakout: Infinite",
        "emoji": "\U0001f3dc\ufe0f",
        "colour": 0x6B8E23,
        "status_kind": None,
        "news_kind": "steam",
        "appid": 2073620,
        # No patchnotes tag is ever applied by this publisher - verified across
        # 30 announcements. Filter on title instead.
        "steam_tag": None,
        "title_filter": r"(?i)(live update|season|update announcement|patch|maintenance)",
        # Recurring anti-cheat ban lists; not patch notes.
        "exclude_filter": r"(?i)bans notice",
        "site": "https://store.steampowered.com/news/app/2073620",
        "status_url": None,
        "icon": STEAM_ICON.format(appid=2073620, h="3c3cb9ae001821040f4a5c255bc577fb0143dadd"),
        "art": STEAM_HEADER.format(appid=2073620),
        "source_label": "Steam \u2022 Morefun Studios",
        "status_label": "Status",
    },
}

# No-status games (arc, abi; status_kind=None above): documented so nobody
# wastes an afternoon looking - status.arcraiders.com and
# status.embark-studios.com are NXDOMAIN, arcraiders.statuspage.io and
# embarkstudios.statuspage.io are inactive placeholders, and Arena Breakout
# has no status host whatsoever. Code checks status_kind directly.

USER_AGENT = "Red-DiscordBot GameWatch cog (+https://github.com/ZeD3v/gamewatch)"

# ---------------------------------------------------------------------------
# Default location allowlists (per game key). Applied only to status
# incidents that name a specific game-server location (fail-open otherwise).
# Valorant's "eu" deployment is really EMEA: London, Paris, Frankfurt,
# Madrid, Warsaw, Stockholm, Istanbul + Bahrain + Cape Town + Tokyo
# (cross-checked valorfeed.gg + gameserverping.com, 2026-09-12). Defaults
# below = closest playable servers to Latvia. Matching is case-insensitive
# substring. Empty/missing = filter disabled for that game.
# ---------------------------------------------------------------------------
DEFAULT_LOCATION_ALLOW: dict[str, list[str]] = {
    "valorant": ["warsaw", "stockholm", "frankfurt", "london", "paris"],
}
