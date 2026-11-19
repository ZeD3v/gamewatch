"""HTTP fetchers and parsers for each upstream source.

Kept free of any discord.py or redbot imports so the parsing logic can be
exercised standalone (see scripts/selftest.py).
"""

from __future__ import annotations

import asyncio
import email.utils
import functools
import json
import logging
import math
import random
import re
from datetime import datetime, timezone
from typing import Any, Optional, Sequence
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import aiohttp

from .constants import (
    ABI_ARTICLE_URL,
    ABI_CMS_DETAIL_URL,
    ABI_CMS_GAME_ID,
    ABI_CMS_HEADERS,
    ABI_CMS_LIST_URL,
    ABI_CMS_PRIMARY_LABEL,
    ABI_CMS_SECONDARY_LABEL,
    EFT_DATACENTERS_URL,
    EFT_GLOBAL_DEFAULT_TEXT,
    EFT_GLOBAL_URL,
    EFT_MESSAGE_TYPES,
    EFT_MESSAGES_URL,
    EFT_NEWS_API_URL,
    EFT_SERVICES_URL,
    EFT_SITE_BASE,
    EFT_STATUS_MAP,
    GAMES,
    RIOT_MAINTENANCE_SEVERITY,
    RIOT_PCS_URL,
    RIOT_SEVERITY_ORDER,
    RIOT_STATUS_URL,
    STEAM_CLAN_IMAGE_FASTLY,
    STEAM_EVENT_CANONICAL_URL,
    STEAM_NEWS_URL,
    STEAM_PARTNER_EVENTS_URL,
    USER_AGENT,
)
from .types import CacheValidator, NewsItem, ParsedStatus, StatusItem

log = logging.getLogger("red.zed3v.gamewatch.sources")

MAX_RESPONSE_BYTES = 4 * 1024 * 1024  # 4 MiB hard cap per HTTP response
MAX_RETRY_AFTER = 10.0  # seconds; longer Retry-After defers to the next poll
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
_SECRET_QUERY_KEYS = frozenset({"key", "api_key", "apikey", "token", "access_token", "auth"})

NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>', re.S
)
TAG_RE = re.compile(r"<[^>]+>")
_STORE_UPDATE_TITLE_RE = re.compile(r"(?i)\bstore update\b")
_CAPSULE_FILENAME_RE = re.compile(r"^[A-Za-z0-9_.-]+\.(?:png|jpe?g|webp)$")
_STEAM_IMG_PAIR_RE = re.compile(r"\[img(?:[\s=][^\]]*)?\][^\[]*\[/img\]", re.I | re.S)
_STEAM_BLOCK_BREAK_RE = re.compile(
    r"(?<!\\)\[(?:(?:/p|/tr|br|/h[1-6]|/?hr)(?:[\s=][^\]]*)?|\*)\]", re.I
)
_STEAM_CELL_SEP_RE = re.compile(r"(?<!\\)\[/(?:td|th)\]", re.I)
_STEAM_KNOWN_TAG_RE = re.compile(
    r"(?<!\\)\[/?(?:(?:[biu]|\*)"
    r"|(?:previewyoutube|dynamiclink|table|tr|td|th|video|noparse|expand"
    r"|olist|list|url|quote|spoiler|strike|code|img|h[1-6]|p|br|hr)(?:[\s=][^\]]*)?)\]",
    re.I,
)
_MULTI_NL_RE = re.compile(r"\n{3,}")
_TRAILING_SPACE_RE = re.compile(r"(?<![ \t])[ \t]+\n")


class FetchError(Exception):
    """Upstream fetch failed in a way worth counting toward the failure streak."""


@functools.lru_cache(maxsize=64)
def _compiled_filter(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


def _safe_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return default


def _safe_status_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    return value if isinstance(value, int) else default


def _parse_iso_dt(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_iso_ts(value: Any) -> float:
    dt = _parse_iso_dt(value)
    return dt.timestamp() if dt is not None else 0.0


def _redact_url(url: str) -> str:
    """Mask sensitive query parameters before logging or raising FetchError."""
    parts = urlsplit(url)
    if not parts.query:
        return url
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    redacted = [(k, "REDACTED" if k.lower() in _SECRET_QUERY_KEYS else v) for k, v in pairs]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(redacted), parts.fragment))


def _parse_retry_after(header: Optional[str], *, now: Optional[datetime] = None) -> Optional[float]:
    """Parse an RFC 9110 Retry-After header (seconds or HTTP-date)."""
    if not header or not isinstance(header, str):
        return None
    raw = header.strip()
    if not raw:
        return None
    try:
        secs = float(raw)
    except ValueError:
        secs = None
    if secs is not None:
        if math.isnan(secs) or math.isinf(secs):
            return None
        return max(0.0, secs)
    try:
        dt = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    ref = now or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    return max(0.0, (dt - ref).total_seconds())


def _extract_validator(
    headers: Any, previous: Optional[CacheValidator] = None, *, merge_previous: bool = False
) -> CacheValidator:
    out: CacheValidator = dict(previous) if (merge_previous and previous) else {}
    etag = headers.get("ETag")
    last_modified = headers.get("Last-Modified")
    if etag:
        out["etag"] = etag
    if last_modified:
        out["last_modified"] = last_modified
    return out


