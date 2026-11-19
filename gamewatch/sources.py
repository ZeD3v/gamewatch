"""HTTP fetchers and parsers for each upstream source.

Kept free of any discord.py imports so the parsing logic can be exercised
standalone (see scripts/selftest.py).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Optional

import aiohttp

from .constants import (
    EFT_GLOBAL_URL,
    EFT_MESSAGES_URL,
    EFT_SERVICES_URL,
    EFT_STATUS_MAP,
    GAMES,
    RIOT_STATUS_URL,
    STEAM_NEWS_URL,
    USER_AGENT,
)
from .types import NewsItem, ParsedStatus, StatusItem

log = logging.getLogger("red.zed3v.gamewatch.sources")

NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>', re.S
)
TAG_RE = re.compile(r"<[^>]+>")


class FetchError(Exception):
    """Upstream fetch failed in a way worth counting toward the failure streak."""


async def _get(
    session: aiohttp.ClientSession,
    url: str,
    *,
    etag: Optional[str] = None,
    as_json: bool = True,
    timeout: int = 20,
) -> tuple[Any, Optional[str], int]:
    """GET with conditional-request support.

    Returns (payload, new_etag, http_status). payload is None on 304 or error.
    JSON is parsed with content_type=None because several upstreams (notably
    BSG's status API) serve JSON as text/html.
    """
    headers = {"User-Agent": USER_AGENT}
    if etag:
        headers["If-None-Match"] = etag
    try:
        async with session.get(
            url, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)
        ) as resp:
            new_etag = resp.headers.get("ETag")
            if resp.status == 304:
                return None, etag, 304
            if resp.status != 200:
                return None, etag, resp.status
            if as_json:
                return await resp.json(content_type=None), new_etag, 200
            return await resp.text(), new_etag, 200
    except asyncio.TimeoutError as exc:
        raise FetchError(f"timeout fetching {url}") from exc
    except aiohttp.ClientError as exc:
        raise FetchError(f"client error fetching {url}: {exc}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        # Malformed body (truncated JSON, wrong encoding, HTML error page
        # served as 200). Must stay inside the FetchError contract so the
        # failure-streak accounting and owner alert still fire.
        raise FetchError(f"malformed response from {url}: {exc}") from exc


# ---------------------------------------------------------------------------
# Riot status
# ---------------------------------------------------------------------------
def _riot_locale_content(entries: list[dict], locale: str = "en_US") -> str:
    """Pick one locale out of Riot's ~27-translation arrays."""
    if not entries:
        return ""
    for entry in entries:
        if entry.get("locale") == locale:
            return (entry.get("content") or "").strip()
    for entry in entries:
        if (entry.get("locale") or "").startswith("en"):
            return (entry.get("content") or "").strip()
    return (entries[0].get("content") or "").strip()


def parse_riot_status(payload: dict) -> ParsedStatus:
    """Normalise a Riot status document into our internal shape."""
    items: list[StatusItem] = []
    for kind in ("incidents", "maintenances"):
        for raw in payload.get(kind) or []:
            updates = raw.get("updates") or []
            latest = updates[-1] if updates else {}
            stamp = raw.get("updated_at") or raw.get("created_at")
            items.append(
                {
                    "uid": f"riot-{kind}-{raw.get('id')}-{stamp}",
                    "kind": kind,
                    "id": raw.get("id"),
                    "severity": raw.get("incident_severity")
                    or raw.get("maintenance_status")
                    or "info",
                    "title": _riot_locale_content(raw.get("titles") or []),
                    "body": _riot_locale_content(latest.get("translations") or []),
                    "platforms": raw.get("platforms") or [],
                    "created_at": raw.get("created_at"),
                    "updated_at": stamp,
                }
            )
    return {
        "region_id": payload.get("id"),
        "region_name": payload.get("name"),
        "items": items,
        "ok": not items,
    }


async def fetch_riot_status(
    session: aiohttp.ClientSession, game: str, region: str, etag: Optional[str] = None
) -> tuple[Optional[ParsedStatus], Optional[str], int]:
    cfg = GAMES[game]
    url = RIOT_STATUS_URL.format(host=cfg["riot_host"], region=region)
    payload, new_etag, status = await _get(session, url, etag=etag)
    if payload is None:
        return None, new_etag, status
    return parse_riot_status(payload), new_etag, status


# ---------------------------------------------------------------------------
# Escape from Tarkov status
# ---------------------------------------------------------------------------
def parse_eft_status(
    services: list, global_status: Optional[int], messages: list[dict]
) -> ParsedStatus:
    """Pure merge of the three BSG payloads into a ParsedStatus."""
    parsed_services = [
        {
            "name": svc.get("name"),
            "status": svc.get("status"),
            "label": EFT_STATUS_MAP.get(svc.get("status"), "Unknown"),
        }
        for svc in services
        if isinstance(svc, dict)
    ]
    worst = (
        max((s["status"] if isinstance(s["status"], int) else 0) for s in parsed_services)
        if parsed_services
        else 0
    )
    # Guard: the global endpoint has only ever been seen returning ints, but a
    # non-int here would poison max() and kill the whole parse.
    if isinstance(global_status, int):
        worst = max(worst, global_status)

    items = []
    if worst > 0:
        degraded = [s for s in parsed_services if (s["status"] or 0) > 0]
        items.append(
            {
                "uid": "eft-degraded-" + ",".join(f"{s['name']}:{s['status']}" for s in degraded),
                "kind": "incidents",
                "id": "services",
                "severity": "critical" if worst >= 3 else "warning",
                "title": f"{EFT_STATUS_MAP.get(worst, 'Degraded')} - "
                + ", ".join(s["name"] for s in degraded[:6]),
                "body": "\n".join(f"{s['name']}: {s['label']}" for s in degraded),
                "platforms": [],
                "created_at": None,
                "updated_at": None,
            }
        )
    for msg in messages:
        if not msg["content"]:
            continue
        items.append(
            {
                "uid": msg["uid"],
                "kind": "incidents",
                "id": msg["uid"],
                "severity": "warning",
                "title": "Server message",
                "body": msg["content"],
                "platforms": [],
                "created_at": msg["time"],
                "updated_at": msg["time"],
            }
        )

    return {
        "region_id": "global",
        "region_name": "Global",
        "services": parsed_services,
        "worst": worst,
        "items": items,
        "ok": worst == 0 and not items,
    }


async def fetch_eft_status(
    session: aiohttp.ClientSession, etag: Optional[str] = None
) -> tuple[Optional[ParsedStatus], Optional[str], int]:
    """Fetch the three BSG endpoints and merge via parse_eft_status.

    NOTE: the etag parameter is accepted for interface symmetry but ignored.
    Honouring a 304 on /api/services alone would skip /api/message/list and
    /api/global/status, hiding a new server message or global degradation
    behind an unchanged services payload. Combined size is ~1KB per poll, so
    conditional requests buy nothing here.
    """
    services, _, status = await _get(session, EFT_SERVICES_URL)
    if services is None:
        return None, None, status

    # The two secondary endpoints are independent of each other - fetch them
    # concurrently (same host, two requests; no rate-limit exposure).
    gres, mres = await asyncio.gather(
        _get(session, EFT_GLOBAL_URL),
        _get(session, EFT_MESSAGES_URL),
        return_exceptions=True,
    )

    global_status = None
    if isinstance(gres, tuple):
        gpayload, _, gstatus = gres
        if gstatus == 200 and isinstance(gpayload, dict):
            global_status = gpayload.get("status")
    elif isinstance(gres, FetchError):
        log.debug("EFT global status unavailable; using per-service data")
    elif isinstance(gres, BaseException):
        raise gres

    messages: list[dict] = []
    if isinstance(mres, tuple):
        mpayload, _, mstatus = mres
        if mstatus == 200 and isinstance(mpayload, list):
            for msg in mpayload:
                if msg.get("solveTime"):
                    continue  # already resolved
                messages.append(
                    {
                        "uid": f"eft-msg-{msg.get('_id')}",
                        "content": (msg.get("content") or "").strip(),
                        "type": msg.get("type"),
                        "time": msg.get("time"),
                    }
                )
    elif isinstance(mres, FetchError):
        log.debug("EFT message list unavailable")
    elif isinstance(mres, BaseException):
        raise mres

    return parse_eft_status(services, global_status, messages), None, 200


# ---------------------------------------------------------------------------
# Steam news
# ---------------------------------------------------------------------------
def _clean_steam_body(text: str, limit: int = 1400) -> str:
    text = re.sub(r"\[/?[a-z0-9=*\.\s\"':/\-_]+\]", "", text or "", flags=re.I)
    text = TAG_RE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.replace("\\[", "[").replace("\\]", "]").strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "\u2026"
    return text


def parse_steam_news(payload: dict, game: str) -> list[NewsItem]:
    """Filter a Steam news payload down to genuine patch notes.

    An item is kept when the patchnotes tag matches OR the title matches, and
    is then vetoed by exclude_filter. Tag-only filtering is wrong on several
    of these titles - see the notes in constants.GAMES.
    """
    cfg = GAMES[game]
    title_pattern = re.compile(cfg["title_filter"]) if cfg.get("title_filter") else None
    exclude_pattern = re.compile(cfg["exclude_filter"]) if cfg.get("exclude_filter") else None
    tag = cfg.get("steam_tag")
    out = []
    for item in (payload.get("appnews") or {}).get("newsitems") or []:
        title = item.get("title") or ""
        tags = item.get("tags") or []

        tag_hit = bool(tag) and tag in tags
        title_hit = bool(title_pattern) and bool(title_pattern.search(title))
        if not (tag_hit or title_hit):
            continue
        if exclude_pattern and exclude_pattern.search(title):
            continue

        out.append(
            {
                "uid": f"steam-{item.get('gid')}",
                "title": title,
                "url": item.get("url"),
                "body": _clean_steam_body(item.get("contents") or ""),
                # The BBCode original is kept so the embed layer can convert it
                # to Discord markdown (headings + bullets) rather than using the
                # flattened plain-text fallback above.
                "raw_bbcode": item.get("contents") or "",
                "author": item.get("author") or "",
                "timestamp": item.get("date"),
            }
        )
    return out


async def fetch_steam_news(
    session: aiohttp.ClientSession, game: str, count: int = 20, etag: Optional[str] = None
) -> tuple[Optional[list[NewsItem]], Optional[str], int]:
    cfg = GAMES[game]
    # Always request the UNFILTERED feed; filtering happens in parse_steam_news
    # because the server-side tag filter drops real patches on some titles.
    url = STEAM_NEWS_URL.format(appid=cfg["appid"], count=count)
    payload, new_etag, status = await _get(session, url, etag=etag)
    if payload is None:
        return None, new_etag, status
    return parse_steam_news(payload, game), new_etag, status


# ---------------------------------------------------------------------------
# Riot news (Next.js __NEXT_DATA__)
# ---------------------------------------------------------------------------
def parse_riot_news(html: str, game: str) -> list[NewsItem]:
    """Extract article cards from a Riot Next.js news page.

    Riot ships no RSS for any of these sites. The hydration blob carries the
    full article list, so we walk it rather than scraping rendered markup
    (which is class-name soup that changes constantly).
    """
    cfg = GAMES[game]
    match = NEXT_DATA_RE.search(html)
    if not match:
        raise FetchError(f"__NEXT_DATA__ not found for {game} (page structure changed?)")
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise FetchError(f"__NEXT_DATA__ not valid JSON for {game}") from exc

    found: list[dict] = []
    seen: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            items = node.get("items")
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    title = item.get("title")
                    published = item.get("publishedAt")
                    if not title or not published:
                        continue
                    action = item.get("action") or {}
                    url = (action.get("payload") or {}).get("url") or ""
                    key = f"{title}|{published}"
                    if key in seen:
                        continue
                    seen.add(key)
                    found.append(
                        {"title": title, "publishedAt": published, "url": url, "raw": item}
                    )
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)

    pattern = re.compile(cfg["title_filter"]) if cfg.get("title_filter") else None
    out = []
    for item in found:
        title = item["title"]
        url = item["url"]
        if pattern and not pattern.search(title):
            continue
        # Skip offsite cards (YouTube rundowns etc) - we want the notes page.
        if (
            url.startswith("http")
            and "leagueoflegends.com" not in url
            and "playvalorant.com" not in url
        ):
            continue
        full_url = url if url.startswith("http") else cfg["news_base"] + url
        desc = ((item["raw"].get("description") or {}).get("body")) or ""
        # Riot ships article hero art in media.url on the Sanity CDN. Request a
        # 1200x675 crop so Discord gets a 16:9 image sized for an embed rather
        # than the full 1920px original.
        raw_media = item["raw"].get("media")
        media = raw_media if isinstance(raw_media, dict) else {}
        image = media.get("url") or ""
        if image and media.get("type") == "image":
            sep = "&" if "?" in image else "?"
            image = f"{image}{sep}w=1200&h=675&fit=crop&auto=format"
        else:
            image = ""
        # The CMS also exposes a per-article primary colour; used as the embed
        # accent so each patch post matches its own artwork.
        colours = media.get("colors") or {}
        accent = colours.get("primary") or ""
        out.append(
            {
                "uid": f"riotnews-{game}-{full_url}",
                "title": title,
                "url": full_url,
                "body": TAG_RE.sub("", desc).strip(),
                "author": "",
                "published_at": item["publishedAt"],
                "image": image or None,
                "accent": accent or None,
                "category": ((item["raw"].get("category") or {}).get("title") or ""),
            }
        )
    out.sort(key=lambda entry: entry["published_at"], reverse=True)
    return out


