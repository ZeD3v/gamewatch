"""Pure unit tests for upstream parsers and location filtering."""

from __future__ import annotations

import json

import pytest

from gamewatch.sources import (
    FetchError,
    extract_location,
    filter_status_view,
    location_allowed,
    parse_eft_status,
    parse_riot_news,
    parse_riot_status,
    parse_steam_news,
)


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
                    {"translations": [{"locale": "en_US", "content": "Initial note"}]},
                    {"translations": [{"locale": "en_US", "content": "Investigating now."}]},
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
    assert item["uid"] == "riot-incidents-42-2026-11-19T10:05:00+00:00"
    assert item["title"] == "Login issues"
    assert item["body"] == "Investigating now."
    assert item["severity"] == "warning"


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


def test_parse_riot_news_filters_offsite_and_crops_sanity_image() -> None:
    next_data = {
        "props": {
            "pageProps": {
                "items": [
                    {
                        "title": "League of Legends Patch 26.23 Notes",
                        "publishedAt": "2026-11-17T19:00:00.000Z",
                        "action": {"payload": {"url": "/en-gb/news/game-updates/patch-26-23/"}},
                        "description": {"body": "<p>Balance updates.</p>"},
                        "media": {
                            "type": "image",
                            "url": "https://cmsassets.rgpub.io/sanity/images/hero.jpg",
                            "colors": {"primary": "#c89b3c"},
                        },
                    },
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
    assert items[0]["url"] == "https://www.leagueoflegends.com/en-gb/news/game-updates/patch-26-23/"
    assert "w=1200&h=675&fit=crop&auto=format" in (items[0]["image"] or "")
    assert items[0]["accent"] == "#c89b3c"
    assert items[0]["body"] == "Balance updates."


def test_parse_riot_news_raises_when_next_data_missing() -> None:
    with pytest.raises(FetchError):
        parse_riot_news("<html><body>No hydration blob</body></html>", "lol")


def test_location_extraction_and_filtering() -> None:
    dubai_body = (
        "The following game server location is currently unavailable "
        "while we investigate an issue: Dubai"
    )
    assert extract_location({"body": dubai_body, "title": ""}) == "Dubai"
    assert extract_location({"body": dubai_body + "\nMore info soon.", "title": ""}) == "Dubai"
    assert extract_location({"body": "Login issues for all players.", "title": "Login"}) is None

    dubai_item = {"uid": "loc-Dubai", "body": dubai_body, "title": "Game Server Unavailable"}
    warsaw_item = {
        "uid": "loc-Warsaw",
        "body": dubai_body.replace("Dubai", "Warsaw"),
        "title": "Game Server Unavailable",
    }
    unlocated = {"uid": "loc-none", "body": "Players cannot log in.", "title": "Outage"}

    assert location_allowed(dubai_item, []) is True
    assert location_allowed(dubai_item, ["warsaw"]) is False
    assert location_allowed(warsaw_item, ["warsaw"]) is True
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

    all_filtered = filter_status_view(
        {"region_id": "EU", "region_name": "Europe", "items": [dubai_item], "ok": False},
        ["warsaw"],
    )
    assert all_filtered["items"] == []
    assert all_filtered["ok"] is True
