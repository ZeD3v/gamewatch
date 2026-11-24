"""Unit tests for embed formatting, BBCode & HTML conversion, and Discord limits."""

from __future__ import annotations

import time

import discord

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
    fit_embed,
    html_to_markdown,
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


def test_steam_bbcode_fixes_heading_newline_bold_whitespace_multiline_tables_and_aligned_p() -> (
    None
):
    raw = (
        "[h3]\nExpansion Hub[/h3]"
        '[p align="center"]Window[b] opens on [/b]Sep 8[/p]'
        "[table]\n"
        '[tr]\n[th colspan="1"]Pos[/th]\n[th]Share[/th]\n[/tr]\n'
        "[tr]\n[td]1[/td]\n[td]2.85%[/td]\n[/tr]\n"
        "[/table]"
    )
    body, _ = steam_bbcode_to_markdown(raw)
    assert "### Expansion Hub" in body
    assert "###\nExpansion Hub" not in body
    assert "Window **opens on** Sep 8" in body
    assert "** opens on **" not in body
    assert "Pos | Share" in body
    assert "1 | 2.85%" in body
    assert not any(line.rstrip().endswith("|") for line in body.splitlines())


def test_steam_bbcode_preserves_quoted_urls_link_labels_prose_brackets_and_multi_bold() -> None:
    raw = (
        '[p]Visit [url="https://discord.com/x"]the dedicated PvE feedback channel[/url], '
        '[url="https://arcraiders.com/y"]PVE Test Thanks[/url], '
        '[url="https://x.com/z"]In case you missed it[/url], '
        '[url="https://id.embark.games/id/sign-in"][u]here[/u][/url], '
        '[url="javascript:alert(1)"]bad link[/url].[/p]'
        "[p]Fixed [UI] scaling, [PC] crash, [Patch 1.3.0.0], [Bugfix], [Info], [listen], "
        "\\[UI\\], and \\[list\\].[/p]"
        '[p]Orphan [/img] and [img src="https://example.com/a.png"][b]cap[/b][/img].[/p]'
        "[h3][b]Part 1[/b] and [b]Part 2[/b][/h3]"
    )
    body, _ = steam_bbcode_to_markdown(raw)
    assert "[the dedicated PvE feedback channel](https://discord.com/x)" in body
    assert "[PVE Test Thanks](https://arcraiders.com/y)" in body
    assert "[In case you missed it](https://x.com/z)" in body
    assert "[__here__](https://id.embark.games/id/sign-in)" in body
    assert '("' not in body
    assert "javascript:" not in body and "bad link" in body
    assert (
        "Fixed [UI] scaling, [PC] crash, [Patch 1.3.0.0], [Bugfix], [Info], [listen], "
        "[UI], and [list]." in body
    )
    assert "[/img]" not in body
    assert "**cap**" in body
    assert "### **Part 1** and **Part 2**" in body

    # Standalone / leading [listen] / [/list_end] / [olist_foo] segments must never be dropped
    assert steam_bbcode_to_markdown("[listen]")[0] == "[listen]"
    assert steam_bbcode_to_markdown("[listen] to the sound")[0] == "[listen] to the sound"
    assert (
        steam_bbcode_to_markdown("[list][*][listen] carefully[/list]")[0] == "- [listen] carefully"
    )
    assert steam_bbcode_to_markdown("[/list_end] text")[0] == "[/list_end] text"
    assert steam_bbcode_to_markdown("[olist_foo] text")[0] == "[olist_foo] text"


def test_steam_bbcode_redos_guard() -> None:
    for payload in (
        "[b]" * 60000,
        "[list]" * 1333 + "[*]" * 2666,
        " " * 15999 + "a",
    ):
        start = time.monotonic()
        steam_bbcode_to_markdown(payload)
        elapsed = time.monotonic() - start
        assert elapsed < 0.5


def test_html_to_markdown_converts_bsg_and_abi_html_and_extracts_image() -> None:
    raw = (
        '<head id="cms_head"><style id="cms_style">body{color:red}</style>'
        "<title>Ignored</title></head>"
        "<script>alert(1)</script>"
        "<body>"
        "<h3><strong>Changes</strong></h3>"
        "<ul><li><p>Item 1 &amp; fixes</p><ul><li>Sub&nbsp;item</li></ul></li></ul>"
        "<p>Check <strong> bold </strong><strong> </strong>and "
        '<a href="https://example.com">link</a>'
        '<a href="https://example.com"><img src="https://example.com/a.jpg"></a> and '
        '<a href="javascript:alert(1)">bad</a></p>'
        "<table><tr><th>Col A</th><th>Col B</th></tr><tr><td>10</td><td>20</td></tr></table>"
        "</body>"
    )
    body, image = html_to_markdown(raw)
    assert image == "https://example.com/a.jpg"
    assert "color:red" not in body
    assert "Ignored" not in body
    assert "alert(1)" not in body
    assert "### Changes" in body
    assert "### **Changes**" not in body
    assert "- Item 1 & fixes" in body
    assert "  - Sub item" in body
    assert "Check **bold** and [link](https://example.com) and bad" in body
    assert "Col A | Col B" in body
    assert "10 | 20" in body


