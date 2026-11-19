"""Render test: builds real embeds from live upstream data and asserts that
every Discord limit is respected.

    uv run python scripts/rendertest.py
"""

from __future__ import annotations

import asyncio
import sys
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
    accent_int,
    incident_embed,
    news_embed,
    recovery_embed,
    status_panel_embed,
)
from gamewatch.sources import (
    fetch_eft_status,
    fetch_riot_news,
    fetch_riot_status,
    fetch_steam_news,
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
            if cfg["news_kind"] == "steam":
                items, _, http = await fetch_steam_news(session, game)
            else:
                items, _, http = await fetch_riot_news(session, game)
            if not items:
                print(f"  {game}: NO ITEMS (http {http})")
                failures.append((game, ["no items"]))
                continue
            # Test the newest three, not just one - long CS2 notes are the
            # ones most likely to breach a limit.
            for item in items[:3]:
                acc = accent_int(item.get("accent"))
                e = check(f"{game}/{item['title'][:22]}", news_embed(game, item, acc))
            print(f"    -> sample desc head: {(e.description or '')[:120]!r}")
            print(f"    -> image: {(e.image.url or '')[:90] if e.image else None}")

        print()
        print("=" * 70)
        print("STATUS EMBEDS")
        print("=" * 70)
        for game, cfg in GAMES.items():
            if not cfg["status_kind"]:
                continue
            region = REGIONS.get(game, cfg["default_region"])
            if cfg["status_kind"] == "riot":
                parsed, _, http = await fetch_riot_status(session, game, region)
            else:
                parsed, _, http = await fetch_eft_status(session)
            if not parsed:
                failures.append((game, [f"status http {http}"]))
                continue
            check(f"{game}/panel", status_panel_embed(game, region, parsed))
            check(f"{game}/recovery", recovery_embed(game, region, parsed, None))
            for item in (parsed.get("items") or [])[:2]:
                check(f"{game}/incident", incident_embed(game, region, parsed, item))

        # Synthetic worst case: an oversized incident must be trimmed, not raise.
        print()
        print("=" * 70)
        print("SYNTHETIC OVERFLOW")
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
        }
        check("overflow/incident", incident_embed("lol", "eun1", parsed, huge))
        check("overflow/panel", status_panel_embed("lol", "eun1", parsed))
        check(
            "overflow/news",
            news_embed("cs2", {"title": "Y" * 500, "raw_bbcode": "[p]a[/p]" * 3000}),
        )

        # Oversized third-party region_name must be clamped everywhere it is
        # interpolated (author, title, Region field).
        evil_region = {"region_name": "R" * 600, "ok": False, "items": [huge], "services": []}
        check("overflow/region-author", incident_embed("lol", "eun1", evil_region, huge))
        check("overflow/region-title", status_panel_embed("lol", "eun1", evil_region))
        check("overflow/region-recovery", recovery_embed("lol", "eun1", evil_region, None))

        # ReDoS guard: crafted unclosed-tag payload must convert fast.
        import time as _time

        from gamewatch.embeds import steam_bbcode_to_markdown

        start = _time.monotonic()
        steam_bbcode_to_markdown("[b]" * 60000)
        elapsed = _time.monotonic() - start
        if elapsed > 2.0:
            failures.append(("redos", [f"conversion took {elapsed:.1f}s"]))
        print(f"  redos guard: 180KB hostile payload converted in {elapsed:.3f}s")

        # Truncation must not leave unbalanced markdown pair tokens.
        from gamewatch.embeds import _trunc

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
