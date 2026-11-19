"""Render test: builds real embeds from live upstream data and asserts that
every Discord limit is respected.

    uv run python scripts/rendertest.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp

from gamewatch.constants import GAMES, USER_AGENT
from gamewatch.embeds import (
    MAX_DESC,
    MAX_FIELD_NAME,
    MAX_FIELD_VALUE,
    MAX_FIELDS,
    MAX_FOOTER,
    MAX_TITLE,
    MAX_TOTAL,
    _trunc,
    accent_int,
    html_to_markdown,
    incident_embed,
    news_embed,
    recovery_embed,
    status_panel_embed,
    steam_bbcode_to_markdown,
)
from gamewatch.sources import (
    fetch_abi_news_detail,
    fetch_game_news,
    fetch_game_status,
)

REGIONS = {"lol": "eun1", "tft": "eun1", "valorant": "eu"}
failures = []


def check(label, embed):
    """Assert every documented Discord embed limit."""
    problems = []
    if embed.title and len(embed.title) > MAX_TITLE:
        problems.append(f"title {len(embed.title)}>{MAX_TITLE}")
    if embed.description and len(embed.description) > MAX_DESC:
        problems.append(f"desc {len(embed.description)}>{MAX_DESC}")
    if len(embed.fields) > MAX_FIELDS:
        problems.append(f"fields {len(embed.fields)}>{MAX_FIELDS}")
    for f in embed.fields:
        if len(f.name or "") > MAX_FIELD_NAME:
            problems.append(f"field name {len(f.name)}")
        if len(f.value or "") > MAX_FIELD_VALUE:
            problems.append(f"field value {len(f.value)}")
    if embed.footer and embed.footer.text and len(embed.footer.text) > MAX_FOOTER:
        problems.append("footer too long")
    if embed.author and embed.author.name and len(embed.author.name) > 256:
        problems.append(f"author {len(embed.author.name)}>256")
    if len(embed) > MAX_TOTAL:
        problems.append(f"TOTAL {len(embed)}>{MAX_TOTAL}")
    # Serialising is what discord.py does before POSTing; catches bad types.
    try:
        embed.to_dict()
    except Exception as exc:
        problems.append(f"to_dict failed: {exc}")

    status = "FAIL " + ",".join(problems) if problems else "ok"
    if problems:
        failures.append((label, problems))
    print(f"  {label:34} len={len(embed):4} fields={len(embed.fields)} {status}")
    return embed


async def main():
    timeout = aiohttp.ClientTimeout(total=40)
    async with aiohttp.ClientSession(
        timeout=timeout, headers={"User-Agent": USER_AGENT}
    ) as session:
        print("=" * 70)
        print("NEWS EMBEDS")
        print("=" * 70)
        for game, cfg in GAMES.items():
            items, _, http = await fetch_game_news(session, game)
            if not items:
                print(f"  {game}: NO ITEMS (http {http})")
                failures.append((game, ["no items"]))
                continue
            sample_embed = None
            for idx, item in enumerate(items[:3]):
                to_render = item
                if (
                    idx == 0
                    and cfg["news_kind"] == "abi_cms"
                    and item.get("content_id")
                    and not item.get("raw_html")
                ):
                    detail_html = await fetch_abi_news_detail(session, item["content_id"])
                    if not detail_html:
                        failures.append((game, ["abi detail empty"]))
                    else:
                        to_render = {**item, "raw_html": detail_html}
                acc = accent_int(to_render.get("accent"))
                sample_embed = check(
                    f"{game}/{to_render['title'][:22]}", news_embed(game, to_render, acc)
                )
            if sample_embed is not None:
                print(f"    -> sample desc head: {(sample_embed.description or '')[:120]!r}")
                img_url = (sample_embed.image.url or "")[:90] if sample_embed.image else None
                print(f"    -> image: {img_url}")

        print()
        print("=" * 70)
        print("STATUS EMBEDS")
        print("=" * 70)
        for game, cfg in GAMES.items():
            if not cfg["status_kind"]:
                continue
            region = REGIONS.get(game, cfg["default_region"])
            parsed, _, http = await fetch_game_status(session, game, region)
            if not parsed:
                failures.append((game, [f"status http {http}"]))
                continue
            check(f"{game}/panel", status_panel_embed(game, region, parsed))
            check(f"{game}/recovery", recovery_embed(game, region, parsed, None))
            for item in (parsed.get("items") or [])[:2]:
                check(f"{game}/incident", incident_embed(game, region, parsed, item))

        # Synthetic worst case: oversized incidents, services, datacenters, BBCode & HTML.
        print()
        print("=" * 70)
        print("SYNTHETIC OVERFLOW & REDOS")
        print("=" * 70)
        huge = {
            "title": "X" * 400,
            "body": "long. " * 2000,
            "severity": "critical",
            "platforms": ["windows"] * 40,
        }
        parsed = {
            "region_name": "EU Nordic & East",
            "ok": False,
            "items": [huge],
            "services": [{"name": f"svc{i}", "status": 3, "label": "Down"} for i in range(30)],
            "datacenters": [
                {"name": f"Server {i}", "region": f"Region {i}", "status": 3, "label": "Down"}
                for i in range(30)
            ],
        }
        check("overflow/incident", incident_embed("lol", "eun1", parsed, huge))
        check("overflow/panel", status_panel_embed("lol", "eun1", parsed))
        check(
            "overflow/news-bbcode",
            news_embed("cs2", {"title": "Y" * 500, "raw_bbcode": "[p]a[/p]" * 3000}),
        )
        check(
            "overflow/news-html",
            news_embed("eft", {"title": "Z" * 500, "raw_html": "<p>entry</p>" * 3000}),
        )

        evil_region = {"region_name": "R" * 600, "ok": False, "items": [huge], "services": []}
        check("overflow/region-author", incident_embed("lol", "eun1", evil_region, huge))
        check("overflow/region-title", status_panel_embed("lol", "eun1", evil_region))
        check("overflow/region-recovery", recovery_embed("lol", "eun1", evil_region, None))

        for label, hostile_bb in (
            ("unclosed-bold", "[b]" * 60000),
            ("deep-list", "[list]" * 1333 + "[*]" * 2666),
        ):
            start = time.monotonic()
            steam_bbcode_to_markdown(hostile_bb)
            elapsed_bb = time.monotonic() - start
            if elapsed_bb > 0.5:
                failures.append((f"redos-bbcode-{label}", [f"conversion took {elapsed_bb:.2f}s"]))
            print(f"  redos bbcode ({label}): converted in {elapsed_bb:.3f}s")

        for label, hostile_html in (
            ("nested-tags", "<b><div>" * 25000),
            ("unbalanced-inline", "<b>" * 20000 + "</i>" * 15000),
            ("deep-links", "<a>" * 15000 + "<i>" * 15000),
            ("deep-list-heading", "<ul>" * 15000 + "<h1>" + "<li>" * 5000),
        ):
            start = time.monotonic()
            html_to_markdown(hostile_html)
            elapsed_html = time.monotonic() - start
            if elapsed_html > 0.5:
                failures.append((f"redos-html-{label}", [f"conversion took {elapsed_html:.2f}s"]))
            print(f"  redos html ({label}): converted in {elapsed_html:.3f}s")

        cut = _trunc("start **bold text that will be cut " + "x" * 100, 40)
        if cut.count("**") % 2:
            failures.append(("trunc-markdown", [f"unbalanced ** in {cut!r}"]))
        print(f"  trunc balance: {cut!r}")

    print()
    if failures:
        print(f"FAILURES: {len(failures)}")
        for label, problems in failures:
            print(f"  {label}: {problems}")
        return 1
    print("ALL EMBEDS WITHIN DISCORD LIMITS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