async def _read_bounded(resp: aiohttp.ClientResponse, max_bytes: int, safe_url: str) -> bytes:
    """Read an HTTP response body up to max_bytes (zip-bomb safe)."""
    if not resp.headers.get("Content-Encoding"):
        cl_raw = resp.headers.get("Content-Length")
        if cl_raw is not None:
            try:
                if int(cl_raw) > max_bytes:
                    raise FetchError(f"response from {safe_url} exceeded {max_bytes} bytes")
            except ValueError:
                pass
    buf = bytearray()
    async for chunk in resp.content.iter_chunked(65536):
        if len(buf) + len(chunk) > max_bytes:
            raise FetchError(f"response from {safe_url} exceeded {max_bytes} bytes")
        buf.extend(chunk)
    return bytes(buf)


def _decode_bytes(raw: bytes, charset: Optional[str], *, as_json: bool) -> str:
    norm = (charset or "").lower().replace("_", "-")
    if as_json and norm in ("", "utf-8", "utf8"):
        return raw.decode("utf-8-sig")
    return raw.decode(charset or "utf-8")


async def _request(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    params: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, str]] = None,
    json_body: Optional[dict[str, Any]] = None,
    validator: Optional[CacheValidator] = None,
    as_json: bool = True,
    timeout: int = 20,
    max_bytes: int = MAX_RESPONSE_BYTES,
    retries: int = 2,
) -> tuple[Any, CacheValidator, int]:
    """HTTP request with conditional validators, bounded reads, and transient retries.

    Returns (payload, new_validator, http_status).
    - Never mutates the caller's ``validator``.
    - Retries up to ``retries`` times on transient network/timeout errors and
      HTTP {429, 500, 502, 503, 504}. Exhausted HTTP statuses return
      ``(None, preserved_validator, last_status)``.
    - Never retries 304, 4xx (except 429), malformed JSON/UTF-8, or oversized bodies.
    """
    safe_url = _redact_url(url)
    preserved: CacheValidator = dict(validator) if validator else {}
    req_headers: dict[str, str] = {"User-Agent": USER_AGENT}
    if headers:
        req_headers.update(headers)
    if preserved.get("etag"):
        req_headers["If-None-Match"] = str(preserved["etag"])
    if preserved.get("last_modified"):
        req_headers["If-Modified-Since"] = str(preserved["last_modified"])

    req_kwargs: dict[str, Any] = {
        "headers": req_headers,
        "timeout": aiohttp.ClientTimeout(total=timeout),
    }
    if params is not None:
        req_kwargs["params"] = params
    if json_body is not None:
        req_kwargs["json"] = json_body

    attempts = max(1, retries + 1)
    for attempt in range(attempts):
        retry_delay: Optional[float] = None
        try:
            async with session.request(method, url, **req_kwargs) as resp:
                status = resp.status
                if status == 304:
                    return (
                        None,
                        _extract_validator(resp.headers, preserved, merge_previous=True),
                        304,
                    )
                if status in RETRYABLE_STATUSES:
                    ra_delay = _parse_retry_after(resp.headers.get("Retry-After"))
                    if ra_delay is not None and ra_delay > MAX_RETRY_AFTER:
                        return None, dict(preserved), status
                    if attempt + 1 < attempts:
                        retry_delay = (
                            ra_delay
                            if ra_delay is not None
                            else 0.5 * (2**attempt) + random.uniform(0.0, 0.25)
                        )
                    else:
                        return None, dict(preserved), status
                elif status != 200:
                    return None, dict(preserved), status
                else:
                    raw = await _read_bounded(resp, max_bytes, safe_url)
                    new_val = _extract_validator(resp.headers)
                    try:
                        text = _decode_bytes(raw, resp.charset, as_json=as_json)
                        if as_json:
                            return json.loads(text), new_val, 200
                        return text, new_val, 200
                    except (
                        json.JSONDecodeError,
                        UnicodeDecodeError,
                        LookupError,
                        ValueError,
                    ) as exc:
                        raise FetchError(f"malformed response from {safe_url}: {exc}") from exc
        except asyncio.TimeoutError as exc:
            if attempt + 1 < attempts:
                retry_delay = 0.5 * (2**attempt) + random.uniform(0.0, 0.25)
            else:
                raise FetchError(f"timeout fetching {safe_url}") from exc
        except aiohttp.ClientError as exc:
            if attempt + 1 < attempts:
                retry_delay = 0.5 * (2**attempt) + random.uniform(0.0, 0.25)
            else:
                raise FetchError(f"client error fetching {safe_url}: {exc}") from exc

        if retry_delay is not None:
            await asyncio.sleep(retry_delay)

    return None, dict(preserved), 0


async def _get(
    session: aiohttp.ClientSession,
    url: str,
    *,
    etag: Optional[str] = None,
    as_json: bool = True,
    timeout: int = 20,
) -> tuple[Any, Optional[str], int]:
    """Backwards-compatible GET wrapper delegating to _request."""
    validator: CacheValidator = {"etag": etag} if etag else {}
    payload, new_val, status = await _request(
        session, "GET", url, validator=validator, as_json=as_json, timeout=timeout
    )
    return payload, new_val.get("etag") or etag, status


# ---------------------------------------------------------------------------
# Riot status
# ---------------------------------------------------------------------------
def _riot_locale_content(entries: Any, locale: str = "en_US") -> str:
    """Pick one locale out of Riot's ~27-translation arrays."""
    if not isinstance(entries, list) or not entries:
        return ""
    for entry in entries:
        if isinstance(entry, dict) and entry.get("locale") == locale:
            return (entry.get("content") or "").strip()
    for entry in entries:
        if isinstance(entry, dict) and (entry.get("locale") or "").startswith("en"):
            return (entry.get("content") or "").strip()
    first = entries[0]
    return (first.get("content") or "").strip() if isinstance(first, dict) else ""


