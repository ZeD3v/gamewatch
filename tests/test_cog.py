"""Red-DiscordBot cog lifecycle, hybrid commands, and state-machine tests."""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
from types import SimpleNamespace

import discord
import pytest

import gamewatch
from gamewatch import gamewatch as module
from gamewatch.constants import GAMES


class _ValueCtx:
    """Mimics redbot's Value.__call__: awaitable AND async context manager."""

    def __init__(self, store: dict, key: str) -> None:
        self._store, self._key = store, key

    def __await__(self):
        async def _get():
            return self._store[self._key]

        return _get().__await__()

    async def __aenter__(self):
        return self._store[self._key]

    async def __aexit__(self, *exc):
        return False


class FakeValue:
    def __init__(self, store: dict, key: str) -> None:
        self._store, self._key = store, key

    def __call__(self) -> _ValueCtx:
        return _ValueCtx(self._store, self._key)

    async def set(self, value) -> None:
        self._store[self._key] = value


class FakeGuildGroup:
    def __init__(self, data: dict) -> None:
        self._data = data

    def __getattr__(self, item: str) -> FakeValue:
        if item not in self._data:
            self._data[item] = None
        return FakeValue(self._data, item)


class FakeConfig:
    def __init__(self) -> None:
        self._global: dict = {}
        self._guild_defaults: dict = {}
        self._guilds: dict[int, dict] = {}

    @classmethod
    def get_conf(cls, *args, **kwargs) -> FakeConfig:
        return cls()

    def register_global(self, **kwargs) -> None:
        self._global.update(copy.deepcopy(kwargs))

    def register_guild(self, **kwargs) -> None:
        self._guild_defaults.update(copy.deepcopy(kwargs))

    def guild(self, guild) -> FakeGuildGroup:
        gid = getattr(guild, "id", 0)
        if gid not in self._guilds:
            self._guilds[gid] = copy.deepcopy(self._guild_defaults)
        return FakeGuildGroup(self._guilds[gid])

    async def all_guilds(self) -> dict[int, dict]:
        return copy.deepcopy(self._guilds)

    def __getattr__(self, item: str) -> FakeValue:
        if item.startswith("_"):
            raise AttributeError(item)
        return FakeValue(self._global, item)


class FakeBot:
    def __init__(self, *, embed_requested_result: bool = True) -> None:
        self.guilds: list = []
        self.added_cogs: list = []
        self._embed_requested_result = embed_requested_result

    async def wait_until_red_ready(self) -> None:
        await asyncio.sleep(3600)

    async def send_to_owners(self, *args, **kwargs) -> None:
        return None

    async def add_cog(self, cog) -> None:
        self.added_cogs.append(cog)
        await cog.cog_load()

    async def embed_requested(self, channel, **kwargs) -> bool:
        return self._embed_requested_result


def _make_parsed(ok: bool, uid: str = "incident-1", severity: str = "warning") -> dict:
    items = (
        []
        if ok
        else [
            {
                "uid": uid,
                "kind": "incidents",
                "id": 1,
                "severity": severity,
                "title": "Login errors",
                "body": "Players may fail to log in.",
                "platforms": ["windows"],
                "created_at": None,
                "updated_at": None,
            }
        ]
    )
    return {
        "region_id": "EUN1",
        "region_name": "EU Nordic & East",
        "items": items,
        "ok": ok,
    }


def _loc_parsed(locs: list[str], *, extra_unlocated: bool = False) -> dict:
    items = [
        {
            "uid": f"loc-{loc}",
            "kind": "incidents",
            "id": loc,
            "severity": "warning",
            "title": "Game Server Unavailable",
            "body": (
                "The following game server location is currently "
                f"unavailable while we investigate an issue: {loc}"
            ),
            "platforms": [],
            "created_at": None,
            "updated_at": None,
        }
        for loc in locs
    ]
    if extra_unlocated:
        items.append(
            {
                "uid": "loc-none",
                "kind": "incidents",
                "id": 99,
                "severity": "critical",
                "title": "Region outage",
                "body": "Players cannot log in.",
                "platforms": [],
                "created_at": None,
                "updated_at": None,
            }
        )
    return {"region_id": "EU", "region_name": "Europe", "items": items, "ok": not items}


@pytest.fixture
def patched_config(monkeypatch):
    monkeypatch.setattr(module, "Config", FakeConfig)


