"""Red-DiscordBot cog lifecycle, hybrid commands, concurrency, migration & state tests."""

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
from gamewatch.sources import FetchError


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
    def __init__(
        self,
        *,
        embed_requested_result: bool = True,
        api_tokens: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self.guilds: list = []
        self.added_cogs: list = []
        self._embed_requested_result = embed_requested_result
        self.api_tokens: dict[str, dict[str, str]] = api_tokens or {}

    async def wait_until_red_ready(self) -> None:
        await asyncio.sleep(3600)

    async def send_to_owners(self, *args, **kwargs) -> None:
        return None

    async def add_cog(self, cog) -> None:
        self.added_cogs.append(cog)
        await cog.cog_load()

    async def embed_requested(self, channel, **kwargs) -> bool:
        return self._embed_requested_result

    async def get_shared_api_tokens(self, service_name: str | None = None) -> dict:
        if service_name is None:
            return dict(self.api_tokens)
        return dict(self.api_tokens.get(service_name) or {})


def _make_parsed(
    ok: bool,
    uid: str = "incident-1",
    severity: str = "warning",
    maintenance_status: str | None = None,
) -> dict:
    items = (
        []
        if ok and maintenance_status is None
        else [
            {
                "uid": uid,
                "kind": "maintenances" if maintenance_status else "incidents",
                "id": 1,
                "severity": severity,
                "maintenance_status": maintenance_status,
                "title": "Planned maintenance" if maintenance_status else "Login errors",
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
            "maintenance_status": None,
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
                "maintenance_status": None,
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
    assert any(c.value == "robrun" for c in all_games)

    status_games = await cog._status_game_autocomplete(None, "")  # type: ignore[arg-type]
    assert {c.value for c in status_games} == {"lol", "tft", "valorant", "eft", "cs2"}

    loc_games = await cog._location_game_autocomplete(None, "")  # type: ignore[arg-type]
    assert {c.value for c in loc_games} == {"lol", "tft", "valorant", "cs2"}

    riot_games = await cog._riot_game_autocomplete(None, "val")  # type: ignore[arg-type]
    assert [c.value for c in riot_games] == ["valorant"]

    sev_choices = await cog._severity_autocomplete(None, "crit")  # type: ignore[arg-type]
    assert [c.value for c in sev_choices] == ["critical"]

    act_choices = await cog._location_action_autocomplete(None, "re")  # type: ignore[arg-type]
    assert {c.value for c in act_choices} == {"remove", "reset"}

    # All hybrid slash commands & parameter descriptions fit Discord's 100-char cap
    app_group = cog.gamewatch.app_command
    assert app_group.guild_only is True
    tree_dict = app_group.to_dict(
        discord.app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))
    )
    assert len(tree_dict["description"]) <= 100
    assert len(tree_dict["options"]) == 13
    for opt in tree_dict["options"]:
        assert 1 <= len(opt["description"]) <= 100
        for param in opt.get("options", []):
            assert 1 <= len(param["description"]) <= 100

    # Public upstream-fetching commands have a channel cooldown
    assert cmds["gamewatch check"]._buckets._cooldown is not None
    assert cmds["gamewatch latest"]._buckets._cooldown is not None

    await cog.cog_unload()


async def test_region_autocomplete_and_retired_region_guard(patched_config, monkeypatch) -> None:
    cog = module.GameWatch(FakeBot())  # type: ignore[arg-type]

    lol_inter = SimpleNamespace(namespace=SimpleNamespace(game="lol"))
    tft_inter = SimpleNamespace(namespace=SimpleNamespace(game="TFT"))

    lol_regions = {c.value for c in await cog._region_autocomplete(lol_inter, "")}  # type: ignore[arg-type]
    tft_regions = {c.value for c in await cog._region_autocomplete(tft_inter, "")}  # type: ignore[arg-type]
    union_regions = await cog._region_autocomplete(None, "")  # type: ignore[arg-type]

    assert {"kr1", "ru1", "pbe"}.issubset(lol_regions)
    assert "ph2" not in lol_regions and "th2" not in lol_regions
    assert {"kr", "ru", "pbe1"}.issubset(tft_regions)
    assert "ph2" not in tft_regions and "th2" not in tft_regions
    assert len(union_regions) <= 25
    assert all(c.value not in ("ph2", "th2") for c in union_regions)

    # Retired region must be rejected BEFORE making any HTTP call
    called = False

    async def fail_if_called(*args, **kwargs):
        nonlocal called
        called = True
        return None, {}, 200

    monkeypatch.setattr(module, "fetch_riot_status", fail_if_called)

    sent_msgs: list[str] = []

    class FakeCtx:
        guild = SimpleNamespace(id=1)

        async def send(self, content=None, **kwargs):
            sent_msgs.append(str(content))

    await cog.gw_region.callback(cog, FakeCtx(), "lol", "ph2")  # type: ignore[arg-type]
    assert not called
    assert any("sg2" in m for m in sent_msgs)

    # For VALORANT, ph2 is NOT redirected to invalid sg2; proceeds to Riot validation
    sent_msgs.clear()

    async def fake_val_404(*args, **kwargs):
        nonlocal called
        called = True
        return None, {}, 404

    monkeypatch.setattr(module, "fetch_riot_status", fake_val_404)

    class FakeValCtx(FakeCtx):
        def typing(self):
            class _Noop:
                async def __aenter__(self):
                    return None

                async def __aexit__(self, *exc):
                    return False

            return _Noop()

    await cog.gw_region.callback(cog, FakeValCtx(), "valorant", "ph2")  # type: ignore[arg-type]
    assert called
    assert all("sg2" not in m for m in sent_msgs)

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


async def test_uid_migration_lol_en_gb_legacy_keys_and_empty_ring_no_spam(
    patched_config, monkeypatch
) -> None:
    bot = FakeBot()
    guild = SimpleNamespace(id=1, name="g1")
    bot.guilds = [guild]
    cog = module.GameWatch(bot)  # type: ignore[arg-type]

    # Configure channels for lol, cs2, abi
    async with cog.config.guild(guild).games() as g:
        g["lol"] = {"news_channel": 11, "enabled": True}
        g["cs2"] = {"news_channel": 22, "enabled": True}
        g["abi"] = {"news_channel": 33, "enabled": True}

    # Pre-upgrade state:
    # - lol has legacy un-namespaced seen["lol"] with /en-gb/ URL
    # - cs2 has legacy un-namespaced seen["cs2"] with old steam-123
    # - abi is bootstrapped=True with EMPTY seen["news:abi"] = []
    async with cog.config.bootstrapped() as boot:
        boot["lol"] = True
        boot["cs2"] = True
        boot["abi"] = True
    async with cog.config.seen() as sn:
        sn["lol"] = [
            "riotnews-lol-https://www.leagueoflegends.com/en-gb/news/game-updates/patch-26-23-notes"
        ]
        sn["cs2"] = ["steam-12345"]
        sn["news:abi"] = []

    announced: list[tuple[str, str]] = []

    async def fake_announce(game, item, targets):
        announced.append((game, item["uid"]))

    cog._announce_news = fake_announce  # type: ignore[method-assign]

    # Poll 1: empty items for abi first -> must NOT clear or break migration
    poll_num = 0

    async def fake_fetch_news(session, game, *, validator=None):
        if game == "lol":
            return (
                [
                    {
                        "uid": "riotnews-lol-https://www.leagueoflegends.com/en-us/news/game-updates/patch-26-24-notes",
                        "title": "Patch 26.24 Notes",
                    },
                    {
                        "uid": "riotnews-lol-https://www.leagueoflegends.com/en-us/news/game-updates/patch-26-23-notes",
                        "title": "Patch 26.23 Notes",
                    },
                ],
                {},
                200,
            )
        if game == "cs2":
            items = [{"uid": "steam-ev-100", "title": "CS2 Update"}]
            if poll_num >= 2:
                items.insert(0, {"uid": "steam-ev-200", "title": "New CS2 Update"})
            return items, {}, 200
        if game == "abi":
            if poll_num == 1:
                return [], {}, 200
            items = [{"uid": "abi-cms-old", "content_id": "old", "title": "Old Live Update"}]
            if poll_num >= 3:
                items.insert(
                    0, {"uid": "abi-cms-new", "content_id": "new", "title": "New Live Update"}
                )
            return items, {}, 200
        return [], {}, 200

    monkeypatch.setattr(module, "fetch_game_news", fake_fetch_news)
    monkeypatch.setattr(
        module, "fetch_abi_news_detail", lambda s, cid: asyncio.sleep(0, result=None)
    )

    # Poll 1:
    # - lol migrates /en-gb/ -> /en-us/ in-place, suppresses 26.23, announces brand-new 26.24!
    # - cs2 silently re-seeds steam-ev-100 (0 announcements)
    # - abi gets [] (returns False, does nothing)
    poll_num = 1
    await cog._check_news_once()
    assert announced == [
        (
            "lol",
            "riotnews-lol-https://www.leagueoflegends.com/en-us/news/game-updates/patch-26-24-notes",
        )
    ]
    sn = await cog.config.seen()
    assert "lol" not in sn and "cs2" not in sn
    assert "steam-ev-100" in sn["news:cs2"]

    # Poll 2:
    # - cs2 announces brand-new steam-ev-200
    # - abi sees first non-empty items while seen["news:abi"] == [] -> silently re-seeds
    announced.clear()
    poll_num = 2
    await cog._check_news_once()
    assert announced == [("cs2", "steam-ev-200")]
    assert "abi-cms-old" in (await cog.config.seen())["news:abi"]

    # Poll 3: abi announces brand-new abi-cms-new
    announced.clear()
    poll_num = 3
    await cog._check_news_once()
    assert announced == [("abi", "abi-cms-new")]

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


async def test_scheduled_maintenance_independent_debounce_and_coexistence(patched_config) -> None:
    cog = module.GameWatch(FakeBot())  # type: ignore[arg-type]
    announced: list[str] = []
    recovered: list[str] = []

    async def fake_incident(game, region, parsed, item, targets):
        announced.append(item["uid"])

    async def fake_recovery(game, region, parsed, targets):
        recovered.append(region)

    cog._announce_incident = fake_incident  # type: ignore[method-assign]
    cog._announce_recovery = fake_recovery  # type: ignore[method-assign]

    sched = _make_parsed(True, uid="maint-sched-1", severity="info", maintenance_status="scheduled")
    clean = _make_parsed(True)

    # 1-poll scheduled blip followed by clean resets _pending_scheduled
    await cog._process_status("lol", "eun1", sched, [])
    assert cog._pending_scheduled.get("lol-eun1") == 1
    assert announced == []
    await cog._process_status("lol", "eun1", clean, [])
    assert cog._pending_scheduled.get("lol-eun1") is None

    # 2 consecutive polls announce scheduled notice once WITHOUT setting last_status ok=False
    await cog._process_status("lol", "eun1", sched, [])
    assert announced == []
    await cog._process_status("lol", "eun1", sched, [])
    assert announced == ["maint-sched-1"]
    assert (await cog.config.last_status()).get("lol-eun1", {}).get("ok", True) is True

    # Clearing scheduled notice emits ZERO recovery
    await cog._process_status("lol", "eun1", clean, [])
    assert recovered == []

    # Simultaneous active incident + scheduled notice; active clearing fires recovery
    announced.clear()
    active_and_sched = {
        "region_id": "EUN1",
        "region_name": "EU Nordic & East",
        "ok": False,
        "items": [
            _make_parsed(False, uid="inc-active-1")["items"][0],
            _make_parsed(
                True, uid="maint-sched-2", severity="info", maintenance_status="scheduled"
            )["items"][0],
        ],
    }
    await cog._process_status("lol", "eun1", active_and_sched, [])
    await cog._process_status("lol", "eun1", active_and_sched, [])
    assert set(announced) == {"inc-active-1", "maint-sched-2"}

    ls = await cog.config.last_status()
    ls["lol-eun1"]["since"] = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)
    ).isoformat()
    # Active clears while scheduled remains -> recovery MUST fire!
    sched2_only = _make_parsed(
        True, uid="maint-sched-2", severity="info", maintenance_status="scheduled"
    )
    announced.clear()
    await cog._process_status("lol", "eun1", sched2_only, [])
    assert recovered == ["eun1"]
    assert announced == []  # maint-sched-2 not re-announced

    # Transition scheduled -> in_progress announces warning after 2 polls and later recovers
    inprog = _make_parsed(
        False, uid="maint-inprog-2", severity="warning", maintenance_status="in_progress"
    )
    recovered.clear()
    await cog._process_status("lol", "eun1", inprog, [])
    assert announced == []
    await cog._process_status("lol", "eun1", inprog, [])
    assert announced == ["maint-inprog-2"]
    assert cog._pending_scheduled.get("lol-eun1") is None

    ls = await cog.config.last_status()
    ls["lol-eun1"]["since"] = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)
    ).isoformat()
    await cog._process_status("lol", "eun1", clean, [])
    assert recovered == ["eun1"]

    await cog.cog_unload()