def parse_riot_status(payload: dict, *, now: Optional[datetime] = None) -> ParsedStatus:
    """Normalise a Riot status document with exact status.riotgames.com filtering."""
    if not isinstance(payload, dict):
        raise FetchError("Riot status payload is not a JSON object")

    ref_now = now or datetime.now(timezone.utc)
    if ref_now.tzinfo is None:
        ref_now = ref_now.replace(tzinfo=timezone.utc)
    else:
        ref_now = ref_now.astimezone(timezone.utc)

    items: list[StatusItem] = []
    for kind in ("incidents", "maintenances"):
        raw_kind = payload.get(kind)
        if not isinstance(raw_kind, list):
            continue
        for raw in raw_kind:
            if not isinstance(raw, dict):
                continue

            mstat: Optional[str] = None
            if kind == "maintenances":
                raw_mstat = raw.get("maintenance_status")
                mstat = str(raw_mstat).lower() if raw_mstat else None
                if mstat == "complete":
                    continue

            archive_dt = _parse_iso_dt(raw.get("archive_at"))
            if archive_dt is not None and archive_dt <= ref_now:
                continue

            # Match Riot's official status.riotgames.com React bundle: keep only
            # published updates targeting "riotstatus"; drop items with 0 left.
            raw_updates = raw.get("updates")
            if not isinstance(raw_updates, list):
                continue
            valid_updates = [
                u
                for u in raw_updates
                if isinstance(u, dict)
                and u.get("publish", True) is not False
                and isinstance(u.get("publish_locations"), list)
                and "riotstatus" in u["publish_locations"]
            ]
            if not valid_updates:
                continue

            latest = max(
                valid_updates,
                key=lambda u: (
                    _parse_iso_ts(u.get("updated_at") or u.get("created_at")),
                    _safe_int(u.get("id")),
                ),
            )
            update_stamp = (
                latest.get("updated_at") or latest.get("created_at") or raw.get("created_at")
            )
            state_tag = f"-{mstat}" if mstat else ""
            uid = f"riot-{kind}-{raw.get('id')}{state_tag}-{latest.get('id')}-{update_stamp}"

            if kind == "maintenances":
                severity = RIOT_MAINTENANCE_SEVERITY.get(mstat or "", "info")
            else:
                raw_sev = str(raw.get("incident_severity") or "info").lower()
                severity = raw_sev if raw_sev in RIOT_SEVERITY_ORDER else "info"

            platforms = raw.get("platforms")
            items.append(
                {
                    "uid": uid,
                    "kind": kind,
                    "id": raw.get("id"),
                    "severity": severity,
                    "maintenance_status": mstat,
                    "title": _riot_locale_content(raw.get("titles")),
                    "body": _riot_locale_content(latest.get("translations")),
                    "platforms": platforms if isinstance(platforms, list) else [],
                    "created_at": raw.get("created_at"),
                    "updated_at": update_stamp,
                }
            )

    has_active = any(it.get("maintenance_status") != "scheduled" for it in items)
    return {
        "region_id": payload.get("id"),
        "region_name": payload.get("name"),
        "items": items,
        "ok": not has_active,
    }


async def fetch_riot_status(
    session: aiohttp.ClientSession,
    game: str,
    region: str,
    *,
    validator: Optional[CacheValidator] = None,
) -> tuple[Optional[ParsedStatus], CacheValidator, int]:
    cfg = GAMES[game]
    url = RIOT_STATUS_URL.format(host=cfg["riot_host"], region=region)
    payload, new_val, status = await _request(session, "GET", url, validator=validator)
    if payload is None:
        return None, new_val, status
    return parse_riot_status(payload), new_val, status


# ---------------------------------------------------------------------------
# Escape from Tarkov status
# ---------------------------------------------------------------------------
def _clean_eft_global_message(message: Optional[str]) -> str:
    if not message or not isinstance(message, str):
        return ""
    cleaned = message.strip()
    if cleaned.rstrip(".!").lower() == "access denied":
        return ""
    return cleaned