async def test_setup_lifecycle_and_eud_hooks(patched_config) -> None:
    assert gamewatch.GameWatch is module.GameWatch
    bot = FakeBot()
    await gamewatch.setup(bot)  # type: ignore[arg-type]
    assert len(bot.added_cogs) == 1
    cog = bot.added_cogs[0]

    assert cog.session is not None and not cog.session.closed
    assert cog.status_loop.is_running()
    assert cog.news_loop.is_running()

    assert await cog.red_get_data_for_user(user_id=12345) == {}
    await cog.red_delete_data_for_user(requester="user", user_id=12345)
    await cog.red_delete_data_for_user(requester="discord_deleted_user", user_id=12345)

    await cog.cog_unload()
    assert cog.session.closed


async def test_hybrid_commands_channel_types_and_autocomplete(patched_config) -> None:
    cog = module.GameWatch(FakeBot())  # type: ignore[arg-type]
    cmds = {c.qualified_name: c for c in cog.walk_commands()}
    assert len(cmds) == 14
    assert all(c.__class__.__name__.startswith("Hybrid") for c in cmds.values())

    # news & status accept text, announcement, and thread channels
    for cmd_name in ("gamewatch news", "gamewatch status"):
        chan_param = cmds[cmd_name].app_command.parameters[1]
        assert discord.ChannelType.text in chan_param.channel_types
        assert discord.ChannelType.news in chan_param.channel_types
        assert discord.ChannelType.public_thread in chan_param.channel_types

    # Autocomplete callbacks filter cleanly
    all_games = await cog._game_autocomplete(None, "")  # type: ignore[arg-type]
    assert len(all_games) == len(GAMES)

    status_games = await cog._status_game_autocomplete(None, "")  # type: ignore[arg-type]
    assert {c.value for c in status_games} == {"lol", "tft", "valorant", "eft"}

    riot_games = await cog._riot_game_autocomplete(None, "val")  # type: ignore[arg-type]
    assert [c.value for c in riot_games] == ["valorant"]

    sev_choices = await cog._severity_autocomplete(None, "crit")  # type: ignore[arg-type]
    assert [c.value for c in sev_choices] == ["critical"]

    act_choices = await cog._location_action_autocomplete(None, "re")  # type: ignore[arg-type]
    assert {c.value for c in act_choices} == {"remove", "reset"}

    await cog.cog_unload()


async def test_seen_ring_migration_and_bootstrap_order(patched_config) -> None:
    cog = module.GameWatch(FakeBot())  # type: ignore[arg-type]
    for i in range(200):
        await cog._mark_seen("news", "lol", f"patch-{i}")

    seen = await cog.config.seen()
    assert len(seen["news:lol"]) == module.MAX_SEEN_PER_GAME
    assert await cog._is_seen("news", "lol", "patch-199") is True
    assert await cog._is_seen("news", "lol", "patch-0") is False

    # Legacy un-namespaced key migrates on write
    async with cog.config.seen() as sn:
        sn["cs2"] = ["legacy-uid"]
    assert await cog._is_seen("news", "cs2", "legacy-uid") is True
    await cog._mark_seen("news", "cs2", "new-uid")
    sn = await cog.config.seen()
    assert "cs2" not in sn and "legacy-uid" in sn["news:cs2"]

    # Bootstrap preserves NEWEST items on 150-item newest-first feeds
    riot_items = [{"uid": f"riot-{i}"} for i in range(150)]  # riot-0 is newest
    await cog._bootstrap_seen("valorant", riot_items)  # type: ignore[arg-type]
    assert await cog._is_seen("news", "valorant", "riot-0") is True
    assert await cog._is_seen("news", "valorant", "riot-79") is True
    assert await cog._is_seen("news", "valorant", "riot-149") is False
    assert (await cog.config.bootstrapped())["valorant"] is True

    await cog.cog_unload()