async def test_concurrent_loops_isolate_failures_validators_and_lazy_abi_detail(
    patched_config, monkeypatch
) -> None:
    bot = FakeBot()
    guild = SimpleNamespace(id=1, name="g1")
    bot.guilds = [guild]
    cog = module.GameWatch(bot)  # type: ignore[arg-type]

    async with cog.config.guild(guild).games() as g:
        g["lol"] = {"news_channel": 11, "status_channel": 101, "region": "eun1", "enabled": True}
        g["abi"] = {"news_channel": 22, "enabled": True}
        g["eft"] = {"status_channel": 102, "region": "global", "enabled": True}
    async with cog.config.bootstrapped() as boot:
        boot["lol"] = True
        boot["abi"] = True
    async with cog.config.seen() as sn:
        sn["news:lol"] = ["riotnews-lol-old"]
        sn["news:abi"] = ["abi-cms-seen1"]

    # News loop: lol fails while abi succeeds; detail called ONLY for unseen item
    detail_calls: list[str] = []
    announced_items: list[dict] = []

    async def fake_fetch_news(session, game, *, validator=None):
        if game == "lol":
            raise FetchError("riot boom")
        if game == "abi":
            return (
                [
                    {
                        "uid": "abi-cms-new1",
                        "content_id": "new1",
                        "title": "Nov. 10 Live Update",
                        "body": "Preview fallback",
                    },
                    {
                        "uid": "abi-cms-seen1",
                        "content_id": "seen1",
                        "title": "Oct. 27 Live Update",
                        "body": "Old preview",
                    },
                ],
                {},
                200,
            )
        return [], {}, 200

    async def fake_abi_detail(session, content_id):
        detail_calls.append(content_id)
        return "<p>Rich HTML notes</p>"

    async def fake_announce_news(game, item, targets):
        announced_items.append(item)

    monkeypatch.setattr(module, "fetch_game_news", fake_fetch_news)
    monkeypatch.setattr(module, "fetch_abi_news_detail", fake_abi_detail)
    cog._announce_news = fake_announce_news  # type: ignore[method-assign]

    await cog._check_news_once()
    assert cog._failures.get("news-lol") == 1
    assert detail_calls == ["new1"]  # NEVER called for seen1!
    assert len(announced_items) == 1
    assert announced_items[0]["raw_html"] == "<p>Rich HTML notes</p>"

    # Detail returning None degrades cleanly to preview body
    async def fail_abi_detail(session, content_id):
        return None

    async def fake_fetch_news_2(session, game, *, validator=None):
        if game == "abi":
            return (
                [
                    {
                        "uid": "abi-cms-new2",
                        "content_id": "new2",
                        "title": "Update 2",
                        "body": "Fallback",
                    }
                ],
                {},
                200,
            )
        return None, {}, 304

    monkeypatch.setattr(module, "fetch_game_news", fake_fetch_news_2)
    monkeypatch.setattr(module, "fetch_abi_news_detail", fail_abi_detail)
    announced_items.clear()
    await cog._check_news_once()
    assert len(announced_items) == 1
    assert "raw_html" not in announced_items[0]
    assert announced_items[0]["body"] == "Fallback"

    # Status loop: stores CacheValidator (etag + last_modified) & feeds cached parse on 304
    status_calls = 0
    seen_validators: list[dict] = []
    status_announced: list[str] = []

    async def fake_fetch_status(session, game, region, *, validator=None):
        nonlocal status_calls
        if game == "lol":
            status_calls += 1
            seen_validators.append(dict(validator or {}))
            if status_calls == 1:
                return (
                    _make_parsed(False, uid="inc-304"),
                    {"etag": '"e1"', "last_modified": "Thu, 19 Nov 2026 10:00:00 GMT"},
                    200,
                )
            return None, {"etag": '"e1"', "last_modified": "Thu, 19 Nov 2026 10:00:00 GMT"}, 304
        return _make_parsed(True), {}, 200

    async def fake_announce_inc(game, region, parsed, item, targets):
        status_announced.append(item["uid"])

    monkeypatch.setattr(module, "fetch_game_status", fake_fetch_status)
    cog._announce_incident = fake_announce_inc  # type: ignore[method-assign]

    await cog._check_status_once()
    assert cog._validators["status-lol-eun1"] == {
        "etag": '"e1"',
        "last_modified": "Thu, 19 Nov 2026 10:00:00 GMT",
    }
    assert status_announced == []  # pending=1

    # Poll 2 returns 304 -> validator was sent AND cached parse confirms pending=2!
    await cog._check_status_once()
    assert seen_validators[1] == {"etag": '"e1"', "last_modified": "Thu, 19 Nov 2026 10:00:00 GMT"}
    assert status_announced == ["inc-304"]

    # Unexpected non-FetchError Exception in one game must be isolated without aborting others
    async def boom_lol_ok_abi(session, game, *, validator=None):
        if game == "lol":
            raise RuntimeError("unexpected parser crash")
        return [], {}, 200

    monkeypatch.setattr(module, "fetch_game_news", boom_lol_ok_abi)
    prev_fails = cog._failures.get("news-lol", 0)
    await cog._check_news_once()
    assert cog._failures["news-lol"] == prev_fails + 1
    assert cog._last_health["news-abi"]["http"] == 200

    # CancelledError must propagate cleanly
    async def cancelled_fetch(session, game, *, validator=None):
        raise asyncio.CancelledError()

    monkeypatch.setattr(module, "fetch_game_news", cancelled_fetch)
    with pytest.raises(asyncio.CancelledError):
        await cog._check_news_once()

    # resetseen must clear cached validators and status state so next poll starts fresh
    cog._validators["news-lol"] = {"last_modified": "Thu, 19 Nov 2026 10:00:00 GMT"}
    cog._validators["status-lol-eun1"] = {"etag": '"e1"'}
    cog._last_parsed["status-lol-eun1"] = _make_parsed(False)  # type: ignore[assignment]
    cog._pending["lol-eun1"] = 1
    cog._pending_scheduled["lol-eun1"] = 1
    cog._notified_guilds["lol-eun1"] = {1}
    async with cog.config.last_status() as ls:
        ls["lol-eun1"] = {"ok": False, "since": "2026-11-19T10:00:00+00:00"}

    reset_msgs: list[str] = []

    class FakeResetCtx:
        async def send(self, content=None, **kwargs):
            reset_msgs.append(str(content))

    await cog.gw_resetseen.callback(cog, FakeResetCtx(), "lol")  # type: ignore[arg-type]
    assert "news-lol" not in cog._validators
    assert "status-lol-eun1" not in cog._validators
    assert "status-lol-eun1" not in cog._last_parsed
    assert "lol-eun1" not in cog._pending
    assert "lol-eun1" not in cog._pending_scheduled
    assert "lol-eun1" not in cog._notified_guilds
    assert "lol-eun1" not in (await cog.config.last_status())
    assert any("re-seeded silently" in m for m in reset_msgs)

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
                view_channel=True,
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

    # Recovery for that Dubai-only outage must go ONLY to guild_b, never guild_a
    sent.clear()
    await cog._announce_recovery("valorant", "eu", _loc_parsed([]), targets)
    assert [cid for cid, _ in sent] == [1002]

    # Warsaw goes to BOTH guilds, so recovery after Warsaw goes to BOTH guilds
    sent.clear()
    await cog._announce_incident("valorant", "eu", parsed, parsed["items"][1], targets)
    assert [cid for cid, _ in sent] == [1001, 1002]
    sent.clear()
    await cog._announce_recovery("valorant", "eu", _loc_parsed([]), targets)
    assert [cid for cid, _ in sent] == [1001, 1002]

    await cog.cog_unload()


