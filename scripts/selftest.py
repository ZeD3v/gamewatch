"""Standalone self-test: exercises every parser against the live upstreams.

Runs without discord.py or redbot so it can be executed anywhere:

    python3 scripts/selftest.py

Exits non-zero if any source fails.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp

from gamewatch.constants import GAMES, USER_AGENT
from gamewatch.sources import (
    FetchError,
    extract_location,
    fetch_abi_news_detail,
    fetch_game_news,
    fetch_game_status,
)

REGIONS = {"lol": "eun1", "tft": "eun1", "valorant": "eu"}


async def main() -> int:
    failures = []
    steam_api_key = os.environ.get("STEAM_API_KEY", "").strip() or None
    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
        print("=" * 70)
        print("STATUS SOURCES")
        print("=" * 70)
        for game, cfg in GAMES.items():
            if cfg["status_kind"] is None:
                print(f"  {game:9} SKIP (no public status API)")
                continue
            req_service = cfg.get("requires_api_key")
            api_key = steam_api_key if req_service == "steam" else None
            if req_service and not api_key:
                print(f"  {game:9} SKIP (set {req_service.upper()}_API_KEY to test keyed status)")
                continue
            region = REGIONS.get(game, cfg["default_region"])
            try:
                parsed, validator, http = await fetch_game_status(
                    session, game, region, api_key=api_key
                )
                if http != 200 or parsed is None:
                    failures.append(f"status {game} HTTP {http}")
                    print(f"  {game:9} FAIL http={http}")
                    continue
                extra = ""
                if parsed.get("services"):
                    extra += f" services={len(parsed['services'])} worst={parsed['worst']}"
                if parsed.get("datacenters"):
                    extra += f" datacenters={len(parsed['datacenters'])}"
                val_note = f" val={sorted(validator.keys())}" if validator else ""
                print(
                    f"  {game:9} OK  region={parsed.get('region_id')} "
                    f"({parsed.get('region_name')}) ok={parsed['ok']} "
                    f"items={len(parsed['items'])}{extra}{val_note}"
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
                items, _, http = await fetch_game_news(session, game)
                if http != 200 or items is None:
                    failures.append(f"news {game} HTTP {http}")
                    print(f"  {game:9} FAIL http={http}")
                    continue
                if not items:
                    failures.append(f"news {game}: zero items after filtering")
                    print(f"  {game:9} FAIL zero items after filtering")
                    continue
                extra = ""
                if cfg["news_kind"] == "abi_cms" and items[0].get("content_id"):
                    detail_html = await fetch_abi_news_detail(session, items[0]["content_id"])
                    if not detail_html:
                        failures.append(f"news {game}: GetContentInfoById returned empty HTML")
                        print(f"  {game:9} FAIL detail fetch returned empty HTML")
                        continue
                    extra = f" detail_html={len(detail_html)}B"
                print(f"  {game:9} OK  {len(items)} items ({cfg['news_kind']}){extra}")
                for item in items[:2]:
                    stamp = item.get("published_at") or item.get("timestamp")
                    print(f"            - {item['title'][:60]!r} {stamp}")
                    print(f"              {(item.get('url') or '')[:90]}")
                    body = (item.get("body") or "").replace("\n", " ")[:80]
                    print(f"              body: {body!r}")
            except FetchError as exc:
                failures.append(f"news {game}: {exc}")
                print(f"  {game:9} FAIL {exc}")

    # Location extraction: static fixtures (singular + plural)
    singular = (
        "The following game server location is currently unavailable "
        "while we investigate an issue: Dubai"
    )
    plural = (
        "The following game server locations are currently unavailable "
        "while we investigate an issue: Dubai, Bahrain"
    )
    if (
        extract_location({"body": singular, "title": ""}) != "Dubai"
        or extract_location({"body": plural, "title": ""}) != "Dubai, Bahrain"
    ):
        failures.append("extract_location failed on captured Riot fixtures")
        print("  location extraction: FIXTURE FAIL")
    else:
        print("  location extraction: fixtures OK (singular + plural)")

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