async def test_status_debounce_recovery_and_location_filter(patched_config) -> None:
    cog = module.GameWatch(FakeBot())  # type: ignore[arg-type]
    announced: list[str] = []
    recovered: list[str] = []

    async def fake_incident(game, region, parsed, item, targets):
        announced.append(item["uid"])

    async def fake_recovery(game, region, parsed, targets):
        recovered.append(region)

    cog._announce_incident = fake_incident  # type: ignore[method-assign]
    cog._announce_recovery = fake_recovery  # type: ignore[method-assign]

    bad = _make_parsed(False)
    good = _make_parsed(True)

    await cog._process_status("lol", "eun1", bad, [])
    assert cog._pending.get("lol-eun1") == 1
    assert announced == []

    await cog._process_status("lol", "eun1", bad, [])
    assert announced == ["incident-1"]

    await cog._process_status("lol", "eun1", bad, [])
    assert announced == ["incident-1"]

    # Same uid in distinct region still announces
    await cog._process_status("lol", "euw1", bad, [])
    await cog._process_status("lol", "euw1", bad, [])
    assert announced == ["incident-1", "incident-1"]

    # Recovery after sustained outage
    store = await cog.config.last_status()
    store["lol-eun1"]["since"] = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)
    ).isoformat()
    await cog._process_status("lol", "eun1", good, [])
    assert recovered == ["eun1"]
    assert cog._pending.get("lol-eun1") is None

    # Short blip suppressed
    announced.clear()
    recovered.clear()
    await cog._process_status("tft", "eun1", _make_parsed(False, uid="blip-1"), [])
    await cog._process_status("tft", "eun1", _make_parsed(True), [])
    assert announced == []
    assert recovered == []

    # Location filtering through real state machine
    gcfg = {"region": "eu", "status_channel": 123, "location_allow": ["warsaw"]}
    ltargets = [(SimpleNamespace(id=1), gcfg)]
    bad_far = _loc_parsed(["Dubai"])
    for _ in range(4):
        await cog._process_status("valorant", "eu", dict(bad_far), ltargets)  # type: ignore[arg-type]
    assert announced == []
    ring = (await cog.config.seen()).get("status:valorant", [])
    assert ring.count("eu:loc-Dubai") == 1
    assert (await cog.config.last_status()).get("valorant-eu", {}).get("ok", True) is True
    assert cog._pending.get("valorant-eu") is None

    mixed = _loc_parsed(["Dubai", "Warsaw"])
    await cog._process_status("valorant", "eu", dict(mixed), ltargets)  # type: ignore[arg-type]
    await cog._process_status("valorant", "eu", dict(mixed), ltargets)  # type: ignore[arg-type]
    assert announced == ["loc-Warsaw"]

    ls = await cog.config.last_status()
    ls.setdefault("valorant-eu", {"ok": False})["since"] = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)
    ).isoformat()
    await cog._process_status("valorant", "eu", dict(bad_far), ltargets)  # type: ignore[arg-type]
    assert recovered == ["eu"]

    await cog.cog_unload()


async def test_multiguild_location_isolation_and_embed_fallback(patched_config) -> None:
    bot = FakeBot(embed_requested_result=False)
    cog = module.GameWatch(bot)  # type: ignore[arg-type]

    sent: list[tuple[int, dict]] = []

    class FakeChannel:
        def __init__(self, cid: int, guild) -> None:
            self.id = cid
            self.name = f"chan-{cid}"
            self.guild = guild

        def permissions_for(self, member):
            return SimpleNamespace(
                send_messages=True,
                send_messages_in_threads=True,
                embed_links=True,
            )

        async def send(self, content=None, embed=None, allowed_mentions=None):
            sent.append((self.id, {"content": content, "embed": embed}))

    class FakeGuild:
        def __init__(self, gid: int, cid: int) -> None:
            self.id = gid
            self.name = f"guild-{gid}"
            self.me = object()
            self._chan = FakeChannel(cid, self)

        def get_channel_or_thread(self, cid: int):
            return self._chan if cid == self._chan.id else None

        def get_role(self, rid: int):
            return None

    guild_a = FakeGuild(101, 1001)
    guild_b = FakeGuild(102, 1002)
    targets = [
        (guild_a, {"region": "eu", "status_channel": 1001, "location_allow": ["warsaw"]}),
        (guild_b, {"region": "eu", "status_channel": 1002, "location_allow": []}),
    ]
    parsed = _loc_parsed(["Dubai", "Warsaw"])

    # Dubai goes ONLY to guild_b (unfiltered), and falls back to plain text
    # because embed_requested=False.
    await cog._announce_incident("valorant", "eu", parsed, parsed["items"][0], targets)
    assert [cid for cid, _ in sent] == [1002]
    assert sent[0][1]["embed"] is None
    assert "Dubai" in (sent[0][1]["content"] or "")

    # Warsaw goes to BOTH guilds
    sent.clear()
    await cog._announce_incident("valorant", "eu", parsed, parsed["items"][1], targets)
    assert [cid for cid, _ in sent] == [1001, 1002]

    await cog.cog_unload()