async def test_public_defaults_locations_comma_split_role_and_permission_warnings(
    patched_config,
) -> None:
    cog = module.GameWatch(FakeBot())  # type: ignore[arg-type]
    # Default location allowlist is empty so non-EU / global regions never lose alerts
    assert cog._effective_allowlist({}, "valorant") == []

    msgs: list[str] = []

    class FakeChannel:
        def __init__(self, cid: int, guild, *, send_ok: bool, embed_ok: bool) -> None:
            self.id = cid
            self.mention = f"<#{cid}>"
            self.guild = guild
            self._send_ok = send_ok
            self._embed_ok = embed_ok

        def permissions_for(self, member):
            return SimpleNamespace(
                view_channel=self._send_ok,
                send_messages=self._send_ok,
                send_messages_in_threads=self._send_ok,
                embed_links=self._embed_ok,
            )

    class FakeRole:
        def __init__(self, rid: int, *, default: bool = False, mentionable: bool = True) -> None:
            self.id = rid
            self.mention = f"<@&{rid}>"
            self._default = default
            self.mentionable = mentionable

        def is_default(self) -> bool:
            return self._default

    guild = SimpleNamespace(
        id=55,
        me=SimpleNamespace(guild_permissions=SimpleNamespace(mention_everyone=False)),
    )

    class FakeCtx:
        clean_prefix = "!"

        def __init__(self) -> None:
            self.guild = guild

        async def send(self, content=None, **kwargs):
            msgs.append(str(content))

    ctx = FakeCtx()

    # Comma-separated locations add & remove
    await cog.gw_locations.callback(
        cog, ctx, "valorant", "add", location="Warsaw, Frankfurt, paris"
    )  # type: ignore[arg-type]
    gcfg = await cog._guild_game_cfg(guild, "valorant")  # type: ignore[arg-type]
    assert gcfg["location_allow"] == ["frankfurt", "paris", "warsaw"]

    await cog.gw_locations.callback(cog, ctx, "valorant", "remove", location="paris, frankfurt")  # type: ignore[arg-type]
    gcfg = await cog._guild_game_cfg(guild, "valorant")  # type: ignore[arg-type]
    assert gcfg["location_allow"] == ["warsaw"]

    # gw_role rejects @everyone and warns on non-mentionable role without mention_everyone
    msgs.clear()
    await cog.gw_role.callback(cog, ctx, "lol", FakeRole(55, default=True))  # type: ignore[arg-type]
    assert any("@everyone" in m for m in msgs)
    assert (await cog._guild_game_cfg(guild, "lol")).get("role") is None  # type: ignore[arg-type]

    msgs.clear()
    await cog.gw_role.callback(cog, ctx, "lol", FakeRole(99, mentionable=False))  # type: ignore[arg-type]
    assert any("not mentionable" in m for m in msgs)
    assert (await cog._guild_game_cfg(guild, "lol")).get("role") == 99  # type: ignore[arg-type]

    # gw_news warns on missing send/embed permissions; gw_status also reminds Riot region
    msgs.clear()
    bad_ch = FakeChannel(700, guild, send_ok=False, embed_ok=False)
    await cog.gw_news.callback(cog, ctx, "lol", bad_ch)  # type: ignore[arg-type]
    assert any("missing" in m.lower() and "embed links" in m.lower() for m in msgs)

    msgs.clear()
    ok_ch = FakeChannel(701, guild, send_ok=True, embed_ok=True)
    await cog.gw_status.callback(cog, ctx, "lol", ok_ch)  # type: ignore[arg-type]
    assert any("eun1" in m and "!gamewatch region lol" in m for m in msgs)

    await cog.cog_unload()