def parse_eft_status(
    services: list,
    global_status: Optional[int],
    messages: list[dict],
    datacenters: Optional[list] = None,
    global_message: Optional[str] = None,
) -> ParsedStatus:
    """Pure merge of the four BSG payloads into a ParsedStatus."""
    parsed_services = [
        {
            "name": svc.get("name"),
            "status": _safe_status_int(svc.get("status")),
            "label": EFT_STATUS_MAP.get(_safe_status_int(svc.get("status")), "Unknown"),
        }
        for svc in services
        if isinstance(svc, dict)
    ]
    parsed_dcs = [
        {
            "name": dc.get("name"),
            "region": dc.get("region") or dc.get("name") or "Unknown",
            "status": _safe_status_int(dc.get("status")),
            "label": EFT_STATUS_MAP.get(_safe_status_int(dc.get("status")), "Unknown"),
        }
        for dc in (datacenters or [])
        if isinstance(dc, dict)
    ]

    worst = 0
    if parsed_services:
        worst = max(worst, max(s["status"] for s in parsed_services))
    if parsed_dcs:
        worst = max(worst, max(d["status"] for d in parsed_dcs))
    g_status = _safe_status_int(global_status)
    worst = max(worst, g_status)

    cleaned_global_msg = _clean_eft_global_message(global_message)
    items: list[StatusItem] = []

    if worst > 0:
        degraded_svcs = [s for s in parsed_services if s["status"] > 0]
        degraded_dcs = [d for d in parsed_dcs if d["status"] > 0]

        uid_parts: list[str] = []
        if g_status > 0:
            uid_parts.append(f"g:{g_status}")
        uid_parts.extend(f"s:{s['name']}:{s['status']}" for s in degraded_svcs)
        uid_parts.extend(f"dc:{d['name']}:{d['status']}" for d in degraded_dcs)

        affected_names = [str(s["name"]) for s in degraded_svcs if s.get("name")] + [
            str(d["region"]) for d in degraded_dcs if d.get("region")
        ]
        if affected_names:
            title = f"{EFT_STATUS_MAP.get(worst, 'Degraded')} - " + ", ".join(affected_names[:6])
        else:
            title = EFT_GLOBAL_DEFAULT_TEXT.get(worst, EFT_STATUS_MAP.get(worst, "Degraded"))

        body_lines: list[str] = []
        if cleaned_global_msg:
            body_lines.append(cleaned_global_msg)
        body_lines.extend(f"{s['name']}: {s['label']}" for s in degraded_svcs)
        body_lines.extend(f"{d['region']}: {d['label']}" for d in degraded_dcs)
        if not body_lines:
            body_lines.append(EFT_GLOBAL_DEFAULT_TEXT.get(worst, "Servers degraded"))

        items.append(
            {
                "uid": "eft-degraded-" + ",".join(uid_parts),
                "kind": "incidents",
                "id": "services",
                "severity": "critical" if worst >= 3 else "warning",
                "maintenance_status": None,
                "title": title,
                "body": "\n".join(body_lines),
                "platforms": [],
                "created_at": None,
                "updated_at": None,
            }
        )

    for msg in messages:
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        raw_type = msg.get("type")
        if isinstance(raw_type, int) and not isinstance(raw_type, bool):
            msg_title, msg_sev = EFT_MESSAGE_TYPES.get(raw_type, ("Server message", "warning"))
        else:
            msg_title, msg_sev = ("Server message", "warning")
        items.append(
            {
                "uid": msg["uid"],
                "kind": "incidents",
                "id": msg["uid"],
                "severity": msg_sev,
                "maintenance_status": None,
                "title": msg_title,
                "body": content,
                "platforms": [],
                "created_at": msg.get("time"),
                "updated_at": msg.get("time"),
            }
        )

    return {
        "region_id": "global",
        "region_name": "Global",
        "services": parsed_services,
        "datacenters": parsed_dcs,
        "global_message": cleaned_global_msg or None,
        "worst": worst,
        "items": items,
        "ok": worst == 0 and not items,
    }


async def fetch_eft_status(
    session: aiohttp.ClientSession, *, validator: Optional[CacheValidator] = None
) -> tuple[Optional[ParsedStatus], CacheValidator, int]:
    """Fetch all four BSG status endpoints in one parallel RTT and merge.

    BSG endpoints return Cache-Control: no-cache, private with no ETag or
    Last-Modified, so ``validator`` is accepted for interface symmetry only.
    """
    results = await asyncio.gather(
        _request(session, "GET", EFT_SERVICES_URL),
        _request(session, "GET", EFT_GLOBAL_URL),
        _request(session, "GET", EFT_MESSAGES_URL),
        _request(session, "GET", EFT_DATACENTERS_URL),
        return_exceptions=True,
    )
    # Scan all results for cancellation/fatal BaseExceptions before inspecting
    # primary results so a CancelledError on a secondary endpoint is never masked.
    for res in results:
        if isinstance(res, BaseException) and not isinstance(res, FetchError):
            raise res

    sres, gres, mres, dres = results
    if isinstance(sres, FetchError):
        raise sres
    services, _, status = sres
    if services is None:
        return None, {}, status
    if not isinstance(services, list):
        raise FetchError("EFT /api/services response is not a JSON list")

    global_status: Optional[int] = None
    global_message: Optional[str] = None
    if isinstance(gres, tuple):
        gpayload, _, gstatus = gres
        if gstatus == 200 and isinstance(gpayload, dict):
            global_status = gpayload.get("status")
            raw_msg = gpayload.get("message")
            global_message = raw_msg if isinstance(raw_msg, str) else None
    elif isinstance(gres, FetchError):
        log.debug("EFT global status unavailable; using per-service data")

    messages: list[dict] = []
    if isinstance(mres, tuple):
        mpayload, _, mstatus = mres
        if mstatus == 200 and isinstance(mpayload, list):
            for msg in mpayload:
                if not isinstance(msg, dict) or msg.get("solveTime"):
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

    datacenters: list = []
    if isinstance(dres, tuple):
        dpayload, _, dstatus = dres
        if dstatus == 200 and isinstance(dpayload, list):
            datacenters = dpayload
    elif isinstance(dres, FetchError):
        log.debug("EFT datacenter list unavailable")

    return (
        parse_eft_status(
            services,
            global_status,
            messages,
            datacenters=datacenters,
            global_message=global_message,
        ),
        {},
        200,
    )


async def fetch_game_status(
    session: aiohttp.ClientSession,
    game: str,
    region: str,
    *,
    validator: Optional[CacheValidator] = None,
) -> tuple[Optional[ParsedStatus], CacheValidator, int]:
    """Unified status dispatcher for all status-monitored games."""
    cfg = GAMES[game]
    kind = cfg.get("status_kind")
    if kind == "riot":
        return await fetch_riot_status(session, game, region, validator=validator)
    if kind == "eft":
        return await fetch_eft_status(session, validator=validator)
    raise ValueError(f"Game {game!r} has no configured status source")


