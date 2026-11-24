"""Static game definitions for GameWatch.

Every endpoint referenced here was probed live before being added. Notes on the
quirks are kept next to the definition they apply to, because they are the kind
of thing that silently rots.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Riot status (public CDN, no API key)
# ---------------------------------------------------------------------------
# This is the exact feed status.riotgames.com consumes. The documented
# *.api.riotgames.com/lol/status/v4/platform-data endpoint requires a dev key
# that expires every 24h and is unusable for a long-running bot.
#
# Region identifiers are NOT consistent between games:
#   LoL      -> platform ids  (eun1, euw1, kr1, ru1, pbe, ...)
#   TFT      -> platform ids  (eun1, euw1, kr, ru, pbe1, ...)
#   VALORANT -> deployments   (ap, br, eu, kr, latam, na, pbe)
# ph2 and th2 were merged into sg2 in 2025 (LoL still serves frozen fossils!).
RIOT_STATUS_URL = "https://{host}.secure.dyn.riotcdn.net/channels/public/x/status/{region}.json"

RIOT_VALID_REGIONS: dict[str, tuple[str, ...]] = {
    "lol": (
        "br1",
        "eun1",
        "euw1",
        "jp1",
        "kr1",
        "la1",
        "la2",
        "me1",
        "na1",
        "oc1",
        "pbe",
        "ru1",
        "sg2",
        "tr1",
        "tw2",
        "vn2",
    ),
    "tft": (
        "br1",
        "eun1",
        "euw1",
        "jp1",
        "kr",
        "la1",
        "la2",
        "me1",
        "na1",
        "oc1",
        "pbe1",
        "ru",
        "sg2",
        "tr1",
        "tw2",
        "vn2",
    ),
    "valorant": ("ap", "br", "eu", "kr", "latam", "na", "pbe"),
}

RIOT_RETIRED_REGIONS: dict[str, str] = {
    "ph2": "sg2",
    "th2": "sg2",
}

RIOT_SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}
RIOT_MAINTENANCE_SEVERITY: dict[str, str] = {
    "in_progress": "warning",
    "scheduled": "info",
}

# ---------------------------------------------------------------------------
# Riot news (Publishing Content Service v2.0 public JSON + Next.js HTML fallback)
# ---------------------------------------------------------------------------
# Riot's websites are Next.js on Netlify (which ignores If-None-Match on weak
# ETags and serves 235-535 KB HTML). PCS v2.0 is the underlying public JSON CMS
# endpoint with 100% item parity, 58-85% smaller wire transfer, and no buildId
# rotation. Next.js HTML (__NEXT_DATA__) is retained as an automatic fallback.
RIOT_PCS_URL = (
    "https://content.publishing.riotgames.com/publishing-content/v2.0/public/"
    "channel/{channel}/page/%2Fnews%2Ftags%2Fpatch-notes%2F?locale={locale}"
)

# ---------------------------------------------------------------------------
# Steam news & Partner Events (public, no API key)
# ---------------------------------------------------------------------------
# Partner Events (ajaxgetpartnereventspageable) is the exact production backend
# used by Steam Store News Hub and counter-strike.net/news/updates. Unlike
# ISteamNews/GetNewsForApp/v2 (which edge-caches for 30-56 minutes, mixes in
# third-party press unless feeds=steam_community_announcements is passed, and
# omits event_type and capsule cover art), Partner Events exposes Valve's
# authoritative event_type enum (12=Small Update/Patch Notes, 13=Regular Update,
# 14=Major Update), 800x450 16:9 capsule cover images, and direct canonical URLs.
#
# WARNING for legacy STEAM_NEWS_URL: maxlength must be 0 (any non-zero value
# flattens BBCode and concatenates words), and feeds=steam_community_announcements
# is mandatory to drop third-party press syndication (PlayGround.ru, etc.).
STEAM_NEWS_URL = (
    "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
    "?appid={appid}&count={count}&maxlength=0&feeds=steam_community_announcements"
)
STEAM_PARTNER_EVENTS_URL = "https://store.steampowered.com/events/ajaxgetpartnereventspageable/"
STEAM_EVENT_CANONICAL_URL = "https://store.steampowered.com/news/app/{appid}/view/{gid}"
STEAM_CLAN_IMAGE_FASTLY = "https://clan.fastly.steamstatic.com/images/"

# Optional keyed Steam Web API status (Counter-Strike 2 / appid 730).
# Requires a free Steam Web API key via Red's `[p]set api steam api_key,<key>`.
STEAM_CS2_STATUS_URL = "https://api.steampowered.com/ICSGOServers_730/GetGameServersStatus/v1/"
CS2_SERVICE_NAMES: dict[str, str] = {
    "SessionsLogon": "Sessions Logon",
    "SteamCommunity": "Steam Community",
    "IEconItems": "Player Inventories",
    "Leaderboards": "Leaderboards",
}

# ---------------------------------------------------------------------------
# Escape from Tarkov (BSG status & official patch-notes JSON APIs)
# ---------------------------------------------------------------------------
# Status responses are served with Content-Type: text/html despite being JSON,
# so the fetcher must not gate on content type. Status ints and message types
# were confirmed directly from BSG's Nuxt bundle and tarkov-data-manager.
EFT_SERVICES_URL = "https://status.escapefromtarkov.com/api/services"
EFT_GLOBAL_URL = "https://status.escapefromtarkov.com/api/global/status"
EFT_MESSAGES_URL = "https://status.escapefromtarkov.com/api/message/list"
EFT_DATACENTERS_URL = "https://status.escapefromtarkov.com/api/datacenter/list"
EFT_STATUS_MAP = {0: "OK", 1: "Updating", 2: "Unstable", 3: "Down"}
EFT_GLOBAL_DEFAULT_TEXT: dict[int, str] = {
    0: "Stable",
    1: "Servers on update",
    2: "Partial problems with the server",
    3: "Servers down",
}
EFT_MESSAGE_TYPES: dict[int, tuple[str, str]] = {
    0: ("Info", "info"),
    1: ("Update installation", "warning"),
    2: ("Server issues", "warning"),
    3: ("Server issues", "critical"),
}
# Official BSG Patch Notes JSON feed (type_id=2 = Patch Notes): 6-20 hours faster
# than Steam cross-posts, includes patches missing from Steam, full HTML inline.
EFT_NEWS_API_URL = "https://www.escapefromtarkov.com/site/api/v1/news-list/2/en?page=1"
EFT_SITE_BASE = "https://www.escapefromtarkov.com"

# ---------------------------------------------------------------------------
# Arena Breakout: Infinite (Level Infinite GPTS CMS public JSON API)
# ---------------------------------------------------------------------------
# MoreFun Studios stopped cross-posting routine Live Updates to Steam after
# July 2026; 9 of the last 10 updates only exist on the official CMS.
# CRITICAL: X-Source MUST be "pc_web" (numeric values silently return empty
# data), language MUST be a JSON list ["en"], and get_num must stay <= 20.
ABI_CMS_LIST_URL = (
    "https://sg-community.playerinfinite.com/api/"
    "gpts.information_feeds_svr.InformationFeedsSvr/GetContentByLabel"
)
ABI_CMS_DETAIL_URL = (
    "https://sg-community.playerinfinite.com/api/"
    "gpts.information_feeds_svr.InformationFeedsSvr/GetContentInfoById"
)
ABI_CMS_HEADERS: dict[str, str] = {
    "Content-Type": "application/json;charset=utf-8",
    "X-Language": "en",
    "X-GameId": "30048",
    "X-AreaId": "global",
    "X-Source": "pc_web",
}
ABI_CMS_GAME_ID = "30048"
ABI_CMS_PRIMARY_LABEL = 893  # NEWS
ABI_CMS_SECONDARY_LABEL = 1318  # ANNOUNCEMENT (0 Ban Notices)
ABI_ARTICLE_URL = "https://www.arenabreakoutinfinite.com/en/news.html?content_id={content_id}"

# ---------------------------------------------------------------------------
# Branding assets (every URL below was fetched and confirmed HTTP 200)
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# Game table
# ---------------------------------------------------------------------------
# news_kind:   "riot" | "steam_events" | "eft_bsg" | "abi_cms"
# status_kind: "riot" | "eft" | "steam_cs2" | None
# requires_api_key: optional Red shared API token service name (e.g. "steam")
GAMES = {
    "lol": {
        "name": "League of Legends",
        "emoji": "\U0001f5e1\ufe0f",
        "colour": 0xC89B3C,
        "status_kind": "riot",
        "riot_host": "lol",
        "default_region": "eun1",
        "news_kind": "riot",
        "riot_pcs_channel": "league_of_legends_website",
        "riot_locale": "en-us",
        "news_url": "https://www.leagueoflegends.com/en-us/news/tags/patch-notes/",
        "news_base": "https://www.leagueoflegends.com",
        "title_filter": r"(?i)patch.*notes",
        "site": "https://www.leagueoflegends.com/en-us/news/tags/patch-notes/",
        "status_url": "https://status.riotgames.com/lol?region={region}",
        # Sanity CDN favicon. ?fm=png converts the SVG, because Discord will
        # not render an image/svg+xml embed icon.
        "icon": ICON_LOL,
        "art": None,
        "source_label": "Riot Games",
        "status_label": "Riot status",
        "uid_prefix": "riotnews-lol-",
    },
    "tft": {
        "name": "Teamfight Tactics",
        "emoji": "\u265f\ufe0f",
        "colour": 0x1E90FF,
        "status_kind": "riot",
        "riot_host": "teamfighttactics",
        "default_region": "eun1",
        "news_kind": "riot",
        "riot_pcs_channel": "teamfight_tactics_website",
        "riot_locale": "en-us",
        "news_url": "https://teamfighttactics.leagueoflegends.com/en-us/news/tags/patch-notes/",
        "news_base": "https://teamfighttactics.leagueoflegends.com",
        "title_filter": r"(?i)patch",
        "site": "https://teamfighttactics.leagueoflegends.com/en-us/news/tags/patch-notes/",
        "status_url": "https://status.riotgames.com/teamfighttactics?region={region}",
        "icon": ICON_TFT,
        "art": None,
        "source_label": "Riot Games",
        "status_label": "Riot status",
        "uid_prefix": "riotnews-tft-",
    },
    "valorant": {
        "name": "VALORANT",
        "emoji": "\U0001f3af",
        "colour": 0xFF4655,
        "status_kind": "riot",
        "riot_host": "valorant",
        "default_region": "eu",
        "news_kind": "riot",
        "riot_pcs_channel": "valorant_website",
        "riot_locale": "en-us",
        "news_url": "https://playvalorant.com/en-us/news/tags/patch-notes/",
        "news_base": "https://playvalorant.com",
        "title_filter": r"(?i)patch notes",
        "site": "https://playvalorant.com/en-us/news/tags/patch-notes/",
        "status_url": "https://status.riotgames.com/valorant?region={region}",
        "icon": ICON_VAL,
        "art": None,
        "source_label": "Riot Games",
        "status_label": "Riot status",
        "uid_prefix": "riotnews-valorant-",
    },
    "cs2": {
        "name": "Counter-Strike 2",
        "emoji": "\U0001f52b",
        "colour": 0xF0A202,
        "status_kind": "steam_cs2",
        "requires_api_key": "steam",
        "default_region": "global",
        "news_kind": "steam_events",
        "appid": 730,
        # Exact parity with counter-strike.net/news/updates (event_type == 12).
        "steam_event_types": (12,),
        "steam_exclude_tags": ("hide_library_detail", "mod_hide_library_detail"),
        "steam_tag": "patchnotes",
        "title_filter": r"(?i)(counter-strike 2 update|release notes)",
        "exclude_filter": None,
        "site": "https://store.steampowered.com/news/app/730",
        "status_url": None,
        "icon": STEAM_ICON.format(appid=730, h="8dbc71957312bbd3baea65848b545be9eae2a355"),
        "art": STEAM_HEADER.format(appid=730),
        "source_label": "Steam \u2022 Valve",
        "status_label": "Steam Web API",
        "uid_prefix": "steam-ev-",
    },
    "eft": {
        "name": "Escape from Tarkov",
        "emoji": "\U0001fa96",
        "colour": 0x9A8866,
        "status_kind": "eft",
        # BSG's status API is global; there is no region selector. The key must
        # still exist because the status loop groups targets by region.
        "default_region": "global",
        "news_kind": "eft_bsg",
        "appid": 3932890,
        "steam_tag": "patchnotes",
        "title_filter": r"(?i)(patch|hotfix|technical update|wipe)",
        "exclude_filter": r"(?i)(survey|twitch drops|sale|discount|tarkovtv|chinajoy)",
        "site": "https://www.escapefromtarkov.com/news",
        "status_url": "https://status.escapefromtarkov.com/",
        "icon": STEAM_ICON.format(appid=3932890, h="807fdb2b66eacb5e550b9043197cb7fd2b11b505"),
        # This app has no plain header.jpg (404); it only ships hashed alt assets.
        "art": (
            "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/"
            "3932890/4dd12ea3b00a4f8b67bfd629f3b9abbf8d5cab05/header_alt_assets_2.jpg"
        ),
        "source_label": "Battlestate Games",
        "status_label": "BSG status",
        "uid_prefix": "eft-bsg-",
    },
    "arc": {
        "name": "ARC Raiders",
        "emoji": "\U0001f6f0\ufe0f",
        "colour": 0xE87722,
        "status_kind": None,
        "news_kind": "steam_events",
        "appid": 1808500,
        # Embark files Live Updates as ET=13, major expansions as ET=14, and
        # occasional hotfixes (e.g. 1.36.1) as ET=28, while mis-filing cosmetic
        # Store Updates as ET=12. Keep ET in (12, 13, 14) OR title_filter, veto
        # anti-cheat/surveys, and apply body-aware filtering on "Store Update"
        # so real 7-11 KB gameplay Store Updates pass while 0.2-2.7 KB cosmetic
        # shop rotations stay dropped.
        "steam_event_types": (12, 13, 14),
        "steam_exclude_tags": (),
        "steam_tag": None,
        "title_filter": (
            r"(?i)(live update|hotfix|patch|\b\d+\.\d+(?:\.\d+)?\s+update\b|expedition)"
        ),
        "exclude_filter": r"(?i)(anti-cheat|survey|feedback)",
        "store_update_min_body": 3500,
        "store_update_patch_filter": (
            r"(?i)(bug\s*fix|content and bug fixes|"
            r"\[h[23]\]\s*(?:fixes|bug\s*fixes|gameplay|balance|highlights))"
        ),
        "site": "https://store.steampowered.com/news/app/1808500",
        "status_url": None,
        "icon": STEAM_ICON.format(appid=1808500, h="c284e73b6f3321864805d66f99924a0da9f0b219"),
        "art": STEAM_HEADER.format(appid=1808500),
        "source_label": "Steam \u2022 Embark Studios",
        "status_label": "Status",
        "uid_prefix": "steam-ev-",
    },
    "abi": {
        "name": "Arena Breakout: Infinite",
        "emoji": "\U0001f3dc\ufe0f",
        "colour": 0x6B8E23,
        "status_kind": None,
        "news_kind": "abi_cms",
        "appid": 2073620,
        "title_filter": r"(?i)(\bupdate\b|patch|hotfix|maintenance|season reset)",
        "exclude_filter": (
            r"(?i)(test server|bans notice|security report|"
            r"events announcement|creator|partner|giveaway|sign-in)"
        ),
        "site": "https://www.arenabreakoutinfinite.com/en/list.html",
        "status_url": None,
        "icon": STEAM_ICON.format(appid=2073620, h="3c3cb9ae001821040f4a5c255bc577fb0143dadd"),
        "art": STEAM_HEADER.format(appid=2073620),
        "source_label": "MoreFun Studios",
        "status_label": "Status",
        "uid_prefix": "abi-cms-",
    },
    "robrun": {
        "name": "ROB RUN",
        "emoji": "\U0001f4b0",
        "colour": 0xF4D03F,
        "status_kind": None,
        "news_kind": "steam_events",
        "appid": 4262460,
        "steam_event_types": (12, 13, 14),
        "steam_exclude_tags": (),
        "steam_tag": "patchnotes",
        "title_filter": r"(?i)(patch|hotfix|update|changelog|maintenance|first changes)",
        "exclude_filter": (
            r"(?i)(\bfaq\b|frequently asked|video challenge|"
            r"creator(?:s)? wanted|partner program|playtest starts|"
            r"gameplay overview|first look)"
        ),
        "site": "https://store.steampowered.com/news/app/4262460",
        "status_url": None,
        "icon": STEAM_ICON.format(appid=4262460, h="6925066dc70cf9453455a8c1db9539521a116f63"),
        # Plain header.jpg returns 404; use the hashed store header asset.
        "art": (
            "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/"
            "4262460/db8e0cf43c9475b96fb9446b4908de79fcb9f619/header.jpg"
        ),
        "source_label": "Steam \u2022 Kefir",
        "status_label": "Status",
        "uid_prefix": "steam-ev-",
    },
}

# No-status games (arc, abi, robrun; status_kind=None above):
# - arc: status.arcraiders.com / status.embark-studios.com / status.embark.games
#   are NXDOMAIN; embark.statuspage.io is inactive (401); no Steam Web API
#   status interface exists for appid 1808500; Embark directs players to Discord.
# - abi: status.arenabreakoutinfinite.com / status.levelinfinite.com NXDOMAIN;
#   zero status endpoints in frontend JS or Steam Web API.
# - robrun: status.robrun.com / status.kefir.com NXDOMAIN; status.kefirgames.com
#   has invalid SSL hostname mismatch; no Steam Web API status interface.
# Note: cs2 uses Valve's official ICSGOServers_730/GetGameServersStatus/v1
# endpoint when a free Steam Web API key is configured via `[p]set api steam`.

USER_AGENT = "Red-DiscordBot GameWatch cog (+https://github.com/ZeD3v/gamewatch)"

# ---------------------------------------------------------------------------
# Default location allowlists (per game key). Applied only to status
# incidents that name a specific game-server location (fail-open otherwise).
# Disabled by default (empty) so servers in any region receive all status
# alerts out of the box. Admins on wide deployments like VALORANT's "eu"
# (EMEA: London, Paris, Frankfurt, Madrid, Warsaw, Stockholm, Istanbul,
# Dubai, Bahrain, Cape Town, Tokyo) can opt in via `[p]gamewatch locations`.
# Matching is case-insensitive substring. Empty/missing = filter disabled.
# ---------------------------------------------------------------------------
DEFAULT_LOCATION_ALLOW: dict[str, list[str]] = {}