async def fetch_riot_news(
    session: aiohttp.ClientSession, game: str, etag: Optional[str] = None
) -> tuple[Optional[list[NewsItem]], Optional[str], int]:
    cfg = GAMES[game]
    html, new_etag, status = await _get(
        session, cfg["news_url"], etag=etag, as_json=False, timeout=30
    )
    if html is None:
        return None, new_etag, status
    return parse_riot_news(html, game), new_etag, status


# ---------------------------------------------------------------------------
# Location filter (Valorant EMEA noise control)
# ---------------------------------------------------------------------------
# Riot embeds the affected game-server location ONLY in the incident body,
# one location per incident, fixed phrasing (verified across all six Valorant
# status deployments, 2026-09-12):
#   "The following game server location is currently unavailable while we
#    investigate an issue: Dubai"
LOCATION_RE = re.compile(r"location is currently unavailable.*?:\s*(?P<loc>.+)$", re.I | re.M)


def extract_location(item: StatusItem) -> Optional[str]:
    """Pull the affected server location out of an incident, if present."""
    for text in (item.get("body") or "", item.get("title") or ""):
        m = LOCATION_RE.search(text)
        if m:
            return m.group("loc").strip().rstrip(".")
    return None


def location_allowed(item: StatusItem, allow: list[str]) -> bool:
    """Return True if an incident should pass the location allowlist.

    Fail-open: returns True when the allowlist is empty (filter disabled) or
    when no specific game-server location can be extracted from the incident.
    """
    if not allow:
        return True
    loc = extract_location(item)
    if loc is None:
        return True
    loc_l = loc.lower()
    return any(a.lower() in loc_l for a in allow)


def filter_status_view(parsed: ParsedStatus, allow: list[str]) -> ParsedStatus:
    """Return a NEW ParsedStatus with location-filtered incidents removed.

    Fail-open: an incident whose location cannot be extracted is NEVER
    filtered - a genuine region-wide outage phrased differently must always
    announce. An empty allowlist disables filtering entirely.
    ok is recomputed from the surviving items so a lingering filtered
    incident cannot suppress recovery for locations people actually play on.
    """
    if not allow:
        out = dict(parsed)
        out["filtered_count"] = 0
        out["filtered_items"] = []
        return out  # type: ignore[return-value]
    kept, dropped = [], []
    for item in parsed.get("items") or []:
        if location_allowed(item, allow):
            kept.append(item)
        else:
            dropped.append(item)
    out = dict(parsed)
    out["items"] = kept
    out["filtered_count"] = len(dropped)
    out["filtered_items"] = dropped
    # A region degraded only by filtered locations counts as healthy: the
    # servers this community connects to are fine.
    worst_service = parsed.get("worst", 0) or 0
    out["ok"] = not kept and worst_service == 0
    if parsed.get("ok"):
        out["ok"] = True
    return out  # type: ignore[return-value]