# ---------------------------------------------------------------------------
# Steam news & Partner Events
# ---------------------------------------------------------------------------
def _clean_steam_body(text: str, limit: int = 1400) -> str:
    """Produce readable plain-text fallback from Steam BBCode."""
    # Cap raw input before regex passes (DoS guard; plain fallback only needs `limit` chars).
    cap = max(16000, limit * 8)
    out = (text or "")[:cap]
    # Strip paired [img]...[/img] first so {STEAM_CLAN_IMAGE} URLs never leak.
    out = _STEAM_IMG_PAIR_RE.sub("", out)
    # Turn block/row/list boundaries into newlines and cell closes into pipes so
    # adjacent paragraphs/bullets never mash together into one word.
    out = _STEAM_CELL_SEP_RE.sub(" | ", out)
    out = _STEAM_BLOCK_BREAK_RE.sub("\n", out)
    out = _STEAM_KNOWN_TAG_RE.sub("", out)
    out = TAG_RE.sub("", out)
    out = out.replace("\\[", "[").replace("\\]", "]").replace("\u00a0", " ")
    out = _TRAILING_SPACE_RE.sub("\n", out)
    out = _MULTI_NL_RE.sub("\n\n", out).strip()
    if len(out) > limit:
        out = out[:limit].rsplit(" ", 1)[0] + "\u2026"
    return out


def _matches_steam_filters(
    cfg: dict,
    *,
    title: str,
    body: str,
    tags: Sequence[str] = (),
    event_type: Optional[int] = None,
) -> bool:
    """Shared filter predicate for Steam Partner Events and legacy GetNewsForApp."""
    title_pat = _compiled_filter(cfg["title_filter"]) if cfg.get("title_filter") else None
    exclude_pat = _compiled_filter(cfg["exclude_filter"]) if cfg.get("exclude_filter") else None

    title_hit = bool(title_pat and title_pat.search(title))
    if event_type is not None:
        allowed_types = cfg.get("steam_event_types") or ()
        primary_hit = (
            isinstance(event_type, int)
            and not isinstance(event_type, bool)
            and event_type in allowed_types
        )
    else:
        tag = cfg.get("steam_tag")
        primary_hit = bool(tag) and tag in tags

    if not (primary_hit or title_hit):
        return False
    if exclude_pat and exclude_pat.search(title):
        return False

    min_store_body = cfg.get("store_update_min_body")
    if min_store_body and _STORE_UPDATE_TITLE_RE.search(title):
        patch_pat = (
            _compiled_filter(cfg["store_update_patch_filter"])
            if cfg.get("store_update_patch_filter")
            else None
        )
        is_gameplay_patch = len(body) >= min_store_body or bool(
            patch_pat and patch_pat.search(body)
        )
        if not is_gameplay_patch:
            return False

    return True


def _extract_steam_capsule_image(event: dict, ab: dict) -> Optional[str]:
    clan_accountid = 0
    raw_clan = event.get("clan_steamid")
    if not isinstance(raw_clan, bool) and raw_clan is not None:
        try:
            clan_accountid = int(str(raw_clan).strip()) & 0xFFFFFFFF
        except (ValueError, TypeError):
            clan_accountid = 0
    if clan_accountid <= 0:
        clan_accountid = _safe_int(ab.get("clanid"))
    if clan_accountid <= 0:
        return None

    raw_jd = event.get("jsondata")
    if not isinstance(raw_jd, str) or not raw_jd.strip():
        return None
    try:
        jd = json.loads(raw_jd)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(jd, dict):
        return None

    caps = jd.get("localized_capsule_image")
    if not isinstance(caps, list) or not caps or not isinstance(caps[0], str):
        return None
    filename = caps[0].strip()
    if not _CAPSULE_FILENAME_RE.fullmatch(filename):
        return None
    return f"{STEAM_CLAN_IMAGE_FASTLY}{clan_accountid}/{filename}"


def parse_steam_partner_events(payload: dict, game: str) -> list[NewsItem]:
    """Parse and filter Valve's Partner Events response (ajaxgetpartnereventspageable)."""
    if not isinstance(payload, dict):
        raise FetchError(f"Steam Partner Events payload for {game} is not a JSON object")
    if payload.get("success", 1) != 1:
        raise FetchError(
            f"Steam Partner Events returned success={payload.get('success')} for {game}"
        )
    events = payload.get("events")
    if not isinstance(events, list):
        raise FetchError(f"Steam Partner Events 'events' is not a list for {game}")

    cfg = GAMES[game]
    appid = cfg["appid"]
    out: list[NewsItem] = []

    for ev in events:
        if not isinstance(ev, dict):
            continue
        event_gid = ev.get("gid")
        if not event_gid:
            continue

        ab = ev.get("announcement_body") if isinstance(ev.get("announcement_body"), dict) else {}
        title = str(ab.get("headline") or ev.get("event_name") or "").strip()
        raw_body = str(ab.get("body") or "")
        tags = ab.get("tags") if isinstance(ab.get("tags"), list) else []
        raw_et = ev.get("event_type")
        event_type = raw_et if isinstance(raw_et, int) and not isinstance(raw_et, bool) else -1

        if not _matches_steam_filters(
            cfg, title=title, body=raw_body, tags=tags, event_type=event_type
        ):
            continue

        ts = _safe_int(ab.get("posttime") or ev.get("rtime32_start_time"))
        out.append(
            {
                "uid": f"steam-ev-{event_gid}",
                "title": title,
                "url": STEAM_EVENT_CANONICAL_URL.format(appid=appid, gid=event_gid),
                "body": _clean_steam_body(raw_body),
                "raw_bbcode": raw_body,
                "author": "",
                "timestamp": ts,
                "image": _extract_steam_capsule_image(ev, ab),
            }
        )

    # Partner Events orders by rtime32_start_time DESC; sort by posttime DESC so
    # future-scheduled events never displace newer announcements.
    out.sort(key=lambda entry: entry.get("timestamp", 0), reverse=True)
    return out


