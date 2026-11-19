"""Validate Red-DiscordBot Downloader repository & cog metadata."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from redbot.cogs.downloader.installable import Installable, InstallableType
from redbot.cogs.downloader.json_mixins import RepoJSONMixin

import gamewatch
from gamewatch.constants import (
    GAMES,
    RIOT_RETIRED_REGIONS,
    RIOT_VALID_REGIONS,
    STEAM_NEWS_URL,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
COG_DIR = REPO_ROOT / "gamewatch"
VALID_NEWS_KINDS = {"riot", "steam_events", "eft_bsg", "abi_cms"}


def test_repo_info_json_valid(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="red.downloader"):
        repo = RepoJSONMixin(REPO_ROOT)

    assert not caplog.records, (
        f"Unexpected Downloader warnings: {[r.message for r in caplog.records]}"
    )
    assert repo.author
    assert repo.short
    assert repo.description
    assert repo.install_msg


def test_cog_info_json_valid(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="red.downloader"):
        inst = Installable(COG_DIR)

    assert not caplog.records, (
        f"Unexpected Downloader warnings: {[r.message for r in caplog.records]}"
    )
    assert inst.name == "gamewatch"
    assert inst.type == InstallableType.COG
    assert inst.author
    assert inst.short
    assert inst.description
    assert inst.install_msg
    assert inst.end_user_data_statement
    assert not inst.hidden
    assert not inst.disabled

    raw = json.loads((COG_DIR / "info.json").read_text(encoding="utf-8"))
    assert raw["name"] == "GameWatch"
    assert raw["requirements"] == []
    assert gamewatch.__red_end_user_data_statement__ == raw["end_user_data_statement"]


def test_all_game_configs_and_regex_filters_valid() -> None:
    assert "robrun" in GAMES
    assert GAMES["robrun"]["appid"] == 4262460
    assert GAMES["robrun"]["status_kind"] is None

    for key, cfg in GAMES.items():
        assert cfg.get("uid_prefix"), f"{key} missing uid_prefix"
        assert cfg.get("news_kind") in VALID_NEWS_KINDS, f"{key} invalid news_kind"
        for field in ("title_filter", "exclude_filter", "store_update_patch_filter"):
            pattern = cfg.get(field)
            if pattern:
                re.compile(pattern)


def test_riot_valid_regions_and_steam_url_constants() -> None:
    assert {"kr1", "ru1", "pbe"}.issubset(set(RIOT_VALID_REGIONS["lol"]))
    assert {"kr", "ru", "pbe1"}.issubset(set(RIOT_VALID_REGIONS["tft"]))
    assert {"eu", "na", "ap"}.issubset(set(RIOT_VALID_REGIONS["valorant"]))
    for game, regions in RIOT_VALID_REGIONS.items():
        for retired in ("ph2", "th2"):
            assert retired not in regions, f"{retired} should not be in {game} valid regions"
    assert RIOT_RETIRED_REGIONS["ph2"] == "sg2"
    assert RIOT_RETIRED_REGIONS["th2"] == "sg2"

    assert "feeds=steam_community_announcements" in STEAM_NEWS_URL
    assert "maxlength=0" in STEAM_NEWS_URL
