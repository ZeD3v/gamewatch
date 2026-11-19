"""Standalone self-test: exercises every parser against the live upstreams.

Runs without discord.py or redbot so it can be executed anywhere:

    python3 scripts/selftest.py

Exits non-zero if any source fails.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp

from gamewatch.constants import GAMES, USER_AGENT
from gamewatch.sources import (
    FetchError,
    fetch_eft_status,
    fetch_riot_news,
    fetch_riot_status,
    fetch_steam_news,
)

REGIONS = {"lol": "eun1", "tft": "eun1", "valorant": "eu"}


async def main() -> int:
    failures = []
    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
        print("=" * 70)
        print("STATUS SOURCES")
        print("=" * 70)
        for game, cfg in GAMES.items():
            if cfg["status_kind"] is None:
                print(f"  {game:9} SKIP (no public status API)")
                continue
            region = REGIONS.get(game, cfg["default_region"])
            try:
                if cfg["status_kind"] == "riot":
                    parsed, etag, http = await fetch_riot_status(session, game, region)
                else:
                    parsed, etag, http = await fetch_eft_status(session)
                if http != 200 or parsed is None:
                    failures.append(f"status {game} HTTP {http}")
                    print(f"  {game:9} FAIL http={http}")
                    continue
                extra = ""
                if parsed.get("services"):
                    extra = f" services={len(parsed['services'])} worst={parsed['worst']}"
                print(
                    f"  {game:9} OK  region={parsed.get('region_id')} "
                    f"({parsed.get('region_name')}) ok={parsed['ok']} "
                    f"items={len(parsed['items'])}{extra}"
                )
                for item in parsed["items"][:2]:
                    print(f"            - [{item['severity']}] {item['title'][:70]}")
            except FetchError as exc:
                failures.append(f"status {game}: {exc}")
                print(f"  {game:9} FAIL {exc}")

        print()
        print("=" * 70)
        print("NEWS SOURCES")
        print("=" * 70)
        for game, cfg in GAMES.items():
            try:
                if cfg["news_kind"] == "steam":
                    items, etag, http = await fetch_steam_news(session, game)
                else:
                    items, etag, http = await fetch_riot_news(session, game)
                if http != 200 or items is None:
                    failures.append(f"news {game} HTTP {http}")
                    print(f"  {game:9} FAIL http={http}")
                    continue
                if not items:
                    failures.append(f"news {game}: zero items after filtering")
                    print(f"  {game:9} FAIL zero items after filtering")
                    continue
                print(f"  {game:9} OK  {len(items)} items ({cfg['news_kind']})")
                for item in items[:2]:
                    stamp = item.get("published_at") or item.get("timestamp")
                    print(f"            - {item['title'][:60]!r} {stamp}")
                    print(f"              {(item.get('url') or '')[:90]}")
                    body = (item.get("body") or "").replace("\n", " ")[:80]
                    print(f"              body: {body!r}")
            except FetchError as exc:
                failures.append(f"news {game}: {exc}")
                print(f"  {game:9} FAIL {exc}")

    # Location extraction: static fixture (always) + live sample (may skip)
    from gamewatch.sources import extract_location

    fixture = (
        "The following game server location is currently unavailable "
        "while we investigate an issue: Dubai"
    )
    if extract_location({"body": fixture, "title": ""}) != "Dubai":
        failures.append("extract_location failed on captured Riot fixture")
        print("  location extraction: FIXTURE FAIL")
    else:
        print("  location extraction: fixture OK (live incidents may legitimately be absent)")

    print()
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("ALL SOURCES OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