async def fetch_steam_partner_events(
    session: aiohttp.ClientSession,
    game: str,
    count: int = 20,
    *,
    validator: Optional[CacheValidator] = None,
) -> tuple[Optional[list[NewsItem]], CacheValidator, int]:
    """Fetch official Steam Partner Events for a game (no ETag/IMS on Steam)."""
    cfg = GAMES[game]
    params: dict[str, Any] = {
        "clan_accountid": 0,
        "appid": cfg["appid"],
        "offset": 0,
        "count": count,
        "l": "english",
    }
    exclude_tags = cfg.get("steam_exclude_tags") or ()
    if exclude_tags:
        params["exclude_tags"] = ",".join(exclude_tags)

    payload, _, status = await _request(
        session, "GET", STEAM_PARTNER_EVENTS_URL, params=params, validator=None
    )
    if payload is None:
        return None, {}, status
    return parse_steam_partner_events(payload, game), {}, status


def parse_steam_news(payload: dict, game: str) -> list[NewsItem]:
    """Filter a legacy ISteamNews/GetNewsForApp/v2 payload down to patch notes."""
    cfg = GAMES[game]
    out: list[NewsItem] = []
    for item in (payload.get("appnews") or {}).get("newsitems") or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        tags = item.get("tags") or []
        raw_body = str(item.get("contents") or "")

        if not _matches_steam_filters(cfg, title=title, body=raw_body, tags=tags, event_type=None):
            continue

        out.append(
            {
                "uid": f"steam-{item.get('gid')}",
                "title": title,
                "url": item.get("url") or "",
                "body": _clean_steam_body(raw_body),
                "raw_bbcode": raw_body,
                "author": item.get("author") or "",
                "timestamp": _safe_int(item.get("date")),
            }
        )
    return out


async def fetch_steam_news(
    session: aiohttp.ClientSession,
    game: str,
    count: int = 20,
    *,
    validator: Optional[CacheValidator] = None,
) -> tuple[Optional[list[NewsItem]], CacheValidator, int]:
    cfg = GAMES[game]
    url = STEAM_NEWS_URL.format(appid=cfg["appid"], count=count)
    payload, _, status = await _request(session, "GET", url, validator=None)
    if payload is None:
        return None, {}, status
    return parse_steam_news(payload, game), {}, status


# ---------------------------------------------------------------------------
# Riot news (PCS v2.0 JSON primary + Next.js __NEXT_DATA__ HTML fallback)
# ---------------------------------------------------------------------------
def _extract_riot_cards(data: Any) -> list[dict]:
    """Extract article cards directly from articleCardGrid blades, with walk fallback."""
    blades: Any = None
    if isinstance(data, dict):
        if isinstance(data.get("blades"), list):
            blades = data["blades"]
        else:
            props = data.get("props")
            page_props = props.get("pageProps") if isinstance(props, dict) else None
            page = page_props.get("page") if isinstance(page_props, dict) else None
            if isinstance(page, dict) and isinstance(page.get("blades"), list):
                blades = page["blades"]

    found: list[dict] = []
    seen: set[str] = set()

    def _collect(items: Any) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            title = item.get("title")
            published = item.get("publishedAt")
            if not title or not published:
                continue
            action = item.get("action")
            payload = action.get("payload") if isinstance(action, dict) else None
            url = (payload.get("url") or "") if isinstance(payload, dict) else ""
            key = f"{title}|{published}"
            if key in seen:
                continue
            seen.add(key)
            found.append({"title": title, "publishedAt": published, "url": url, "raw": item})

    if isinstance(blades, list):
        for blade in blades:
            if isinstance(blade, dict) and blade.get("type") == "articleCardGrid":
                _collect(blade.get("items"))
        if found:
            return found

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("items"), list):
                _collect(node["items"])
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for value in node:
                _walk(value)

    _walk(data)
    return found


def parse_riot_news_data(data: Any, game: str) -> list[NewsItem]:
    """Parse Riot PCS v2.0 JSON or __NEXT_DATA__ JSON into NewsItem records."""
    if not isinstance(data, dict):
        raise FetchError(f"Riot news payload for {game} is not a JSON object")

    cfg = GAMES[game]
    expected_host = urlsplit(cfg["news_base"]).netloc.lower()
    pattern = _compiled_filter(cfg["title_filter"]) if cfg.get("title_filter") else None
    out: list[NewsItem] = []

    for item in _extract_riot_cards(data):
        title = str(item["title"]).strip()
        raw_url = str(item["url"] or "").strip()
        if not raw_url:
            continue
        if pattern and not pattern.search(title):
            continue

        full_url = urljoin(cfg["news_base"] + "/", raw_url)
        parts = urlsplit(full_url)
        if parts.scheme not in ("http", "https") or parts.netloc.lower() != expected_host:
            continue

        raw_item = item["raw"]
        desc_obj = raw_item.get("description")
        desc = (desc_obj.get("body") or "") if isinstance(desc_obj, dict) else ""

        raw_media = raw_item.get("media")
        media = raw_media if isinstance(raw_media, dict) else {}
        image = media.get("url") or ""
        if (
            isinstance(image, str)
            and image.startswith(("http://", "https://"))
            and media.get("type") == "image"
        ):
            sep = "&" if "?" in image else "?"
            image = f"{image}{sep}w=1200&h=675&fit=crop&auto=format"
        else:
            image = ""

        raw_colors = media.get("colors")
        accent = (raw_colors.get("primary") or "") if isinstance(raw_colors, dict) else ""
        raw_cat = raw_item.get("category")
        category = (raw_cat.get("title") or "") if isinstance(raw_cat, dict) else ""

        out.append(
            {
                "uid": f"riotnews-{game}-{full_url}",
                "title": title,
                "url": full_url,
                "body": TAG_RE.sub("", str(desc)).strip(),
                "author": "",
                "published_at": str(item["publishedAt"]),
                "image": image or None,
                "accent": accent or None,
                "category": str(category),
            }
        )

    out.sort(key=lambda entry: _parse_iso_ts(entry["published_at"]), reverse=True)
    return out