def test_html_to_markdown_redos_and_large_input_guard() -> None:
    import tracemalloc

    for payload in (
        "<b><div>" * 25000,
        "<b>" * 20000 + "</i>" * 15000,
        "<a>" * 15000 + "<i>" * 15000,
        "<b>" * 20000 + "x" * 20000,
        "<ul>" * 15000 + "<h1>" + "<li>" * 5000,
    ):
        # Measure wall-clock time without tracemalloc's allocation hooks (which
        # slow CPython down by ~5-6x and cause false failures on shared CI VMs).
        start = time.monotonic()
        out, _ = html_to_markdown(payload, budget=2000)
        elapsed = time.monotonic() - start
        assert elapsed < 0.5
        assert len(out) <= 2000

        tracemalloc.start()
        try:
            html_to_markdown(payload, budget=2000)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert peak < 20 * 1024 * 1024  # well under 20 MiB even for deep list amplification

    # 75 KB legitimate ABI HTML starting with a 4 KB <style> block still renders body
    large_abi = f"<head><style>{'a{}' * 1400}</style></head><body><h2>Season 7</h2>" + (
        "<p>Weapon balance adjustment.</p>" * 2200
    )
    body, _ = html_to_markdown(large_abi, budget=2600)
    assert "## Season 7" in body
    assert "Weapon balance adjustment." in body
    assert len(body) <= 2600


def test_html_to_markdown_multi_bold_heading_preserved() -> None:
    body, _ = html_to_markdown("<h3><strong>Part 1</strong> and <strong>Part 2</strong></h3>")
    assert body == "### **Part 1** and **Part 2**"


def test_news_embed_renders_raw_html_falls_back_when_html_empty_and_preserves_image() -> None:
    embed = news_embed(
        "eft",
        {
            "title": "Patch 1.3.0.0",
            "url": "https://www.escapefromtarkov.com/news/id/416",
            "body": "Fallback summary.",
            "raw_html": (
                "<h3>Changes</h3><ul><li>Halloween event</li></ul>"
                '<img src="https://example.com/inline.jpg">'
            ),
            "published_at": "2026-10-29T10:00:00+03:00",
            "image": "https://www.escapefromtarkov.com/uploads/content/news/patch_note_en.jpg?en",
        },
    )
    assert "### Changes" in (embed.description or "")
    assert "- Halloween event" in (embed.description or "")
    assert embed.image.url == (
        "https://www.escapefromtarkov.com/uploads/content/news/patch_note_en.jpg?en"
    )
    _assert_discord_limits(embed)

    # Image-only raw_html falls back to plain body preview instead of blank description
    img_only = news_embed(
        "abi",
        {
            "title": "Nov. 10 (UTC+0) Live Update",
            "url": "https://www.arenabreakoutinfinite.com/en/news.html?content_id=abc",
            "body": "Preview fallback text.",
            "raw_html": '<p><img src="https://example.com/only.jpg"></p>',
            "timestamp": 1794218400,
        },
    )
    assert "Preview fallback text." in (img_only.description or "")
    assert img_only.image.url == "https://example.com/only.jpg"
    _assert_discord_limits(img_only)


