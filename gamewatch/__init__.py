"""Package for the GameWatch Red-DiscordBot cog."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from redbot.core.bot import Red

with Path(__file__).parent.joinpath("info.json").open(encoding="utf-8") as _fp:
    __red_end_user_data_statement__: str = json.load(_fp)["end_user_data_statement"]


def __getattr__(name: str) -> Any:
    if name == "GameWatch":
        from .gamewatch import GameWatch

        return GameWatch
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


async def setup(bot: Red) -> None:
    """Load the GameWatch cog."""
    from .gamewatch import GameWatch

    await bot.add_cog(GameWatch(bot))
