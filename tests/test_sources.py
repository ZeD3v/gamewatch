"""Pure unit tests for HTTP transport, upstream parsers, fetchers, and location filtering."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
import pytest

from gamewatch import sources
from gamewatch.constants import ABI_CMS_GAME_ID, ABI_CMS_HEADERS, ABI_CMS_PRIMARY_LABEL
from gamewatch.sources import (
    FetchError,
    _clean_steam_body,
    _parse_retry_after,
    _redact_url,
    _request,
    extract_location,
    fetch_abi_news,
    fetch_abi_news_detail,
    fetch_eft_status,
    fetch_riot_news,
    filter_status_view,
    location_allowed,
    parse_abi_news,
    parse_eft_news,
    parse_eft_status,
    parse_riot_news,
    parse_riot_news_data,
    parse_riot_status,
    parse_steam_news,
    parse_steam_partner_events,
)


# ---------------------------------------------------------------------------
# Fake aiohttp helpers for unit-testing _request without hitting the network
# ---------------------------------------------------------------------------
class _FakeContent:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def iter_chunked(self, _size: int):
        for chunk in self._chunks:
            yield chunk


class _FakeResponse:
    def __init__(
        self,
        status: int = 200,
        body: bytes = b"{}",
        *,
        headers: dict[str, str] | None = None,
        charset: str | None = None,
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self.charset = charset
        self.content = _FakeContent(chunks if chunks is not None else [body])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, outcomes: list[Any]) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


# ---------------------------------------------------------------------------
# Task 2: HTTP Transport (_redact_url, _parse_retry_after, _request)
# ---------------------------------------------------------------------------
def test_redact_url_masks_secret_params() -> None:
    url = "https://api.steampowered.com/x?key=SECRET123&appid=730&api_key=ABC&token=XYZ&locale=en"
    redacted = _redact_url(url)
    assert "SECRET123" not in redacted
    assert "ABC" not in redacted
    assert "XYZ" not in redacted
    assert "appid=730" in redacted
    assert "locale=en" in redacted
    assert "key=REDACTED" in redacted


def test_parse_retry_after_handles_seconds_http_date_naive_and_invalid() -> None:
    now = datetime(2026, 11, 19, 20, 0, 0, tzinfo=timezone.utc)
    assert _parse_retry_after("3.5", now=now) == pytest.approx(3.5)
    assert _parse_retry_after("-5", now=now) == 0.0
    assert _parse_retry_after("nan", now=now) is None
    assert _parse_retry_after("inf", now=now) is None
    assert _parse_retry_after("Thu, 19 Nov 2026 20:00:05 GMT", now=now) == pytest.approx(5.0)
    assert _parse_retry_after("Thu, 19 Nov 2026 19:59:50 GMT", now=now) == 0.0
    # Obsolete RFC 9110 asctime format produces a naive datetime; must not raise TypeError
    assert _parse_retry_after("Thu Nov 19 20:00:04 2026", now=now) == pytest.approx(4.0)
    assert _parse_retry_after("not-a-date", now=now) is None


async def test_request_sends_etag_and_last_modified_handles_304_without_mutating() -> None:
    session = _FakeSession(
        [
            _FakeResponse(
                304,
                b"",
                headers={"ETag": '"new-etag"'},
            )
        ]
    )
    orig_validator = {"etag": '"old-etag"', "last_modified": "Thu, 19 Nov 2026 10:00:00 GMT"}
    snapshot = dict(orig_validator)

    payload, new_val, status = await _request(
        session,
        "GET",
        "https://example.com/status.json",
        validator=orig_validator,  # type: ignore[arg-type]
    )
    assert status == 304
    assert payload is None
    assert len(session.calls) == 1
    sent_headers = session.calls[0]["headers"]
    assert sent_headers["If-None-Match"] == '"old-etag"'
    assert sent_headers["If-Modified-Since"] == "Thu, 19 Nov 2026 10:00:00 GMT"
    # Input validator not mutated; refreshed ETag merged into new dict
    assert orig_validator == snapshot
    assert new_val == {"etag": '"new-etag"', "last_modified": "Thu, 19 Nov 2026 10:00:00 GMT"}


async def test_request_retries_transient_503_and_429_respecting_retry_after(monkeypatch) -> None:
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(sources.asyncio, "sleep", fake_sleep)

    # 503 -> 429 (Retry-After: 1) -> 200
    s1 = _FakeSession(
        [
            _FakeResponse(503, b"unavailable"),
            _FakeResponse(429, b"slow down", headers={"Retry-After": "1"}),
            _FakeResponse(200, b'{"ok": true}', headers={"ETag": '"v1"'}),
        ]
    )
    payload, val, status = await _request(s1, "GET", "https://example.com/a", retries=2)  # type: ignore[arg-type]
    assert status == 200
    assert payload == {"ok": True}
    assert val == {"etag": '"v1"'}
    assert len(s1.calls) == 3
    assert slept[1] == pytest.approx(1.0)

    # 429 with HTTP-date Retry-After within cap retries
    slept.clear()
    future_hdr = (datetime.now(timezone.utc) + timedelta(seconds=2)).strftime(
        "%a, %d %b %Y %H:%M:%S GMT"
    )
    s_date = _FakeSession(
        [
            _FakeResponse(429, b"slow", headers={"Retry-After": future_hdr}),
            _FakeResponse(200, b'{"ok": 1}'),
        ]
    )
    payload, _, status = await _request(s_date, "GET", "https://example.com/d", retries=1)  # type: ignore[arg-type]
    assert status == 200 and payload == {"ok": 1}
    assert len(slept) == 1 and 0.0 <= slept[0] <= 10.0

    # 429 with Retry-After > MAX_RETRY_AFTER aborts immediately without sleeping
    slept.clear()
    s_huge = _FakeSession([_FakeResponse(429, b"too long", headers={"Retry-After": "3600"})])
    orig_v = {"etag": '"keep"'}
    payload, val, status = await _request(
        s_huge,
        "GET",
        "https://example.com/huge",
        validator=orig_v,
        retries=2,  # type: ignore[arg-type]
    )
    assert status == 429
    assert payload is None
    assert val == {"etag": '"keep"'}
    assert len(s_huge.calls) == 1
    assert slept == []

    # Exhausted 503 returns (None, preserved_validator, 503)
    slept.clear()
    s_exhaust = _FakeSession([_FakeResponse(503, b"x"), _FakeResponse(503, b"x")])
    payload, val, status = await _request(
        s_exhaust,
        "GET",
        "https://example.com/down",
        validator=orig_v,
        retries=1,  # type: ignore[arg-type]
    )
    assert status == 503 and payload is None and val == {"etag": '"keep"'}
    assert len(s_exhaust.calls) == 2

    # 404 and 403 do NOT retry
    for code in (404, 403):
        s_no = _FakeSession([_FakeResponse(code, b"no")])
        payload, _, status = await _request(s_no, "GET", "https://example.com/x", retries=2)  # type: ignore[arg-type]
        assert status == code and payload is None and len(s_no.calls) == 1


async def test_request_rejects_oversized_response_fast_and_streaming() -> None:
    # Fast-reject via Content-Length when uncompressed
    s_fast = _FakeSession([_FakeResponse(200, b"x", headers={"Content-Length": "5000"})])
    with pytest.raises(FetchError, match="exceeded"):
        await _request(s_fast, "GET", "https://example.com/big", max_bytes=100, retries=2)  # type: ignore[arg-type]
    assert len(s_fast.calls) == 1  # never retried

    # Streaming reject (compressed or chunked)
    s_stream = _FakeSession(
        [
            _FakeResponse(
                200,
                headers={"Content-Encoding": "gzip"},
                chunks=[b"a" * 60, b"b" * 60],
            )
        ]
    )
    with pytest.raises(FetchError, match="exceeded"):
        await _request(s_stream, "GET", "https://example.com/big2", max_bytes=100, retries=2)  # type: ignore[arg-type]
    assert len(s_stream.calls) == 1


async def test_request_supports_post_json_bom_with_utf8_charset_and_redacts_errors() -> None:
    bom_json = b'\xef\xbb\xbf{"status": 0}'
    s_post = _FakeSession(
        [
            _FakeResponse(
                200,
                bom_json,
                headers={"Content-Type": "text/html; charset=UTF-8"},
                charset="UTF-8",
            )
        ]
    )
    payload, _, status = await _request(
        s_post,
        "POST",
        "https://example.com/api",
        json_body={"hello": "world"},
        headers={"X-Source": "pc_web"},
    )  # type: ignore[arg-type]
    assert status == 200
    assert payload == {"status": 0}
    assert s_post.calls[0]["method"] == "POST"
    assert s_post.calls[0]["json"] == {"hello": "world"}
    assert s_post.calls[0]["headers"]["X-Source"] == "pc_web"

    # Malformed JSON raises FetchError with redacted URL secret
    s_bad = _FakeSession([_FakeResponse(200, b"not-json")])
    with pytest.raises(FetchError) as exc_info:
        await _request(s_bad, "GET", "https://example.com/api?key=TOPSECRET", retries=2)  # type: ignore[arg-type]
    assert "TOPSECRET" not in str(exc_info.value)
    assert "key=REDACTED" in str(exc_info.value)
    assert len(s_bad.calls) == 1

    # Unknown charset (LookupError) is wrapped in FetchError
    s_charset = _FakeSession([_FakeResponse(200, b'{"ok": 1}', charset="not-a-real-codec")])
    with pytest.raises(FetchError, match="malformed"):
        await _request(s_charset, "GET", "https://example.com/api", retries=2)  # type: ignore[arg-type]
    assert len(s_charset.calls) == 1


async def test_request_retries_timeout_and_client_error_then_raises(monkeypatch) -> None:
    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(sources.asyncio, "sleep", fake_sleep)

    # Timeout recovers on retry
    s_to_ok = _FakeSession([asyncio.TimeoutError(), _FakeResponse(200, b'{"ok": true}')])
    payload, _, status = await _request(s_to_ok, "GET", "https://example.com/t", retries=1)  # type: ignore[arg-type]
    assert status == 200 and payload == {"ok": True} and len(s_to_ok.calls) == 2

    # Exhausted TimeoutError raises FetchError
    s_to_fail = _FakeSession([asyncio.TimeoutError(), asyncio.TimeoutError()])
    with pytest.raises(FetchError, match="timeout"):
        await _request(s_to_fail, "GET", "https://example.com/t?key=SECRET", retries=1)  # type: ignore[arg-type]
    assert len(s_to_fail.calls) == 2

    # Exhausted ClientError raises FetchError with redacted secret
    s_ce = _FakeSession([aiohttp.ClientError("reset"), aiohttp.ClientError("reset")])
    with pytest.raises(FetchError, match="client error") as exc_info:
        await _request(s_ce, "GET", "https://example.com/c?token=SECRET", retries=1)  # type: ignore[arg-type]
    assert "SECRET" not in str(exc_info.value)
    assert len(s_ce.calls) == 2


# ---------------------------------------------------------------------------
# Task 3: Status Parsers & Fetchers (Riot + EFT + Location Filter)
# ---------------------------------------------------------------------------
def test_parse_riot_status_normalises_incidents_and_maintenances() -> None:
    payload = {
        "id": "EUN1",
        "name": "EU Nordic & East",
        "incidents": [
            {
                "id": 42,
                "incident_severity": "warning",
                "platforms": ["windows"],
                "created_at": "2026-11-19T10:00:00+00:00",
                "updated_at": "2026-11-19T10:05:00+00:00",
                "titles": [
                    {"locale": "fr_FR", "content": "Erreur"},
                    {"locale": "en_US", "content": "Login issues"},
                ],
                "updates": [
                    {
                        "id": 100,
                        "created_at": "2026-11-19T10:00:00+00:00",
                        "updated_at": "2026-11-19T10:00:00+00:00",
                        "publish": True,
                        "publish_locations": ["riotstatus"],
                        "translations": [{"locale": "en_US", "content": "Initial note"}],
                    },
                    {
                        "id": 101,
                        "created_at": "2026-11-19T10:05:00+00:00",
                        "updated_at": "2026-11-19T10:05:00+00:00",
                        "publish": True,
                        "publish_locations": ["riotclient", "riotstatus"],
                        "translations": [{"locale": "en_US", "content": "Investigating now."}],
                    },
                ],
            }
        ],
        "maintenances": [],
    }
    parsed = parse_riot_status(payload)
    assert parsed["ok"] is False
    assert parsed["region_id"] == "EUN1"
    assert parsed["region_name"] == "EU Nordic & East"
    assert len(parsed["items"]) == 1
    item = parsed["items"][0]
    assert item["uid"] == "riot-incidents-42-101-2026-11-19T10:05:00+00:00"
    assert item["title"] == "Login issues"
    assert item["body"] == "Investigating now."
    assert item["severity"] == "warning"
    assert item["maintenance_status"] is None


def test_parse_riot_status_filters_publish_locations_and_publish_false() -> None:
    payload = {
        "id": "EUN1",
        "name": "EU Nordic & East",
        "incidents": [
            {
                "id": 13871,
                "incident_severity": "info",
                "titles": [
                    {"locale": "en_US", "content": "Your Default Settings May Have Changed"}
                ],
                "updates": [
                    {
                        "id": 24807,
                        "created_at": "2026-11-18T22:13:16+00:00",
                        "publish": True,
                        "publish_locations": ["game", "riotclient"],
                        "translations": [{"locale": "en_US", "content": "Client-only tip"}],
                    }
                ],
            },
            {
                "id": 13831,
                "incident_severity": "critical",
                "titles": [{"locale": "en_US", "content": "Login Issues"}],
                "updates": [
                    {
                        "id": 24750,
                        "created_at": "2026-11-13T10:00:00+00:00",
                        "publish": True,
                        "publish_locations": ["riotclient"],
                        "translations": [{"locale": "en_US", "content": "Old client banner"}],
                    }
                ],
            },
            {
                "id": 13900,
                "incident_severity": "warning",
                "titles": [{"locale": "en_US", "content": "Unpublished draft"}],
                "updates": [
                    {
                        "id": 25000,
                        "created_at": "2026-11-19T10:00:00+00:00",
                        "publish": False,
                        "publish_locations": ["riotstatus"],
                        "translations": [{"locale": "en_US", "content": "Hidden"}],
                    }
                ],
            },
        ],
        "maintenances": [],
    }
    parsed = parse_riot_status(payload)
    assert parsed["ok"] is True
    assert parsed["items"] == []


def test_parse_riot_status_picks_newest_update_regardless_of_order_or_tz() -> None:
    # Descending order + mixed timezone offsets (+02:00 vs Z) + non-numeric id
    def _build(raw_updated_at: str | None, reverse: bool) -> dict:
        updates = [
            {
                "id": "u-new",
                "created_at": "2026-11-19T12:30:00+02:00",  # 10:30 UTC (newest!)
                "updated_at": "2026-11-19T12:30:00+02:00",
                "publish_locations": ["riotstatus"],
                "translations": [{"locale": "en_US", "content": "Newest text"}],
            },
            {
                "id": "u-old",
                "created_at": "2026-11-19T10:00:00Z",  # 10:00 UTC
                "updated_at": "2026-11-19T10:00:00Z",
                "publish_locations": ["riotstatus"],
                "translations": [{"locale": "en_US", "content": "Older text"}],
            },
        ]
        if reverse:
            updates.reverse()
        return {
            "id": "EU",
            "name": "Europe",
            "incidents": [
                {
                    "id": 50,
                    "incident_severity": "warning",
                    "created_at": "2026-11-19T10:00:00Z",
                    "updated_at": raw_updated_at,
                    "titles": [{"locale": "en_US", "content": "Degraded"}],
                    "updates": updates,
                }
            ],
            "maintenances": [],
        }

    desc = parse_riot_status(_build("2026-11-19T11:00:00Z", reverse=False))
    asc = parse_riot_status(_build("2026-11-19T19:00:00Z", reverse=True))
    assert desc["items"][0]["body"] == "Newest text"
    assert asc["items"][0]["body"] == "Newest text"
    # Silent raw.updated_at edit did NOT change uid
    assert desc["items"][0]["uid"] == asc["items"][0]["uid"]


def test_parse_riot_status_handles_maintenances_and_archive_at() -> None:
    now = datetime(2026, 11, 19, 12, 0, 0, tzinfo=timezone.utc)
    upd = {
        "id": 1,
        "created_at": "2026-11-19T08:00:00Z",
        "publish_locations": ["riotstatus"],
        "translations": [{"locale": "en_US", "content": "Maintenance details."}],
    }
    payload = {
        "id": "EUN1",
        "name": "EU Nordic & East",
        "incidents": [
            {
                "id": 1,
                "incident_severity": "critical",
                "archive_at": "2026-11-19T11:00:00",  # naive past -> expired
                "titles": [{"locale": "en_US", "content": "Expired incident"}],
                "updates": [upd],
            }
        ],
        "maintenances": [
            {
                "id": 10,
                "maintenance_status": "complete",
                "titles": [{"locale": "en_US", "content": "Done"}],
                "updates": [upd],
            },
            {
                "id": 11,
                "maintenance_status": "scheduled",
                "archive_at": "not-a-timestamp",  # malformed archive_at -> fail-open
                "titles": [{"locale": "en_US", "content": "Planned tonight"}],
                "updates": [upd],
            },
        ],
    }
    sched_only = parse_riot_status(payload, now=now)
    assert sched_only["ok"] is True
    assert len(sched_only["items"]) == 1
    assert sched_only["items"][0]["severity"] == "info"
    assert sched_only["items"][0]["maintenance_status"] == "scheduled"
    assert "-scheduled-" in sched_only["items"][0]["uid"]

    # Transitioning scheduled -> in_progress flips ok=False, severity=warning, new uid
    payload["maintenances"][1]["maintenance_status"] = "in_progress"
    in_prog = parse_riot_status(payload, now=now)
    assert in_prog["ok"] is False
    assert in_prog["items"][0]["severity"] == "warning"
    assert in_prog["items"][0]["maintenance_status"] == "in_progress"
    assert in_prog["items"][0]["uid"] != sched_only["items"][0]["uid"]


def test_parse_eft_status_handles_degraded_and_non_int_global() -> None:
    services = [
        {"name": "Matching", "status": 0},
        {"name": "Backend", "status": 3},
    ]
    messages = [
        {"uid": "eft-msg-1", "content": "Server maintenance active", "time": "2026-11-19T12:00:00Z"}
    ]
    parsed = parse_eft_status(services, global_status="not-an-int", messages=messages)  # type: ignore[arg-type]
    assert parsed["ok"] is False
    assert parsed["worst"] == 3
    assert len(parsed["items"]) == 2
    assert parsed["items"][0]["severity"] == "critical"
    assert "Backend" in parsed["items"][0]["title"]
    assert parsed["items"][1]["uid"] == "eft-msg-1"
    assert parsed["items"][1]["title"] == "Server message"
    assert parsed["items"][1]["severity"] == "warning"


def test_parse_eft_status_datacenters_global_message_uid_escalation_and_message_types() -> None:
    healthy_svcs = [{"name": "Authentication", "status": 0}, {"name": "Matching", "status": True}]
    # Datacenter-only degradation
    dcs = [
        {"name": "Server EUR", "region": "Europe", "status": 2},
        {"name": "Server AF", "region": "Africa", "status": 0},
    ]
    dc_parsed = parse_eft_status(healthy_svcs, global_status=0, messages=[], datacenters=dcs)
    assert dc_parsed["ok"] is False
    assert dc_parsed["worst"] == 2
    assert "Europe" in dc_parsed["items"][0]["title"]
    assert "dc:Server EUR:2" in dc_parsed["items"][0]["uid"]
    assert len(dc_parsed["datacenters"]) == 2

    # Global-only escalation 1 -> 3 must produce distinct UIDs and ignore "Access denied"
    g1 = parse_eft_status(
        healthy_svcs, global_status=1, messages=[], global_message="Access denied."
    )
    g3 = parse_eft_status(
        healthy_svcs, global_status=3, messages=[], global_message="Major backend outage"
    )
    assert g1["items"][0]["uid"] == "eft-degraded-g:1"
    assert g3["items"][0]["uid"] == "eft-degraded-g:3"
    assert g1["items"][0]["title"] == "Servers on update"
    assert "Access denied" not in g1["items"][0]["body"]
    assert g3["items"][0]["body"] == "Major backend outage"
    assert g3["items"][0]["severity"] == "critical"

    # Message types 0..3 + bool guard
    msgs = [
        {"uid": "m0", "content": "Note", "type": 0, "time": None},
        {"uid": "m1", "content": "Patching", "type": 1, "time": None},
        {"uid": "m2", "content": "Lag", "type": 2, "time": None},
        {"uid": "m3", "content": "Down", "type": 3, "time": None},
        {"uid": "mb", "content": "Bool type", "type": True, "time": None},
    ]
    mp = parse_eft_status(healthy_svcs, global_status=0, messages=msgs)
    got = [(i["title"], i["severity"]) for i in mp["items"]]
    assert got == [
        ("Info", "info"),
        ("Update installation", "warning"),
        ("Server issues", "warning"),
        ("Server issues", "critical"),
        ("Server message", "warning"),
    ]


async def test_fetch_eft_status_runs_all_four_in_parallel_and_validates_services(
    monkeypatch,
) -> None:
    called_urls: list[str] = []

    async def fake_request(session, method, url, **kwargs):
        called_urls.append(url)
        if url.endswith("/services"):
            return [{"name": "Auth", "status": 0}], {}, 200
        if url.endswith("/global/status"):
            return {"status": 0, "message": ""}, {}, 200
        if url.endswith("/message/list"):
            return (
                [{"_id": "1", "content": "Resolved", "solveTime": "2026-11-19T00:00:00Z"}],
                {},
                200,
            )
        if url.endswith("/datacenter/list"):
            raise FetchError("dc blip")
        raise AssertionError(url)

    monkeypatch.setattr(sources, "_request", fake_request)
    parsed, val, status = await fetch_eft_status(object())  # type: ignore[arg-type]
    assert status == 200 and val == {}
    assert parsed is not None and parsed["ok"] is True
    assert len(called_urls) == 4

    # Non-list /api/services must raise FetchError instead of returning ok=True
    async def bad_services(session, method, url, **kwargs):
        if url.endswith("/services"):
            return {"error": "maintenance"}, {}, 200
        return [], {}, 200

    monkeypatch.setattr(sources, "_request", bad_services)
    with pytest.raises(FetchError):
        await fetch_eft_status(object())  # type: ignore[arg-type]

    # CancelledError on a secondary endpoint must propagate even if primary failed
    async def cancelled_secondary(session, method, url, **kwargs):
        if url.endswith("/services"):
            raise FetchError("primary down")
        if url.endswith("/datacenter/list"):
            raise asyncio.CancelledError()
        return [], {}, 200

    monkeypatch.setattr(sources, "_request", cancelled_secondary)
    with pytest.raises(asyncio.CancelledError):
        await fetch_eft_status(object())  # type: ignore[arg-type]


def test_location_extraction_and_filtering_with_plural_and_scheduled() -> None:
    dubai_body = (
        "The following game server location is currently unavailable "
        "while we investigate an issue: Dubai"
    )
    plural_body = (
        "The following game server locations are currently unavailable "
        "while we investigate an issue: Dubai, Warsaw"
    )
    assert extract_location({"body": dubai_body, "title": ""}) == "Dubai"
    assert extract_location({"body": dubai_body + "\nMore info soon.", "title": ""}) == "Dubai"
    assert extract_location({"body": plural_body, "title": ""}) == "Dubai, Warsaw"
    assert extract_location({"body": "Login issues for all players.", "title": "Login"}) is None

    dubai_item = {"uid": "loc-Dubai", "body": dubai_body, "title": "Game Server Unavailable"}
    warsaw_item = {
        "uid": "loc-Warsaw",
        "body": dubai_body.replace("Dubai", "Warsaw"),
        "title": "Game Server Unavailable",
    }
    plural_item = {"uid": "loc-plural", "body": plural_body, "title": "Game Server Unavailable"}
    unlocated = {"uid": "loc-none", "body": "Players cannot log in.", "title": "Outage"}
    scheduled = {
        "uid": "sched-1",
        "body": "Planned maintenance tomorrow.",
        "title": "Scheduled",
        "maintenance_status": "scheduled",
        "severity": "info",
    }

    assert location_allowed(dubai_item, []) is True
    assert location_allowed(dubai_item, ["warsaw"]) is False
    assert location_allowed(warsaw_item, ["warsaw"]) is True
    assert location_allowed(plural_item, ["warsaw"]) is True
    assert location_allowed(unlocated, ["warsaw"]) is True

    parsed = {
        "region_id": "EU",
        "region_name": "Europe",
        "items": [dubai_item, warsaw_item],
        "ok": False,
    }
    view = filter_status_view(parsed, ["warsaw"])
    assert [i["uid"] for i in view["items"]] == ["loc-Warsaw"]
    assert view["filtered_count"] == 1
    assert view["ok"] is False

    # Dubai filtered out while global scheduled maintenance survives -> ok MUST flip True
    sched_view = filter_status_view(
        {
            "region_id": "EU",
            "region_name": "Europe",
            "items": [dubai_item, scheduled],
            "ok": False,
        },
        ["warsaw"],
    )
    assert [i["uid"] for i in sched_view["items"]] == ["sched-1"]
    assert sched_view["filtered_count"] == 1
    assert sched_view["ok"] is True


# ---------------------------------------------------------------------------
# Task 4: Riot News (PCS v2.0 Primary + HTML Fallback + Host Enforcement)
# ---------------------------------------------------------------------------
def _riot_card(title: str, url: str, published: str = "2026-11-17T19:00:00.000Z") -> dict:
    return {
        "title": title,
        "publishedAt": published,
        "action": {"payload": {"url": url}},
        "description": {"body": "<p>Balance updates.</p>"},
        "media": {
            "type": "image",
            "url": "https://cmsassets.rgpub.io/sanity/images/hero.jpg?accountingTag=LoL",
            "colors": {"primary": "#c89b3c"},
        },
        "category": {"title": "Game Updates"},
    }


def test_parse_riot_news_data_extracts_article_card_grid_and_enforces_exact_host() -> None:
    pcs_payload = {
        "blades": [
            {"type": "masthead", "items": [_riot_card("Patch 99.99 Notes", "/en-us/fake")]},
            {
                "type": "articleCardGrid",
                "items": [
                    _riot_card(
                        "League of Legends Patch 26.23 Notes",
                        "/en-us/news/game-updates/patch-26-23-notes",
                    ),
                    # Cross-subdomain TFT leak must be rejected for lol!
                    _riot_card(
                        "Teamfight Tactics patch 14.6 notes",
                        "https://teamfighttactics.leagueoflegends.com/en-us/news/game-updates/tft",
                    ),
                    # Protocol-relative / javascript / empty URLs rejected
                    _riot_card("Patch 26.22 Notes", "//evil.example.com/patch"),
                    _riot_card("Patch 26.21 Notes", "javascript:alert(1)"),
                    _riot_card("Patch 26.20 Notes", ""),
                ],
            },
        ]
    }
    items = parse_riot_news_data(pcs_payload, "lol")
    assert len(items) == 1
    assert (
        items[0]["url"]
        == "https://www.leagueoflegends.com/en-us/news/game-updates/patch-26-23-notes"
    )
    assert items[0]["uid"] == f"riotnews-lol-{items[0]['url']}"
    assert "accountingTag=LoL&w=1200&h=675&fit=crop&auto=format" in (items[0]["image"] or "")
    assert items[0]["accent"] == "#c89b3c"
    assert items[0]["body"] == "Balance updates."


def test_parse_riot_news_falls_back_to_recursive_walk_when_blade_type_changes() -> None:
    next_data = {
        "props": {
            "pageProps": {
                "items": [
                    _riot_card(
                        "League of Legends Patch 26.23 Notes",
                        "/en-us/news/game-updates/patch-26-23/",
                    ),
                    {
                        "title": "Patch 26.23 Rundown Video",
                        "publishedAt": "2026-11-17T18:00:00.000Z",
                        "action": {"payload": {"url": "https://youtube.com/watch?v=abc"}},
                    },
                ]
            }
        }
    }
    html = (
        '<html><script id="__NEXT_DATA__" type="application/json">'
        f"{json.dumps(next_data)}"
        "</script></html>"
    )
    items = parse_riot_news(html, "lol")
    assert len(items) == 1
    assert items[0]["url"] == "https://www.leagueoflegends.com/en-us/news/game-updates/patch-26-23/"


def test_parse_riot_news_raises_when_next_data_missing() -> None:
    with pytest.raises(FetchError):
        parse_riot_news("<html><body>No hydration blob</body></html>", "lol")


async def test_fetch_riot_news_pcs_primary_isolates_validators_and_falls_back(monkeypatch) -> None:
    pcs_payload = {
        "blades": [
            {
                "type": "articleCardGrid",
                "items": [_riot_card("League of Legends Patch 26.23 Notes", "/en-us/news/p26-23")],
            }
        ]
    }
    html_payload = (
        '<html><script id="__NEXT_DATA__" type="application/json">'
        f"{json.dumps({'props': {'pageProps': {'page': pcs_payload}}})}"
        "</script></html>"
    )

    # 1. Happy path: hits PCS only
    calls: list[tuple[str, Any]] = []

    async def pcs_ok(session, method, url, **kwargs):
        calls.append((url, kwargs.get("validator")))
        return pcs_payload, {"last_modified": "Thu, 19 Nov 2026 19:00:00 GMT"}, 200

    monkeypatch.setattr(sources, "_request", pcs_ok)
    items, val, status = await fetch_riot_news(
        object(),
        "lol",
        validator={"last_modified": "prev"},  # type: ignore[arg-type]
    )
    assert status == 200 and items and len(items) == 1
    assert val == {"last_modified": "Thu, 19 Nov 2026 19:00:00 GMT"}
    assert len(calls) == 1 and "content.publishing.riotgames.com" in calls[0][0]

    # 2. PCS 304 returns immediately without hitting HTML
    calls.clear()

    async def pcs_304(session, method, url, **kwargs):
        calls.append((url, kwargs.get("validator")))
        return None, {"last_modified": "prev"}, 304

    monkeypatch.setattr(sources, "_request", pcs_304)
    items, val, status = await fetch_riot_news(
        object(),
        "lol",
        validator={"last_modified": "prev"},  # type: ignore[arg-type]
    )
    assert status == 304 and items is None and len(calls) == 1

    # 3. PCS 500, FetchError, non-dict, or 0 parsed items -> HTML fallback with validator=None -> {}
    for bad_pcs in (
        ("http500", None, 500),
        ("err", FetchError("pcs down"), 0),
        ("nondict", ["bad"], 200),
        ("empty", {"blades": []}, 200),
    ):
        calls.clear()

        async def pcs_fallback(session, method, url, *, _case=bad_pcs, **kwargs):
            calls.append((url, kwargs.get("validator")))
            if "content.publishing.riotgames.com" in url:
                if isinstance(_case[1], Exception):
                    raise _case[1]
                return _case[1], {"last_modified": "pcs"}, _case[2]
            # Netlify HTML returns a weak ETag that MUST NOT leak into validator!
            return html_payload, {"etag": 'W/"netlify-weak"'}, 200

        monkeypatch.setattr(sources, "_request", pcs_fallback)
        items, val, status = await fetch_riot_news(
            object(),
            "lol",
            validator={"last_modified": "old-pcs"},  # type: ignore[arg-type]
        )
        assert status == 200 and items and len(items) == 1, f"failed case {bad_pcs[0]}"
        assert len(calls) == 2
        assert calls[1][1] is None  # did NOT pass PCS validator to Netlify HTML
        assert val == {}  # did NOT store Netlify weak ETag

    # 4. Both PCS and HTML failing propagates HTML failure cleanly
    async def both_fail(session, method, url, **kwargs):
        if "content.publishing.riotgames.com" in url:
            return None, {}, 503
        raise FetchError("html timeout")

    monkeypatch.setattr(sources, "_request", both_fail)
    with pytest.raises(FetchError, match="html timeout"):
        await fetch_riot_news(object(), "lol")  # type: ignore[arg-type]

    async def both_http_fail(session, method, url, **kwargs):
        return None, {}, 502

    monkeypatch.setattr(sources, "_request", both_http_fail)
    items, val, status = await fetch_riot_news(object(), "lol")  # type: ignore[arg-type]
    assert items is None and val == {} and status == 502


# ---------------------------------------------------------------------------
# Task 5: Steam Partner Events, _clean_steam_body, EFT BSG & ABI CMS
# ---------------------------------------------------------------------------
def _steam_ev(
    gid: str,
    title: str,
    event_type: int,
    *,
    posttime: int = 1795000000,
    start_time: int | None = None,
    body: str = "[p]Details[/p]",
    tags: list[str] | None = None,
    clan_steamid: str = "103582791432902485",  # & 0xFFFFFFFF == 3381077
    capsule: str | None = "24b2ead311223344556677889900aabbccddeeff.png",
) -> dict:
    jd = json.dumps({"localized_capsule_image": [capsule] if capsule else []})
    return {
        "gid": gid,
        "clan_steamid": clan_steamid,
        "event_type": event_type,
        "event_name": title,
        "rtime32_start_time": start_time if start_time is not None else posttime,
        "jsondata": jd,
        "announcement_body": {
            "gid": str(int(gid) + 1),
            "clanid": int(clan_steamid) & 0xFFFFFFFF,
            "headline": title,
            "body": body,
            "posttime": posttime,
            "tags": tags or [],
        },
    }


def test_parse_steam_partner_events_cs2_filters_et12_canonical_url_capsule_and_sort() -> None:
    payload = {
        "success": 1,
        "events": [
            # Future-scheduled start_time but older posttime -> must sort AFTER newer posttime
            _steam_ev(
                "694278269818634500",
                "Counter-Strike 2 Update",
                12,
                posttime=1794000000,
                start_time=1799999999,
            ),
            _steam_ev(
                "694278269818634559",
                "Counter-Strike 2 Update",
                12,
                posttime=1795049879,
                tags=["patchnotes"],
            ),
            # ET=28 accidentally tagged patchnotes must be dropped for CS2!
            _steam_ev(
                "694278269818634400",
                "Call II Arms-ory",
                28,
                posttime=1795000000,
                tags=["patchnotes"],
            ),
        ],
    }
    items = parse_steam_partner_events(payload, "cs2")
    assert [i["uid"] for i in items] == [
        "steam-ev-694278269818634559",
        "steam-ev-694278269818634500",
    ]
    assert items[0]["url"] == "https://store.steampowered.com/news/app/730/view/694278269818634559"
    assert (
        items[0]["image"]
        == "https://clan.fastly.steamstatic.com/images/3381077/24b2ead311223344556677889900aabbccddeeff.png"
    )


def test_parse_steam_partner_events_defensive_malformed_fields() -> None:
    payload = {
        "success": 1,
        "events": [
            # Missing announcement_body, bad jsondata, missing clan_steamid -> must not crash
            {
                "gid": "111",
                "event_type": 12,
                "event_name": "Counter-Strike 2 Update",
                "rtime32_start_time": 1795000000,
                "jsondata": "not-json",
                "announcement_body": None,
            },
            # jsondata="null" and non-int clan_steamid with valid ab.clanid fallback
            {
                "gid": "222",
                "clan_steamid": "bad",
                "event_type": 12,
                "event_name": "Counter-Strike 2 Update",
                "jsondata": json.dumps({"localized_capsule_image": ["abc.jpg"]}),
                "announcement_body": {
                    "clanid": 3381077,
                    "headline": "Counter-Strike 2 Update",
                    "body": "Notes",
                    "posttime": 1795000100,
                },
            },
            # Valid capsule filename but clan_accountid == 0 -> image must be None
            {
                "gid": "333",
                "clan_steamid": "0",
                "event_type": 12,
                "event_name": "Counter-Strike 2 Update",
                "jsondata": json.dumps({"localized_capsule_image": ["abc.jpg"]}),
                "announcement_body": {
                    "headline": "Counter-Strike 2 Update",
                    "posttime": 1795000050,
                },
            },
        ],
    }
    items = parse_steam_partner_events(payload, "cs2")
    assert [i["uid"] for i in items] == ["steam-ev-222", "steam-ev-333", "steam-ev-111"]
    assert items[0]["image"] == "https://clan.fastly.steamstatic.com/images/3381077/abc.jpg"
    assert items[1]["image"] is None
    assert items[2]["image"] is None

    with pytest.raises(FetchError):
        parse_steam_partner_events({"success": 0, "events": []}, "cs2")
    with pytest.raises(FetchError):
        parse_steam_partner_events({"success": 1, "events": "bad"}, "cs2")  # type: ignore[arg-type]


def test_parse_steam_partner_events_arc_major_et14_and_body_aware_store_updates() -> None:
    payload = {
        "success": 1,
        "events": [
            _steam_ev("1", "FROZEN TRAIL - 2.0 Update", 14, posttime=1795000500),
            _steam_ev("2", "Hotfix 1.36.1", 28, posttime=1795000400),
            _steam_ev(
                "3",
                "Store Update 2.2.0",
                12,
                posttime=1795000300,
                body="[h2]Content and Bug Fixes[/h2]" + ("x" * 4000),
            ),
            _steam_ev("4", "Store Update 2.5.0", 12, posttime=1795000200, body="Cosmetics only."),
            _steam_ev("5", "Store Update 1.43.0", 12, posttime=1795000150, body="s" * 2687),
            _steam_ev("6", "An Update on Anti-Cheat", 13, posttime=1795000100),
            _steam_ev("7", "Matchmaking Survey", 28, posttime=1795000050),
        ],
    }
    items = parse_steam_partner_events(payload, "arc")
    assert [i["uid"] for i in items] == ["steam-ev-1", "steam-ev-2", "steam-ev-3"]


def test_parse_steam_partner_events_robrun_strips_title_and_filters_updates() -> None:
    payload = {
        "success": 1,
        "events": [
            _steam_ev("717921532895037154", " Patch Notes", 13, posttime=1794914340),
            _steam_ev(
                "712291399099220569",
                "ROB RUN: First Changes & Server Schedule",
                28,
                posttime=1793797440,
            ),
            _steam_ev(
                "717921532895036113",
                "ROB RUN: Answering Your Most Frequently Asked Questions",
                28,
                posttime=1794575820,
            ),
            _steam_ev(
                "712290761518876438",
                "ROB RUN Playtest Video Challenge!",
                26,
                posttime=1793350800,
            ),
        ],
    }
    items = parse_steam_partner_events(payload, "robrun")
    assert [i["uid"] for i in items] == [
        "steam-ev-717921532895037154",
        "steam-ev-712291399099220569",
    ]
    assert items[0]["title"] == "Patch Notes"


def test_parse_steam_news_arc_inverted_tag_and_exclude() -> None:
    payload = {
        "appnews": {
            "newsitems": [
                {
                    "gid": "1",
                    "title": "Store Update - Weekly Cosmetics",
                    "tags": ["patchnotes"],
                    "url": "https://example.com/1",
                    "contents": "Cosmetics",
                    "date": 1700000000,
                },
                {
                    "gid": "2",
                    "title": "Live Update 2.6.0",
                    "tags": [],
                    "url": "https://example.com/2",
                    "contents": "[h2]Fixes[/h2][list][*]Fixed bug[/list]",
                    "date": 1700000100,
                },
            ]
        }
    }
    items = parse_steam_news(payload, "arc")
    assert [i["uid"] for i in items] == ["steam-2"]
    assert items[0]["raw_bbcode"] == "[h2]Fixes[/h2][list][*]Fixed bug[/list]"


def test_clean_steam_body_strips_img_url_preserves_word_boundaries_brackets_and_redos() -> None:
    raw = (
        "[img]{STEAM_CLAN_IMAGE}/123/a.png[/img]"
        "[p]Cache[/p]"
        "[p]Fixed [UI], [PC], [Patch 1.3.0.0] and \\[UI\\] bug[/p]"
        "[list][*]First fix[*]Second fix[/list]"
    )
    cleaned = _clean_steam_body(raw)
    assert "STEAM_CLAN_IMAGE" not in cleaned
    assert "Cache\nFixed [UI], [PC], [Patch 1.3.0.0] and [UI] bug" in cleaned
    assert "First fix\nSecond fix" in cleaned

    # Hostile repeated unclosed [img] tags must finish in O(n) linear time
    t0 = time.monotonic()
    _clean_steam_body("[img]" * 30000)
    assert time.monotonic() - t0 < 0.5


def test_parse_eft_news_sorts_by_utc_timestamp_ignoring_pinned_and_resolves_urls() -> None:
    payload = {
        "list": [
            {
                "id": 400,
                "pinned": 1,
                "date": "2026-08-01T10:00:00+03:00",  # 07:00 UTC (older but pinned!)
                "name": "Patch 1.1.0.0",
                "link": None,  # fallback to /news/id/400
                "ico": "https://cdn.example.com/abs.jpg",
                "smallDescr": "",
                "descr": "<h3>Changes</h3><p>Big wipe.</p>",
            },
            {
                "id": 416,
                "pinned": 0,
                "date": "2026-10-29T10:00:00+03:00",  # 07:00 UTC
                "name": "Patch 1.3.0.0",
                "link": "/news/id/416",
                "ico": "/uploads/content/news/patch_note_en.jpg?en",
                "smallDescr": "Patch 1.3.0.0 notes.",
                "descr": "<h3>Changes</h3><ul><li>Halloween event</li></ul>",
            },
            {
                "id": 417,
                "pinned": 0,
                "date": "2026-10-29T08:00:00Z",  # 08:00 UTC (1h newer than 416)
                "name": "Patch 1.3.0.1",
                "link": "javascript:alert(1)",  # bad scheme -> fallback to canonical
                "ico": None,
                "smallDescr": "Hotfix.",
                "descr": "<p>Hotfix details.</p>",
            },
            {"name": "Missing ID"},
        ]
    }
    items = parse_eft_news(payload)
    assert [i["uid"] for i in items] == ["eft-bsg-417", "eft-bsg-416", "eft-bsg-400"]
    assert items[0]["url"] == "https://www.escapefromtarkov.com/news/id/417"
    assert items[0]["image"] is None
    assert items[1]["url"] == "https://www.escapefromtarkov.com/news/id/416"
    assert (
        items[1]["image"]
        == "https://www.escapefromtarkov.com/uploads/content/news/patch_note_en.jpg?en"
    )
    assert items[1]["raw_html"] == "<h3>Changes</h3><ul><li>Halloween event</li></ul>"
    assert items[2]["url"] == "https://www.escapefromtarkov.com/news/id/400"
    assert items[2]["image"] == "https://cdn.example.com/abs.jpg"
    assert "Big wipe." in items[2]["body"]

    with pytest.raises(FetchError):
        parse_eft_news({"list": "not-a-list"})  # type: ignore[arg-type]


def test_parse_abi_news_filters_sorts_by_pub_timestamp_and_guards_malformed() -> None:
    payload = {
        "code": 0,
        "data": {
            "result": 0,
            "info_content": [
                {
                    "content_id": "aaa111",
                    "title": "Oct. 27 (UTC+0) Live Update",
                    "pub_timestamp": "1793008800",
                    "content_part": "Older update preview",
                    "pic_urls": [
                        "ftp://bad.example/img.jpg",
                        "https://sg-cag.playerinfinite.com/a.jpg",
                    ],
                },
                {
                    "content_id": "bbb222",
                    "title": "Nov. 10 (UTC+0) Live Update",
                    "pub_timestamp": "1794218400",
                    "content_part": "Newer update preview",
                    "pic_urls": ["https://sg-cag.playerinfinite.com/b.jpg"],
                },
                {
                    "content_id": "ccc333",
                    "title": "Halloween Sign-In Spooky Skins, Bonds + RTX 5060 Giveaway",
                    "pub_timestamp": "1794300000",
                    "content_part": "Giveaway",
                    "pic_urls": [],
                },
                {
                    "content_id": "ddd444",
                    "title": "Season 6 Security Report",
                    "pub_timestamp": "1794100000",
                    "content_part": "Bans",
                    "pic_urls": None,
                },
                {
                    "content_id": "eee555",
                    "title": "June 13 Test Server Update Announcement",
                    "pub_timestamp": "1794000000",
                    "content_part": "Test",
                    "pic_urls": [],
                },
            ],
        },
    }
    items = parse_abi_news(payload)
    assert [i["uid"] for i in items] == ["abi-cms-bbb222", "abi-cms-aaa111"]
    assert items[0]["content_id"] == "bbb222"
    assert items[0]["timestamp"] == 1794218400
    assert items[0]["url"] == "https://www.arenabreakoutinfinite.com/en/news.html?content_id=bbb222"
    assert items[0]["body"] == "Newer update preview"
    assert items[0]["image"] == "https://sg-cag.playerinfinite.com/b.jpg"
    assert items[1]["image"] == "https://sg-cag.playerinfinite.com/a.jpg"

    with pytest.raises(FetchError):
        parse_abi_news({"code": 1, "data": {}})
    with pytest.raises(FetchError):
        parse_abi_news({"code": 0, "data": {"result": 1, "errmsg": "codec error"}})
    with pytest.raises(FetchError):
        parse_abi_news({"code": 0, "data": {"result": 0}})


async def test_fetch_abi_news_list_and_detail_payloads_and_error_degradation(monkeypatch) -> None:
    recorded: list[dict[str, Any]] = []

    async def fake_request(session, method, url, **kwargs):
        recorded.append({"method": method, "url": url, **kwargs})
        if url.endswith("GetContentByLabel"):
            return (
                {
                    "code": 0,
                    "data": {
                        "result": 0,
                        "info_content": [
                            {
                                "content_id": "xyz",
                                "title": "Nov. 10 (UTC+0) Live Update",
                                "pub_timestamp": "1794218400",
                                "content_part": "Preview",
                                "pic_urls": [],
                            }
                        ],
                    },
                },
                {},
                200,
            )
        return {"code": 0, "data": {"result": 0, "content": "<p>Full HTML</p>"}}, {}, 200

    monkeypatch.setattr(sources, "_request", fake_request)
    items, _, status = await fetch_abi_news(object())  # type: ignore[arg-type]
    assert status == 200 and items and len(items) == 1
    assert recorded[0]["method"] == "POST"
    assert recorded[0]["headers"] == ABI_CMS_HEADERS
    body = recorded[0]["json_body"]
    assert body["language"] == ["en"]
    assert body["gameid"] == ABI_CMS_GAME_ID
    assert body["primary_label_id"] == ABI_CMS_PRIMARY_LABEL
    assert body["get_num"] == 20

    html = await fetch_abi_news_detail(object(), "xyz")  # type: ignore[arg-type]
    assert html == "<p>Full HTML</p>"
    assert recorded[1]["json_body"] == {"content_id": "xyz"}
    assert recorded[1]["headers"] == ABI_CMS_HEADERS

    # Detail degradation on error / non-zero code / non-zero result / empty content
    for bad in (
        FetchError("timeout"),
        ({"code": 1, "data": {}}, {}, 200),
        ({"code": 0, "data": {"result": 1}}, {}, 200),
        ({"code": 0, "data": {"result": 0, "content": ""}}, {}, 200),
        (None, {}, 503),
    ):

        async def fail_detail(session, method, url, *, _bad=bad, **kwargs):
            if isinstance(_bad, Exception):
                raise _bad
            return _bad

        monkeypatch.setattr(sources, "_request", fail_detail)
        assert await fetch_abi_news_detail(object(), "xyz") is None  # type: ignore[arg-type]