async def test_steam_api_key_status_gating_polling_and_token_update(
    patched_config, monkeypatch
) -> None:
    bot = FakeBot()
    guild = SimpleNamespace(
        id=88,
        name="g88",
        me=SimpleNamespace(guild_permissions=SimpleNamespace(mention_everyone=True)),
    )
    bot.guilds = [guild]
    cog = module.GameWatch(bot)  # type: ignore[arg-type]

    sent_text: list[str] = []
    sent_embeds: list[discord.Embed] = []

    class FakeChannel:
        id = 8801
        mention = "<#8801>"

        def __init__(self) -> None:
            self.guild = guild

        def permissions_for(self, member):
            return SimpleNamespace(
                view_channel=True,
                send_messages=True,
                send_messages_in_threads=True,
                embed_links=True,
            )

    class FakeCtx:
        clean_prefix = "!"

        def __init__(self) -> None:
            self.guild = guild

        async def embed_requested(self) -> bool:
            return True

        async def embed_colour(self) -> discord.Colour:
            return discord.Colour.blue()

        def typing(self):
            class _Noop:
                async def __aenter__(self):
                    return None

                async def __aexit__(self, *exc):
                    return False

            return _Noop()

        async def send(self, content=None, *, embed=None, **kwargs):
            if content is not None:
                sent_text.append(str(content))
            if embed is not None:
                sent_embeds.append(embed)

    ctx = FakeCtx()
    chan = FakeChannel()

    # 1. Without a Steam API key: gw_games marks CS2 under patch notes only,
    #    gw_status and gw_check guide owner to `[p]set api steam`, and status loop skips cleanly.
    await cog.gw_games.callback(cog, ctx)  # type: ignore[arg-type]
    games_fields = {f.name: f.value for f in sent_embeds[-1].fields}
    assert "cs2" not in (games_fields.get("\U0001f4e1 Status + patch notes") or "")
    assert "Steam Web API key" in (games_fields.get("\U0001f4f0 Patch notes only") or "")

    sent_text.clear()
    await cog.gw_status.callback(cog, ctx, "cs2", chan)  # type: ignore[arg-type]
    assert any("!set api steam api_key" in m for m in sent_text)
    assert (await cog._guild_game_cfg(guild, "cs2")).get("status_channel") is None  # type: ignore[arg-type]

    sent_text.clear()
    await cog.gw_check.callback(cog, ctx, "cs2")  # type: ignore[arg-type]
    assert any("!set api steam api_key" in m for m in sent_text)

    # Even if a guild had cs2 status configured before key removal, status loop skips without error
    async with cog.config.guild(guild).games() as g:
        g["cs2"] = {"status_channel": 8801, "region": "global", "enabled": True}

    fetch_calls: list[tuple[str, str | None]] = []

    async def fake_fetch_status(session, game, region, *, validator=None, api_key=None):
        fetch_calls.append((game, api_key))
        return (
            {
                "region_id": "global",
                "region_name": "Global",
                "ok": True,
                "services": [{"name": "Sessions Logon", "status": 0, "label": "Normal"}],
                "datacenters": [{"name": "EU North", "region": "EU North", "status": 0}],
                "items": [],
                "worst": 0,
            },
            {},
            200,
        )

    monkeypatch.setattr(module, "fetch_game_status", fake_fetch_status)
    await cog._check_status_once()
    assert fetch_calls == []
    assert cog._failures.get("status-cs2-global", 0) == 0

    sent_text.clear()
    await cog.gw_diag.callback(cog, ctx)  # type: ignore[arg-type]
    assert any("no steam api_key" in m and "steam=not set" in m for m in sent_text)

    # Disabling cs2 status still works even without a key
    sent_text.clear()
    await cog.gw_status.callback(cog, ctx, "cs2", None)  # type: ignore[arg-type]
    assert any("disabled" in m.lower() for m in sent_text)

    # 2. Configure Steam API key via Red's shared API tokens -> CS2 unlocks immediately!
    cog._failures["status-cs2-global"] = 5
    cog._alerted.add("status-cs2-global")
    bot.api_tokens["steam"] = {"api_key": "VALID_KEY_XYZ"}
    await cog.on_red_api_tokens_update("steam", bot.api_tokens["steam"])
    assert "status-cs2-global" not in cog._failures
    assert "status-cs2-global" not in cog._alerted

    sent_embeds.clear()
    await cog.gw_games.callback(cog, ctx)  # type: ignore[arg-type]
    games_fields = {f.name: f.value for f in sent_embeds[-1].fields}
    assert "cs2" in (games_fields.get("\U0001f4e1 Status + patch notes") or "")

    sent_text.clear()
    await cog.gw_status.callback(cog, ctx, "cs2", chan)  # type: ignore[arg-type]
    assert (await cog._guild_game_cfg(guild, "cs2"))["status_channel"] == 8801  # type: ignore[arg-type]

    # Location allowlist works for CS2 datacenters
    await cog.gw_locations.callback(cog, ctx, "cs2", "add", location="EU North, Poland")  # type: ignore[arg-type]
    assert (await cog._guild_game_cfg(guild, "cs2"))["location_allow"] == ["eu north", "poland"]  # type: ignore[arg-type]

    # Status loop now polls CS2 with the Steam key
    await cog._check_status_once()
    assert fetch_calls == [("cs2", "VALID_KEY_XYZ")]

    # gw_check succeeds with key and gives a helpful message on HTTP 403 (bad key)
    sent_embeds.clear()
    await cog.gw_check.callback(cog, ctx, "cs2")  # type: ignore[arg-type]
    assert len(sent_embeds) == 1
    assert "Counter-Strike 2" in (sent_embeds[0].title or "")

    async def fake_403(session, game, region, *, validator=None, api_key=None):
        return None, {}, 403

    monkeypatch.setattr(module, "fetch_game_status", fake_403)
    sent_text.clear()
    await cog.gw_check.callback(cog, ctx, "cs2")  # type: ignore[arg-type]
    assert any("rejected the configured key" in m and "403" in m for m in sent_text)

    await cog.cog_unload()
