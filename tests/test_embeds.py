"""Unit tests for embed formatting, BBCode conversion, and Discord limits."""

from __future__ import annotations

import time

from gamewatch.embeds import (
    MAX_AUTHOR,
    MAX_DESC,
    MAX_FIELD_NAME,
    MAX_FIELD_VALUE,
    MAX_FIELDS,
    MAX_FOOTER,
    MAX_TITLE,
    MAX_TOTAL,
    _trunc,
    accent_int,
    embed_to_plain_text,
    incident_embed,
    news_embed,
    recovery_embed,
    status_panel_embed,
    steam_bbcode_to_markdown,
)


def _assert_discord_limits(embed) -> None:
    if embed.title:
        assert len(embed.title) <= MAX_TITLE
    if embed.description:
        assert len(embed.description) <= MAX_DESC
    assert len(embed.fields) <= MAX_FIELDS
    for field in embed.fields:
        assert len(field.name or "") <= MAX_FIELD_NAME
        assert len(field.value or "") <= MAX_FIELD_VALUE
    if embed.footer and embed.footer.text:
        assert len(embed.footer.text) <= MAX_FOOTER
    if embed.author and embed.author.name:
        assert len(embed.author.name) <= MAX_AUTHOR
    assert len(embed) <= MAX_TOTAL
    embed.to_dict()


def test_steam_bbcode_to_markdown_extracts_image_and_formats() -> None:
    raw = (
        '[img src="{STEAM_CLAN_IMAGE}/1234/hero.png"]'
        "[p]\\[ MAPS ][/p]"
        "[list][*]Updated Mirage[list][*]Fixed boost spot[/list][/list]"
        "[p]Read [url=https://example.com]more[/url] with [b]bold[/b].[/p]"
    )
    body, image = steam_bbcode_to_markdown(raw)
    assert image == "https://clan.akamai.steamstatic.com/images/1234/hero.png"
    assert "## MAPS" in body
    assert "- Updated Mirage" in body
    assert "  - Fixed boost spot" in body
    assert "[more](https://example.com)" in body
    assert "**bold**" in body


def test_steam_bbcode_redos_guard() -> None:
    start = time.monotonic()
    steam_bbcode_to_markdown("[b]" * 60000)
    elapsed = time.monotonic() - start
    assert elapsed < 2.0


def test_trunc_closes_unbalanced_markdown() -> None:
    cut = _trunc("start **bold text that will be cut " + "x" * 100, 40)
    assert cut.count("**") % 2 == 0
    assert cut.endswith("\u2026")


def test_accent_int_parses_hex() -> None:
    assert accent_int("#c89b3c") == 0xC89B3C
    assert accent_int("ff4655") == 0xFF4655
    assert accent_int("invalid") is None
    assert accent_int(None) is None


def test_synthetic_overflow_stays_within_discord_limits() -> None:
    huge = {
        "title": "X" * 400,
        "body": "long. " * 2000,
        "severity": "critical",
        "platforms": ["windows"] * 40,
    }
    parsed = {
        "region_name": "R" * 600,
        "ok": False,
        "items": [huge],
        "services": [{"name": f"svc{i}", "status": 3, "label": "Down"} for i in range(30)],
    }
    _assert_discord_limits(incident_embed("lol", "eun1", parsed, huge))
    _assert_discord_limits(status_panel_embed("lol", "eun1", parsed))
    _assert_discord_limits(recovery_embed("lol", "eun1", parsed, None))
    _assert_discord_limits(news_embed("cs2", {"title": "Y" * 500, "raw_bbcode": "[p]a[/p]" * 3000}))


def test_embed_to_plain_text_preserves_key_fields() -> None:
    embed = news_embed(
        "lol",
        {
            "title": "League of Legends Patch 26.23 Notes",
            "url": "https://www.leagueoflegends.com/en-gb/news/game-updates/patch-26-23/",
            "body": "Balance updates.",
            "published_at": "2026-11-17T19:00:00Z",
        },
    )
    text = embed_to_plain_text(embed)
    assert "League of Legends" in text
    assert "Patch 26.23 Notes" in text
    assert "Balance updates." in text
    assert "`26.23`" in text