def parse_riot_news(html: str, game: str) -> list[NewsItem]:
    """Extract article cards from a Riot Next.js __NEXT_DATA__ HTML page."""
    match = NEXT_DATA_RE.search(html)
    if not match:
        raise FetchError(f"__NEXT_DATA__ not found for {game} (page structure changed?)")
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise FetchError(f"__NEXT_DATA__ not valid JSON for {game}") from exc
    return parse_riot_news_data(data, game)


async def fetch_riot_news(
    session: aiohttp.ClientSession,
    game: str,
    *,
    validator: Optional[CacheValidator] = None,
) -> tuple[Optional[list[NewsItem]], CacheValidator, int]:
    """Fetch Riot patch notes via PCS v2.0 JSON with automatic Next.js HTML fallback."""
    cfg = GAMES[game]
    pcs_url = RIOT_PCS_URL.format(
        channel=cfg["riot_pcs_channel"],
        locale=cfg.get("riot_locale", "en-us"),
    )
    try:
        payload, pcs_val, pcs_status = await _request(
            session, "GET", pcs_url, validator=validator, as_json=True, timeout=20
        )
        if pcs_status == 304:
            return None, pcs_val, 304
        if pcs_status == 200 and isinstance(payload, dict):
            items = parse_riot_news_data(payload, game)
            if items:
                return items, pcs_val, 200
            log.warning("Riot PCS returned 0 items for %s; falling back to HTML", game)
        else:
            log.warning("Riot PCS returned HTTP %s for %s; falling back to HTML", pcs_status, game)
    except FetchError as exc:
        log.warning("Riot PCS failed for %s (%s); falling back to HTML", game, exc)

    # Fallback: Next.js HTML page. Pass validator=None and return {} so
    # Netlify's ignored weak ETag never contaminates PCS validators.
    html, _, status = await _request(
        session, "GET", cfg["news_url"], validator=None, as_json=False, timeout=30
    )
    if html is None:
        return None, {}, status
    return parse_riot_news(html, game), {}, status


# ---------------------------------------------------------------------------
# Escape from Tarkov official BSG patch notes (JSON API, type_id=2)
# ---------------------------------------------------------------------------
def parse_eft_news(payload: dict) -> list[NewsItem]:
    """Parse BSG's official /site/api/v1/news-list/2/en patch-notes payload."""
    if not isinstance(payload, dict) or not isinstance(payload.get("list"), list):
        raise FetchError("EFT BSG news payload missing 'list' array")

    out: list[tuple[tuple[float, int], NewsItem]] = []
    for entry in payload["list"]:
        if not isinstance(entry, dict):
            continue
        item_id = entry.get("id")
        if item_id is None or item_id == "":
            continue

        title = str(entry.get("name") or "").strip()
        published_at = str(entry.get("date") or "").strip()
        raw_html = str(entry.get("descr") or "")
        small_descr = str(entry.get("smallDescr") or "").strip()
        body = small_descr or re.sub(r"\s+", " ", TAG_RE.sub(" ", raw_html)).strip()[:1400]

        fallback_url = f"{EFT_SITE_BASE}/news/id/{item_id}"
        raw_link = entry.get("link")
        if isinstance(raw_link, str) and raw_link.strip():
            resolved = urljoin(EFT_SITE_BASE + "/", raw_link.strip())
            url = resolved if urlsplit(resolved).scheme in ("http", "https") else fallback_url
        else:
            url = fallback_url

        image: Optional[str] = None
        raw_img = entry.get("ico") or entry.get("thumb")
        if isinstance(raw_img, str) and raw_img.strip():
            resolved_img = urljoin(EFT_SITE_BASE + "/", raw_img.strip())
            if urlsplit(resolved_img).scheme in ("http", "https"):
                image = resolved_img

        sort_key = (_parse_iso_ts(published_at), _safe_int(item_id))
        out.append(
            (
                sort_key,
                {
                    "uid": f"eft-bsg-{item_id}",
                    "title": title or f"Patch #{item_id}",
                    "url": url,
                    "body": body,
                    "raw_html": raw_html,
                    "author": "",
                    "published_at": published_at,
                    "image": image,
                },
            )
        )

    out.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in out]


async def fetch_eft_news(
    session: aiohttp.ClientSession, *, validator: Optional[CacheValidator] = None
) -> tuple[Optional[list[NewsItem]], CacheValidator, int]:
    """Fetch EFT official Patch Notes JSON directly from BSG.

    Intentionally does NOT fall back to Steam on WAF/HTTP errors: BSG uses
    ``eft-bsg-*`` UIDs whereas Steam uses ``steam-ev-*``, so mixing UID spaces
    during transient errors would re-announce historical patches.
    """
    payload, _, status = await _request(
        session, "GET", EFT_NEWS_API_URL, validator=None, as_json=True, timeout=20
    )
    if payload is None:
        return None, {}, status
    return parse_eft_news(payload), {}, status