def test_status_embeds_render_maintenance_labels_scheduled_ok_and_eft_datacenters() -> None:
    sched_item = {
        "uid": "riot-maint-1",
        "kind": "maintenances",
        "severity": "info",
        "maintenance_status": "scheduled",
        "title": "Planned maintenance",
        "body": "Scheduled tonight at 03:00 UTC.",
        "platforms": ["windows"],
    }
    inprog_item = {
        "uid": "riot-maint-2",
        "kind": "maintenances",
        "severity": "warning",
        "maintenance_status": "in_progress",
        "title": "Server maintenance",
        "body": "Servers are currently offline for patching.",
        "platforms": ["windows"],
    }
    active_inc = {
        "uid": "riot-inc-3",
        "kind": "incidents",
        "severity": "critical",
        "maintenance_status": None,
        "title": "Login outage",
        "body": "Cannot log in.",
        "platforms": [],
    }
    parsed_sched = {
        "region_id": "EUN1",
        "region_name": "EU Nordic & East",
        "ok": True,
        "items": [sched_item],
    }

    sched_emb = incident_embed("lol", "eun1", parsed_sched, sched_item)
    sev_field = next(f for f in sched_emb.fields if f.name == "Severity")
    assert "Scheduled Maintenance" in (sev_field.value or "")
    _assert_discord_limits(sched_emb)

    inprog_emb = incident_embed("lol", "eun1", {**parsed_sched, "ok": False}, inprog_item)
    sev_field2 = next(f for f in inprog_emb.fields if f.name == "Severity")
    assert "Maintenance in Progress" in (sev_field2.value or "")
    _assert_discord_limits(inprog_emb)

    # Scheduled-only panel stays "All systems operational" AND surfaces the scheduled notice
    panel_ok = status_panel_embed("lol", "eun1", parsed_sched)
    assert "All systems operational" in (panel_ok.description or "")
    assert any("Planned maintenance" in (f.name or "") for f in panel_ok.fields)
    _assert_discord_limits(panel_ok)

    # Mixed active + scheduled panel counts ONLY active incidents in headline
    panel_mixed = status_panel_embed(
        "lol",
        "eun1",
        {
            "region_id": "EUN1",
            "region_name": "EU Nordic & East",
            "ok": False,
            "items": [active_inc, sched_item],
        },
    )
    assert "1 active incident(s)" in (panel_mixed.description or "")
    _assert_discord_limits(panel_mixed)

    # EFT panel & incident render datacenters cleanly
    eft_parsed = {
        "region_id": "global",
        "region_name": "Global",
        "ok": False,
        "services": [{"name": "Auth", "status": 0, "label": "OK"}],
        "datacenters": [
            {"name": "Server EUR", "region": "Europe", "status": 2, "label": "Unstable"},
            {"name": "Server AF", "region": "Africa", "status": 0, "label": "OK"},
        ],
        "items": [
            {
                "uid": "eft-degraded-dc:Server EUR:2",
                "severity": "warning",
                "title": "Unstable - Europe",
                "body": "Europe: Unstable",
            }
        ],
    }
    eft_panel = status_panel_embed("eft", "global", eft_parsed)
    assert any("Datacenters" in (f.name or "") for f in eft_panel.fields)
    eft_inc = incident_embed("eft", "global", eft_parsed, eft_parsed["items"][0])
    assert any("Affected datacenters" in (f.name or "") for f in eft_inc.fields)
    _assert_discord_limits(eft_panel)
    _assert_discord_limits(eft_inc)


def test_trunc_closes_unbalanced_markdown_and_never_exceeds_limit() -> None:
    cut = _trunc("start **bold text that will be cut " + "x" * 100, 40)
    assert cut.count("**") % 2 == 0
    assert cut.endswith("\u2026")
    assert len(cut) <= 40

    for text in (
        "**hello " + "x" * 246 + " world",
        "**" + "a" * 300,
        "||~~**__`" + "a" * 300,
    ):
        trimmed = _trunc(text, MAX_TITLE)
        assert len(trimmed) <= MAX_TITLE
        for tok in ("||", "~~", "**", "__"):
            assert trimmed.count(tok) % 2 == 0
        assert trimmed.count("`") % 2 == 0


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
        "datacenters": [
            {"name": f"Server {i}", "region": f"Region {i}", "status": 3, "label": "Down"}
            for i in range(30)
        ],
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
            "url": "https://www.leagueoflegends.com/en-us/news/game-updates/patch-26-23/",
            "body": "Balance updates.",
            "published_at": "2026-11-17T19:00:00Z",
        },
    )
    text = embed_to_plain_text(embed)
    assert "League of Legends" in text
    assert "Patch 26.23 Notes" in text
    assert "Balance updates." in text
    assert "`26.23`" in text


def test_riot_status_url_reflects_configured_region_and_fit_embed_caps_desc() -> None:
    parsed = {
        "region_id": "NA1",
        "region_name": "North America",
        "ok": False,
        "items": [
            {
                "uid": "riot-1",
                "severity": "warning",
                "title": "Login issues",
                "body": "Investigating.",
            }
        ],
    }
    panel = status_panel_embed("lol", "na1", parsed)
    assert panel.url == "https://status.riotgames.com/lol?region=na1"

    inc = incident_embed(
        "valorant", "ap", {**parsed, "region_name": "Asia Pacific"}, parsed["items"][0]
    )
    assert any(
        "https://status.riotgames.com/valorant?region=ap" in (f.value or "") for f in inc.fields
    )

    rec = recovery_embed("tft", "euw1", {**parsed, "region_name": "EU West"}, None)
    assert rec.url == "https://status.riotgames.com/teamfighttactics?region=euw1"

    eft_panel = status_panel_embed("eft", "global", {**parsed, "region_name": "Global"})
    assert eft_panel.url == "https://status.escapefromtarkov.com/"

    # fit_embed enforces MAX_DESC even when aggregate embed length < MAX_TOTAL (6000)
    long_desc = discord.Embed(title="Settings", description="a " * 2500)
    assert MAX_DESC < len(long_desc.description or "") < MAX_TOTAL
    fitted = fit_embed(long_desc)
    _assert_discord_limits(fitted)

    # Leading space after Steam [*] bullet collapses cleanly
    bb, _ = steam_bbcode_to_markdown("[list][*]  First bullet[*]   Second bullet[/list]")
    assert "- First bullet" in bb
    assert "- Second bullet" in bb
    assert "-  " not in bb
