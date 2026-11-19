"""Validate Red-DiscordBot Downloader repository & cog metadata."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from redbot.cogs.downloader.installable import Installable, InstallableType
from redbot.cogs.downloader.json_mixins import RepoJSONMixin

import gamewatch
from gamewatch.constants import GAMES

REPO_ROOT = Path(__file__).resolve().parent.parent
COG_DIR = REPO_ROOT / "gamewatch"


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
    assert gamewatch.__red_end_user_data_statement__ == raw["end_user_data_statement"]


def test_all_game_regex_filters_compile() -> None:
    for cfg in GAMES.values():
        for field in ("title_filter", "exclude_filter"):
            pattern = cfg.get(field)
            if pattern:
                re.compile(pattern)