# ---------------------------------------------------------------------------
# Arena Breakout: Infinite official Level Infinite GPTS CMS (JSON API)
# ---------------------------------------------------------------------------
def parse_abi_news(payload: dict, game: str = "abi") -> list[NewsItem]:
    """Parse Level Infinite GetContentByLabel response into NewsItems."""
    if not isinstance(payload, dict):
        raise FetchError("ABI CMS payload is not a JSON object")
    if payload.get("code") != 0:
        raise FetchError(f"ABI CMS returned error code={payload.get('code')}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise FetchError("ABI CMS payload missing 'data' object")
    if data.get("result") not in (None, 0):
        raise FetchError(
            f"ABI CMS returned data.result={data.get('result')} ({data.get('errmsg')})"
        )
    entries = data.get("info_content")
    if not isinstance(entries, list):
        raise FetchError("ABI CMS payload missing 'data.info_content' list")

    cfg = GAMES[game]
    title_pat = _compiled_filter(cfg["title_filter"]) if cfg.get("title_filter") else None
    exclude_pat = _compiled_filter(cfg["exclude_filter"]) if cfg.get("exclude_filter") else None

    out: list[NewsItem] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        content_id = str(entry.get("content_id") or "").strip()
        if not content_id:
            continue
        title = str(entry.get("title") or "").strip()
        if title_pat and not title_pat.search(title):
            continue
        if exclude_pat and exclude_pat.search(title):
            continue

        image: Optional[str] = None
        pic_urls = entry.get("pic_urls")
        if isinstance(pic_urls, list):
            for candidate in pic_urls:
                if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
                    image = candidate
                    break

        out.append(
            {
                "uid": f"abi-cms-{content_id}",
                "content_id": content_id,
                "title": title,
                "url": ABI_ARTICLE_URL.format(content_id=content_id),
                "body": str(entry.get("content_part") or "").strip(),
                "author": "",
                "timestamp": _safe_int(entry.get("pub_timestamp")),
                "image": image,
            }
        )

    out.sort(key=lambda item: item.get("timestamp", 0), reverse=True)
    return out


async def fetch_abi_news(
    session: aiohttp.ClientSession,
    game: str = "abi",
    *,
    validator: Optional[CacheValidator] = None,
) -> tuple[Optional[list[NewsItem]], CacheValidator, int]:
    """Fetch ABI official announcements via Level Infinite GPTS CMS."""
    # Server silently caps results at 10 when get_num > 20, and requires
    # language to be a JSON list ["en"] and X-Source to be "pc_web".
    body = {
        "language": ["en"],
        "gameid": ABI_CMS_GAME_ID,
        "primary_label_id": ABI_CMS_PRIMARY_LABEL,
        "secondary_label_id": ABI_CMS_SECONDARY_LABEL,
        "offset": 0,
        "get_num": 20,
        "sort_by_list": [{"key": "start_timestamp", "asc": 0}],
    }
    payload, _, status = await _request(
        session,
        "POST",
        ABI_CMS_LIST_URL,
        headers=ABI_CMS_HEADERS,
        json_body=body,
        validator=None,
        as_json=True,
        timeout=20,
    )
    if payload is None:
        return None, {}, status
    return parse_abi_news(payload, game), {}, status


async def fetch_abi_news_detail(session: aiohttp.ClientSession, content_id: str) -> Optional[str]:
    """Lazily fetch full HTML body for one ABI article; returns None on any error."""
    if not content_id:
        return None
    try:
        payload, _, status = await _request(
            session,
            "POST",
            ABI_CMS_DETAIL_URL,
            headers=ABI_CMS_HEADERS,
            json_body={"content_id": content_id},
            validator=None,
            as_json=True,
            timeout=20,
        )
    except FetchError as exc:
        log.debug("ABI detail fetch failed for %s: %s", content_id, exc)
        return None
    if status != 200 or not isinstance(payload, dict) or payload.get("code") != 0:
        return None
    data = payload.get("data")
    if not isinstance(data, dict) or data.get("result") not in (None, 0):
        return None
    html = data.get("content")
    if not isinstance(html, str) or not html.strip():
        return None
    return html


async def fetch_game_news(
    session: aiohttp.ClientSession,
    game: str,
    *,
    validator: Optional[CacheValidator] = None,
) -> tuple[Optional[list[NewsItem]], CacheValidator, int]:
    """Unified news dispatcher for every configured game."""
    cfg = GAMES[game]
    kind = cfg["news_kind"]
    if kind == "riot":
        return await fetch_riot_news(session, game, validator=validator)
    if kind == "steam_events":
        return await fetch_steam_partner_events(session, game, validator=validator)
    if kind == "eft_bsg":
        return await fetch_eft_news(session, validator=validator)
    if kind == "abi_cms":
        return await fetch_abi_news(session, game, validator=validator)
    return await fetch_steam_news(session, game, validator=validator)


# ---------------------------------------------------------------------------
# Location filter (Valorant EMEA noise control)
# ---------------------------------------------------------------------------
# Riot embeds the affected game-server location ONLY in the incident body:
#   "The following game server location is currently unavailable while we
#    investigate an issue: Dubai"
LOCATION_RE = re.compile(
    r"locations? (?:is|are) currently unavailable.*?:\s*(?P<loc>.+)$", re.I | re.M
)


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
    ok is recomputed from surviving non-scheduled items so a lingering filtered
    incident or future scheduled notice cannot suppress recovery.
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
    worst_service = parsed.get("worst", 0) or 0
    has_active = any(it.get("maintenance_status") != "scheduled" for it in kept)
    out["ok"] = (not has_active) and (worst_service == 0)
    return out  # type: ignore[return-value]
