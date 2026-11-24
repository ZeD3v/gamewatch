"""GameWatch - per-game status and patch-note announcements for Red-DiscordBot."""

from __future__ import annotations

import asyncio
import io
import logging
import re
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Literal, Optional, Union

import aiohttp
import discord
from discord.ext import tasks
from redbot.core import Config, app_commands, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import box, humanize_list, pagify

from .constants import (
    DEFAULT_LOCATION_ALLOW,
    GAMES,
    RIOT_RETIRED_REGIONS,
    RIOT_SEVERITY_ORDER,
    RIOT_VALID_REGIONS,
    USER_AGENT,
)
from .embeds import (
    accent_int,
    embed_to_plain_text,
    fit_embed,
    incident_embed,
    news_embed,
    recovery_embed,
    status_panel_embed,
)
from .sources import (
    FetchError,
    extract_location,
    fetch_abi_news_detail,
    fetch_game_news,
    fetch_game_status,
    fetch_riot_status,
    filter_status_view,
    location_allowed,
)
from .types import CacheValidator, NewsItem, ParsedStatus

log = logging.getLogger("red.zed3v.gamewatch")

TargetChannel = Union[discord.TextChannel, discord.Thread]
LOCATION_ACTIONS = ("add", "remove", "clear", "reset")
_LOCATION_FILTER_KINDS = frozenset({"riot", "steam_cs2"})
_API_KEY_PORTALS: dict[str, tuple[str, str]] = {
    "steam": ("Steam Web API", "https://steamcommunity.com/dev/apikey"),
}


STATUS_INTERVAL = 90  # seconds
NEWS_INTERVAL = 900  # seconds
CONFIRM_POLLS = 2  # consecutive bad polls before announcing an incident
RECOVERY_MIN_SECONDS = 120  # suppress "recovered" for blips shorter than this
MAX_SEEN_PER_GAME = 80  # ring buffer of announced uids
MAX_LOCATIONS_PER_GAME = 50  # cap per-guild location allowlist entries
FAILURE_ALERT_AFTER = 12  # consecutive errors before the owner is DMed
FETCH_CONCURRENCY = 6  # max simultaneous upstream requests per loop pass


class GameWatch(commands.Cog):
    """Per-game server status and patch notes for LoL, TFT, VALORANT, CS2, EFT,
    ARC Raiders, Arena Breakout: Infinite, and ROB RUN.

    Status and news are posted to separate, per-game channels. Nothing is
    announced until a channel has been configured for that game.
    """

    __author__ = "TheD3v_"
    __version__ = "2.0.0"

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x47414D4557415443, force_registration=True)

        self.config.register_guild(
            games={},  # game -> {news_channel, status_channel, role, region, enabled}
            ping_severity="warning",
        )
        self.config.register_global(
            seen={},  # namespace:game -> [uid, ...]
            bootstrapped={},  # game -> bool (cold-start guard)
            last_status={},  # game-region -> {"ok": bool, "since": iso}
        )

        self.session: Optional[aiohttp.ClientSession] = None
        self._validators: dict[str, CacheValidator] = {}
        self._last_health: dict[str, dict[str, Any]] = {}
        self._pending: dict[str, int] = {}  # game-region -> consecutive active bad polls
        self._pending_scheduled: dict[str, int] = {}  # game-region -> consecutive scheduled polls
        # Guilds that actually received an active outage alert for game-region,
        # so guilds whose location filter suppressed the outage don't get a
        # confusing recovery notice when another guild had filtering disabled.
        self._notified_guilds: dict[str, set[int]] = {}
        # Last successful parse per status source, so a 304 (unchanged payload)
        # can still be fed through the debounce/recovery state machine.
        self._last_parsed: dict[str, ParsedStatus] = {}
        self._failures: dict[str, int] = {}  # source key -> consecutive errors
        self._alerted: set[str] = set()
        # One lock per pipeline, serialising each loop against a manual
        # [p]gamewatch forcecheck (unlocked, both could pass _is_seen before
        # either marks, announcing the same item twice). Deliberately NOT one
        # shared lock: news fetches use up to 30s timeouts, and a hung news
        # upstream must never stall the 90s status loop.
        self._status_lock = asyncio.Lock()
        self._news_lock = asyncio.Lock()

    def _get_session(self) -> aiohttp.ClientSession:
        """Return an open ClientSession, creating one lazily if needed."""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers={"User-Agent": USER_AGENT},
                connector=aiohttp.TCPConnector(ttl_dns_cache=300, limit=20),
            )
        return self.session

    async def cog_load(self) -> None:
        self._get_session()
        self.status_loop.start()
        self.news_loop.start()

    def format_help_for_context(self, ctx: commands.Context) -> str:
        base = super().format_help_for_context(ctx)
        return f"{base}\n\nAuthor: {self.__author__}\nVersion: {self.__version__}"

    async def cog_unload(self) -> None:
        self.status_loop.cancel()
        self.news_loop.cancel()
        for lock in (self._status_lock, self._news_lock):
            try:
                await asyncio.wait_for(lock.acquire(), timeout=5)
                lock.release()
            except asyncio.TimeoutError:
                log.debug("Loop still busy at unload; closing session anyway")
        if self.session and not self.session.closed:
            await self.session.close()

    async def red_get_data_for_user(self, *, user_id: int) -> dict[str, io.BytesIO]:
        """GameWatch stores no end user data."""
        return {}

    async def red_delete_data_for_user(
        self,
        *,
        requester: Literal["discord_deleted_user", "owner", "user", "user_strict"],
        user_id: int,
    ) -> None:
        """GameWatch stores no end user data."""
        return

    @commands.Cog.listener()
    async def on_red_api_tokens_update(
        self, service_name: str, api_tokens: Mapping[str, str]
    ) -> None:
        """Clear cached health/failure state when a required API key is updated."""
        service = service_name.lower()
        for game, cfg in GAMES.items():
            if cfg.get("requires_api_key") != service:
                continue
            prefix = f"status-{game}-"
            for store in (self._validators, self._failures, self._last_health):
                for skey in [k for k in store if k.startswith(prefix)]:
                    store.pop(skey, None)
            for skey in [k for k in self._alerted if k.startswith(prefix)]:
                self._alerted.discard(skey)

    async def _get_api_key(self, service: str) -> Optional[str]:
        """Read a bot-wide API key from Red's shared API tokens (`[p]set api`)."""
        getter = getattr(self.bot, "get_shared_api_tokens", None)
        if getter is None:
            return None
        tokens = await getter(service)
        if not isinstance(tokens, Mapping):
            return None
        raw = tokens.get("api_key") or tokens.get("key") or ""
        cleaned = str(raw).strip()
        return cleaned or None

    @staticmethod
    def _missing_api_key_message(game: str, prefix: str) -> str:
        cfg = GAMES[game]
        service = cfg.get("requires_api_key") or "steam"
        label, portal = _API_KEY_PORTALS.get(service, (service.title(), ""))
        portal_note = f" (free at <{portal}>)" if portal else ""
        return (
            f"{cfg['name']} status monitoring requires a {label} key{portal_note}.\n"
            f"The bot owner can set one in DM via `{prefix}set api {service} api_key,<your_key>` "
            f"(or `{prefix}set api {service}` for a modal). Patch notes work without an API key."
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    async def _guild_game_cfg(self, guild: discord.Guild, game: str) -> dict:
        games = await self.config.guild(guild).games()
        return games.get(game) or {}

    async def _targets(
        self, game: str, field: str, all_guilds: Optional[dict] = None
    ) -> list[tuple[discord.Guild, dict]]:
        """All (guild, cfg) pairs that have a channel configured for this game."""
        if all_guilds is None:
            all_guilds = await self.config.all_guilds()
        out = []
        for guild in self.bot.guilds:
            data = all_guilds.get(guild.id) or {}
            cfg = (data.get("games") or {}).get(game) or {}
            if not cfg.get("enabled", True):
                continue
            if cfg.get(field):
                out.append((guild, cfg))
        return out

    @staticmethod
    def _seen_key(namespace: str, game: str) -> str:
        return f"{namespace}:{game}"

    def _effective_allowlist(self, gcfg: dict, game: str) -> list[str]:
        """Guild's location allowlist for a game.

        None (unset) => the researched game default; [] => filter disabled;
        a list => custom. Stored lowercase; matching is substring, casefolded.
        """
        val = gcfg.get("location_allow")
        if val is None:
            return DEFAULT_LOCATION_ALLOW.get(game, [])
        return val

    async def _mark_seen(self, namespace: str, game: str, uid: str) -> None:
        key = self._seen_key(namespace, game)
        async with self.config.seen() as seen:
            if game in seen and self._seen_key("news", game) not in seen:
                seen[self._seen_key("news", game)] = seen.pop(game)
            entries = seen.setdefault(key, [])
            entries.append(uid)
            if len(entries) > MAX_SEEN_PER_GAME:
                del entries[:-MAX_SEEN_PER_GAME]

    async def _is_seen(self, namespace: str, game: str, uid: str) -> bool:
        seen = await self.config.seen()
        legacy = seen.get(game) or []
        return uid in (seen.get(self._seen_key(namespace, game)) or []) or (
            namespace == "news" and uid in legacy
        )

    async def _migrate_seen_if_needed(self, game: str, items: list[NewsItem]) -> bool:
        """One-time upgrade guard when a game's news source/UID namespace changes.

        Returns True ONLY when silent re-seeding occurred and the caller must
        skip announcing on this poll.
        """
        key = self._seen_key("news", game)
        prefix = GAMES[game]["uid_prefix"]

        async with self.config.seen() as seen:
            # 1. Fold any legacy un-namespaced seen[game] ring into seen["news:{game}"].
            if game in seen:
                legacy = seen.pop(game) or []
                existing = seen.get(key) or []
                merged = list(dict.fromkeys([*legacy, *existing]))
                seen[key] = merged[-MAX_SEEN_PER_GAME:]

            # 2. Empty feed poll: never clear existing UIDs or trigger migration yet.
            if not items:
                return False

            entries = seen.get(key) or []

            # 3. LoL en-gb -> en-us in-place rewrite preserves exact seen history
            #    so brand-new patches still announce on the very same poll.
            if game == "lol" and any("://www.leagueoflegends.com/en-gb/" in u for u in entries):
                rewritten = list(
                    dict.fromkeys(
                        u.replace(
                            "://www.leagueoflegends.com/en-gb/",
                            "://www.leagueoflegends.com/en-us/",
                        )
                        for u in entries
                    )
                )
                seen[key] = rewritten[-MAX_SEEN_PER_GAME:]
                log.info("GameWatch migrated %s seen ring from en-gb to en-us", game)
                return False

            # 4. Disjoint namespace upgrade (e.g. steam-* -> steam-ev-*/eft-bsg-*/abi-cms-*)
            #    or empty ring while already bootstrapped: silently re-seed once.
            if not any(u.startswith(prefix) for u in entries):
                seeded = [i["uid"] for i in reversed(items)]
                seen[key] = seeded[-MAX_SEEN_PER_GAME:]
                log.info(
                    "GameWatch migrated %s seen ring to %s namespace (%s items)",
                    game,
                    prefix,
                    len(items),
                )
                return True

        return False

    async def _note_failure(self, key: str, detail: str | Exception) -> None:
        self._failures[key] = self._failures.get(key, 0) + 1
        count = self._failures[key]
        log.warning("GameWatch fetch failed for %s (streak %s): %s", key, count, detail)
        if count >= FAILURE_ALERT_AFTER and key not in self._alerted:
            self._alerted.add(key)
            try:
                await self.bot.send_to_owners(
                    f"\u26a0\ufe0f GameWatch: `{key}` has failed {count} consecutive times. "
                    "The upstream may have changed. Check `[p]gamewatch diag`."
                )
            except Exception:
                log.exception("Could not notify owner about %s", key)

    def _note_success(self, key: str) -> None:
        if self._failures.get(key):
            self._failures[key] = 0
        self._alerted.discard(key)

    @staticmethod
    def _resolve_channel(
        guild: discord.Guild, channel_id: Optional[int]
    ) -> Optional[TargetChannel]:
        """Resolve a configured text/announcement channel or thread."""
        if not channel_id:
            return None
        getter = getattr(guild, "get_channel_or_thread", None) or guild.get_channel
        channel = getter(channel_id)
        if channel is None:
            return None
        return channel

    async def _channel_wants_embed(
        self, channel: TargetChannel, perms: discord.Permissions
    ) -> bool:
        """Respect both Discord Embed Links permission and Red's [p]embedset."""
        if not perms.embed_links:
            return False
        embed_req = getattr(self.bot, "embed_requested", None)
        if embed_req is None:
            return True
        try:
            return await embed_req(channel)
        except TypeError:
            return True

    @staticmethod
    def _unknown_game_message() -> str:
        return f"Unknown game. Valid keys: {humanize_list([f'`{g}`' for g in GAMES])}"

    @staticmethod
    def _channel_permission_warning(channel: TargetChannel) -> str:
        me = getattr(channel.guild, "me", None)
        if me is None:
            return ""
        perms = channel.permissions_for(me)
        missing_send: list[str] = []
        if not perms.view_channel:
            missing_send.append("View Channel")
        if isinstance(channel, discord.Thread):
            if not perms.send_messages_in_threads:
                missing_send.append("Send Messages in Threads")
        elif not perms.send_messages:
            missing_send.append("Send Messages")
        notes: list[str] = []
        if missing_send:
            notes.append(
                f"\u26a0\ufe0f I am missing {humanize_list(missing_send)} in {channel.mention}, "
                "so announcements cannot be posted until those permissions are granted."
            )
        if not perms.embed_links:
            notes.append(
                f"\u2139\ufe0f I lack Embed Links in {channel.mention}; "
                "updates will fall back to plain text until it is granted."
            )
        return ("\n" + "\n".join(notes)) if notes else ""

    async def _send_ctx_embed(
        self,
        ctx: commands.Context,
        embed: discord.Embed,
        *,
        allowed_mentions: Optional[discord.AllowedMentions] = None,
    ) -> None:
        """Send an embed in a command response, honouring ctx.embed_requested()."""
        mentions = (
            allowed_mentions if allowed_mentions is not None else discord.AllowedMentions.none()
        )
        if await ctx.embed_requested():
            await ctx.send(embed=embed, allowed_mentions=mentions)
            return
        text = embed_to_plain_text(embed) or "No details available."
        for page in pagify(text, page_length=1990):
            await ctx.send(page, allowed_mentions=mentions)

    async def _send(
        self,
        channel: TargetChannel,
        embed: discord.Embed,
        content: Optional[str] = None,
        ping_role: Optional[int] = None,
    ) -> None:
        perms = channel.permissions_for(channel.guild.me)
        can_send = (
            perms.send_messages_in_threads
            if isinstance(channel, discord.Thread)
            else perms.send_messages
        )
        if not (perms.view_channel and can_send):
            log.warning(
                "Missing view/send permission in #%s (%s)", channel.name, channel.guild.name
            )
            return
        role_objs = []
        if ping_role:
            role = channel.guild.get_role(ping_role)
            if role:
                role_objs = [role]
        allowed = discord.AllowedMentions(
            everyone=False, users=False, roles=role_objs, replied_user=False
        )
        use_embed = await self._channel_wants_embed(channel, perms)
        try:
            if use_embed:
                await channel.send(content=content, embed=embed, allowed_mentions=allowed)
            else:
                text = embed_to_plain_text(embed)
                full = f"{content}\n{text}" if content else text
                for page in pagify(full.strip(), page_length=1990):
                    await channel.send(content=page, allowed_mentions=allowed)
        except discord.HTTPException as exc:
            log.exception("Failed sending to #%s (%s)", channel.name, exc)
            if getattr(exc, "code", None) == 50035:
                try:
                    await channel.send(
                        content=f"**{embed.title}**\n{embed.url or ''}".strip()[:2000],
                        allowed_mentions=allowed,
                    )
                except discord.HTTPException:
                    log.exception("Plain-text fallback also failed for #%s", channel.name)

    # ------------------------------------------------------------------
    # status loop (2-phase: bounded concurrent fetch -> sequential state)
    # ------------------------------------------------------------------
    @tasks.loop(seconds=STATUS_INTERVAL)
    async def status_loop(self) -> None:
        try:
            await self._check_status_once()
        except Exception:
            log.exception("Unhandled error in status loop")

    @status_loop.before_loop
    async def _status_before(self) -> None:
        await self.bot.wait_until_red_ready()

    async def _check_status_once(self) -> None:
        async with self._status_lock:
            await self._check_status_inner()

    async def _check_status_inner(self) -> None:
        all_guilds = await self.config.all_guilds()
        api_keys: dict[str, Optional[str]] = {}
        jobs: list[tuple[str, str, list[tuple[discord.Guild, dict]], Optional[str]]] = []
        for game, cfg in GAMES.items():
            if cfg["status_kind"] is None:
                continue
            targets = await self._targets(game, "status_channel", all_guilds)
            if not targets:
                continue
            req_service = cfg.get("requires_api_key")
            api_key: Optional[str] = None
            if req_service:
                if req_service not in api_keys:
                    api_keys[req_service] = await self._get_api_key(req_service)
                api_key = api_keys[req_service]
                if not api_key:
                    log.debug(
                        "Skipping %s status poll: no %s API key configured", game, req_service
                    )
                    continue
            regions = sorted({(gcfg.get("region") or cfg["default_region"]) for _, gcfg in targets})
            for region in regions:
                jobs.append((game, region, targets, api_key))

        if not jobs:
            return

        session = self._get_session()
        sem = asyncio.Semaphore(FETCH_CONCURRENCY)

        async def _fetch_one(
            game: str, region: str, api_key: Optional[str]
        ) -> tuple[Optional[ParsedStatus], CacheValidator, int, int]:
            key = f"status-{game}-{region}"
            validator = dict(self._validators.get(key) or {})
            async with sem:
                t0 = time.monotonic()
                kwargs: dict[str, Any] = {"validator": validator or None}
                if api_key is not None:
                    kwargs["api_key"] = api_key
                parsed, new_val, http = await fetch_game_status(session, game, region, **kwargs)
                ms = int((time.monotonic() - t0) * 1000)
                return parsed, new_val, http, ms

        results = await asyncio.gather(
            *(_fetch_one(game, region, api_key) for game, region, _, api_key in jobs),
            return_exceptions=True,
        )
        # Only propagate fatal non-Exception BaseExceptions (e.g. CancelledError);
        # isolate ordinary Exceptions per source in Phase 2 so one broken upstream
        # never aborts healthy games or skips failure-streak accounting.
        for res in results:
            if isinstance(res, BaseException) and not isinstance(res, Exception):
                raise res

        # Phase 2: deterministic sequential state updates and announcements.
        for (game, region, targets, _), res in zip(jobs, results, strict=True):
            key = f"status-{game}-{region}"
            if isinstance(res, Exception):
                if not isinstance(res, FetchError):
                    log.exception("Unexpected error fetching %s", key, exc_info=res)
                self._last_health[key] = {"http": 0, "ms": 0}
                await self._note_failure(key, res)
                continue

            parsed, new_val, http, ms = res
            self._last_health[key] = {"http": http, "ms": ms}
            if http == 304:
                self._note_success(key)
                if new_val:
                    self._validators[key] = new_val
                cached = self._last_parsed.get(key)
                if cached is not None:
                    await self._process_status(game, region, cached, targets)
                continue
            if http != 200 or parsed is None:
                req_service = GAMES[game].get("requires_api_key")
                detail = (
                    f"HTTP {http} (invalid or revoked {req_service} API key?)"
                    if req_service and http in (401, 403)
                    else f"HTTP {http}"
                )
                await self._note_failure(key, detail)
                continue

            self._note_success(key)
            if new_val:
                self._validators[key] = new_val
            self._last_parsed[key] = parsed
            await self._process_status(game, region, parsed, targets)

    async def _process_status(
        self,
        game: str,
        region: str,
        parsed: ParsedStatus,
        targets: list[tuple[discord.Guild, dict]],
    ) -> None:
        state_key = f"{game}-{region}"

        # Location filter: union semantics across guilds watching this region.
        cfg = GAMES[game]
        region_targets = [
            (g, gc)
            for g, gc in targets
            if (gc.get("region") or cfg.get("default_region")) == region
        ]
        if region_targets:
            allowlists = [self._effective_allowlist(gc, game) for _, gc in region_targets]
            if not any(not a for a in allowlists):
                union: list[str] = sorted({loc for a in allowlists for loc in a})
                parsed = filter_status_view(parsed, union)
                for item in parsed.get("filtered_items") or []:
                    region_uid = f"{region}:{item['uid']}"
                    if not await self._is_seen("status", game, region_uid):
                        await self._mark_seen("status", game, region_uid)
                if parsed.get("filtered_count"):
                    log.debug(
                        "%s/%s: %s incident(s) location-filtered",
                        game,
                        region,
                        parsed["filtered_count"],
                    )

        all_items = parsed.get("items") or []
        scheduled_items = [i for i in all_items if i.get("maintenance_status") == "scheduled"]
        active_items = [i for i in all_items if i.get("maintenance_status") != "scheduled"]

        # Step A: Independent scheduled-maintenance debounce (never mutates last_status
        # or triggers bogus "Services back to normal" recoveries).
        if scheduled_items:
            self._pending_scheduled[state_key] = self._pending_scheduled.get(state_key, 0) + 1
            if self._pending_scheduled[state_key] >= CONFIRM_POLLS:
                for item in scheduled_items:
                    region_uid = f"{region}:{item['uid']}"
                    if await self._is_seen("status", game, region_uid):
                        continue
                    await self._announce_incident(game, region, parsed, item, targets)
                    await self._mark_seen("status", game, region_uid)
        else:
            self._pending_scheduled.pop(state_key, None)

        # Step B: Active service health, incident debounce, and outage recovery.
        last = (await self.config.last_status()).get(state_key) or {}
        was_ok = last.get("ok", True)
        now = datetime.now(timezone.utc)

        if parsed["ok"]:
            self._pending.pop(state_key, None)
            if not was_ok:
                since = last.get("since")
                long_enough = True
                if since:
                    try:
                        started = datetime.fromisoformat(since)
                        long_enough = (now - started).total_seconds() >= RECOVERY_MIN_SECONDS
                    except ValueError:
                        pass
                if long_enough:
                    await self._announce_recovery(game, region, parsed, targets)
                async with self.config.last_status() as store:
                    store[state_key] = {"ok": True, "since": now.isoformat()}
            return

        self._pending[state_key] = self._pending.get(state_key, 0) + 1
        if self._pending[state_key] < CONFIRM_POLLS:
            log.debug("Status for %s degraded, awaiting confirmation", state_key)
            return

        for item in active_items:
            region_uid = f"{region}:{item['uid']}"
            if await self._is_seen("status", game, region_uid):
                continue
            await self._announce_incident(game, region, parsed, item, targets)
            await self._mark_seen("status", game, region_uid)

        async with self.config.last_status() as store:
            if was_ok or not store.get(state_key):
                store[state_key] = {"ok": False, "since": now.isoformat()}
            else:
                store[state_key]["ok"] = False

    async def _announce_incident(
        self, game: str, region: str, parsed: dict, item: dict, targets: list
    ) -> None:
        cfg = GAMES[game]
        state_key = f"{game}-{region}"
        is_active = item.get("maintenance_status") != "scheduled"
        severity = (item.get("severity") or "info").lower()
        embed = incident_embed(game, region, parsed, item)

        for guild, gcfg in targets:
            if (gcfg.get("region") or cfg.get("default_region")) != region:
                continue
            if not location_allowed(item, self._effective_allowlist(gcfg, game)):
                continue
            channel = self._resolve_channel(guild, gcfg.get("status_channel"))
            if not channel:
                log.warning(
                    "Configured status channel %s for %s in guild %s no longer "
                    "resolves (deleted?); skipping",
                    gcfg.get("status_channel"),
                    game,
                    guild.id,
                )
                continue
            threshold = await self.config.guild(guild).ping_severity()
            content = None
            role_id = None
            if gcfg.get("role") and RIOT_SEVERITY_ORDER.get(severity, 0) >= RIOT_SEVERITY_ORDER.get(
                threshold, 1
            ):
                role_id = gcfg["role"]
                content = f"<@&{role_id}>"
            await self._send(channel, embed, content, ping_role=role_id)
            if is_active:
                self._notified_guilds.setdefault(state_key, set()).add(guild.id)

    async def _announce_recovery(self, game: str, region: str, parsed: dict, targets: list) -> None:
        cfg = GAMES[game]
        state_key = f"{game}-{region}"
        notified = self._notified_guilds.pop(state_key, None)
        down_since = None
        stored = (await self.config.last_status()).get(state_key) or {}
        if stored.get("since"):
            try:
                down_since = datetime.fromisoformat(stored["since"])
            except ValueError:
                down_since = None
        embed = recovery_embed(game, region, parsed, down_since)
        for guild, gcfg in targets:
            if (gcfg.get("region") or cfg.get("default_region")) != region:
                continue
            if notified is not None and guild.id not in notified:
                continue
            channel = self._resolve_channel(guild, gcfg.get("status_channel"))
            if channel:
                await self._send(channel, embed)

    # ------------------------------------------------------------------
    # news loop (2-phase: bounded concurrent fetch -> sequential state)
    # ------------------------------------------------------------------
    @tasks.loop(seconds=NEWS_INTERVAL)
    async def news_loop(self) -> None:
        try:
            await self._check_news_once()
        except Exception:
            log.exception("Unhandled error in news loop")

    @news_loop.before_loop
    async def _news_before(self) -> None:
        await self.bot.wait_until_red_ready()
        await asyncio.sleep(10)

    async def _check_news_once(self) -> None:
        async with self._news_lock:
            await self._check_news_inner()

    async def _bootstrap_seen(self, game: str, items: list[NewsItem]) -> None:
        """Cold start: record every visible item WITHOUT announcing (oldest first)."""
        key = self._seen_key("news", game)
        async with self.config.seen() as seen:
            entries = seen.setdefault(key, [])
            entries.extend(item["uid"] for item in reversed(items))
            if len(entries) > MAX_SEEN_PER_GAME:
                del entries[:-MAX_SEEN_PER_GAME]
        async with self.config.bootstrapped() as store:
            store[game] = True
        log.info("GameWatch bootstrapped %s with %s existing items", game, len(items))

    async def _check_news_inner(self) -> None:
        bootstrapped = await self.config.bootstrapped()
        all_guilds = await self.config.all_guilds()
        jobs: list[tuple[str, list[tuple[discord.Guild, dict]]]] = []
        for game in GAMES:
            targets = await self._targets(game, "news_channel", all_guilds)
            if targets:
                jobs.append((game, targets))

        if not jobs:
            return

        session = self._get_session()
        sem = asyncio.Semaphore(FETCH_CONCURRENCY)

        async def _fetch_one(
            game: str,
        ) -> tuple[Optional[list[NewsItem]], CacheValidator, int, int]:
            key = f"news-{game}"
            validator = dict(self._validators.get(key) or {})
            async with sem:
                t0 = time.monotonic()
                items, new_val, http = await fetch_game_news(
                    session, game, validator=validator or None
                )
                ms = int((time.monotonic() - t0) * 1000)
                return items, new_val, http, ms

        results = await asyncio.gather(
            *(_fetch_one(game) for game, _ in jobs),
            return_exceptions=True,
        )
        for res in results:
            if isinstance(res, BaseException) and not isinstance(res, Exception):
                raise res

        # Phase 2: deterministic sequential state updates and announcements.
        for (game, targets), res in zip(jobs, results, strict=True):
            key = f"news-{game}"
            cfg = GAMES[game]
            if isinstance(res, Exception):
                if not isinstance(res, FetchError):
                    log.exception("Unexpected error fetching %s", key, exc_info=res)
                self._last_health[key] = {"http": 0, "ms": 0}
                await self._note_failure(key, res)
                continue

            items, new_val, http, ms = res
            self._last_health[key] = {"http": http, "ms": ms}
            if http == 304:
                self._note_success(key)
                if new_val:
                    self._validators[key] = new_val
                continue
            if http != 200 or items is None:
                await self._note_failure(key, f"HTTP {http}")
                continue

            self._note_success(key)
            if new_val:
                self._validators[key] = new_val

            if not bootstrapped.get(game):
                await self._bootstrap_seen(game, items)
                continue

            if await self._migrate_seen_if_needed(game, items):
                continue

            for item in reversed(items[:10]):
                if await self._is_seen("news", game, item["uid"]):
                    continue
                to_send: NewsItem = item
                if (
                    cfg["news_kind"] == "abi_cms"
                    and item.get("content_id")
                    and not item.get("raw_html")
                ):
                    detail_html = await fetch_abi_news_detail(session, item["content_id"])
                    if detail_html:
                        to_send = {**item, "raw_html": detail_html}
                await self._announce_news(game, to_send, targets)
                await self._mark_seen("news", game, item["uid"])

    async def _announce_news(
        self, game: str, item: NewsItem, targets: list[tuple[discord.Guild, dict]]
    ) -> None:
        embed = news_embed(game, item, accent=accent_int(item.get("accent")))
        for guild, gcfg in targets:
            channel = self._resolve_channel(guild, gcfg.get("news_channel"))
            if not channel:
                log.warning(
                    "Configured news channel %s for %s in guild %s no longer "
                    "resolves (deleted?); skipping",
                    gcfg.get("news_channel"),
                    game,
                    guild.id,
                )
                continue
            role_id = gcfg.get("role")
            content = f"<@&{role_id}>" if role_id else None
            await self._send(channel, embed, content, ping_role=role_id)

    # ------------------------------------------------------------------
    # autocomplete helpers (slash commands)
    # ------------------------------------------------------------------
    @staticmethod
    def _matching_game_choices(
        current: str,
        *,
        status_only: bool = False,
        riot_only: bool = False,
        location_only: bool = False,
    ) -> list[app_commands.Choice[str]]:
        needle = current.lower().strip()
        choices: list[app_commands.Choice[str]] = []
        for key, cfg in GAMES.items():
            if status_only and cfg["status_kind"] is None:
                continue
            if riot_only and cfg["status_kind"] != "riot":
                continue
            if location_only and cfg["status_kind"] not in _LOCATION_FILTER_KINDS:
                continue
            label = f"{cfg['name']} ({key})"
            if not needle or needle in key or needle in cfg["name"].lower():
                choices.append(app_commands.Choice(name=label, value=key))
        return choices[:25]

    async def _game_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return self._matching_game_choices(current)

    async def _status_game_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return self._matching_game_choices(current, status_only=True)

    async def _riot_game_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return self._matching_game_choices(current, riot_only=True)

    async def _location_game_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return self._matching_game_choices(current, location_only=True)

    async def _region_autocomplete(
        self, interaction: Optional[discord.Interaction], current: str
    ) -> list[app_commands.Choice[str]]:
        raw_game = (
            (getattr(getattr(interaction, "namespace", None), "game", None) or "").lower().strip()
        )
        if raw_game in RIOT_VALID_REGIONS:
            pool = RIOT_VALID_REGIONS[raw_game]
        else:
            pool = tuple(sorted({r for regions in RIOT_VALID_REGIONS.values() for r in regions}))
        needle = current.lower().strip()
        return [
            app_commands.Choice(name=reg, value=reg) for reg in pool if not needle or needle in reg
        ][:25]

    async def _severity_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        needle = current.lower().strip()
        return [
            app_commands.Choice(name=level, value=level)
            for level in RIOT_SEVERITY_ORDER
            if not needle or needle in level
        ]

    async def _location_action_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        needle = current.lower().strip()
        return [
            app_commands.Choice(name=act, value=act)
            for act in LOCATION_ACTIONS
            if not needle or needle in act
        ]

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------
    @commands.hybrid_group(name="gamewatch", aliases=["gw"])
    @commands.guild_only()
    async def gamewatch(self, ctx: commands.Context) -> None:
        """Game status and patch note announcements."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help(ctx.command)

    @gamewatch.command(name="games")
    async def gw_games(self, ctx: commands.Context) -> None:
        """List supported games and their keys."""
        key_cache: dict[str, bool] = {}
        tracked, news_only = [], []
        for key, cfg in GAMES.items():
            line = f"{cfg['emoji']} **{cfg['name']}** \u2014 `{key}`"
            req_service = cfg.get("requires_api_key")
            if cfg["status_kind"] is None:
                news_only.append(line)
            elif req_service:
                if req_service not in key_cache:
                    key_cache[req_service] = bool(await self._get_api_key(req_service))
                if key_cache[req_service]:
                    tracked.append(line)
                else:
                    label = _API_KEY_PORTALS.get(req_service, (req_service.title(), ""))[0]
                    news_only.append(f"{line} *(status with {label} key)*")
            else:
                tracked.append(line)
        embed = discord.Embed(
            title="Supported games",
            description=(
                "Use the key in every other command, e.g. "
                f"`{ctx.clean_prefix}gamewatch news lol #lol-patches`."
            ),
            colour=await ctx.embed_colour(),
        )
        if tracked:
            embed.add_field(
                name="\U0001f4e1 Status + patch notes",
                value="\n".join(tracked),
                inline=False,
            )
        if news_only:
            embed.add_field(
                name="\U0001f4f0 Patch notes only",
                value="\n".join(news_only),
                inline=False,
            )
        embed.set_footer(
            text=(
                "CS2 status uses an optional Steam Web API key ([p]set api steam). "
                "ARC Raiders, Arena Breakout: Infinite, and ROB RUN have no public status API."
            )
        )
        await self._send_ctx_embed(ctx, fit_embed(embed))

    @gamewatch.command(name="news")
    @commands.admin_or_permissions(manage_guild=True)
    @app_commands.describe(
        game="Game key (e.g. lol, cs2, valorant, robrun)",
        channel="Channel or thread for patch notes (omit to disable)",
    )
    @app_commands.autocomplete(game=_game_autocomplete)
    async def gw_news(
        self, ctx: commands.Context, game: str, channel: Optional[TargetChannel] = None
    ) -> None:
        """Set the patch-notes channel for a game. Omit the channel to clear it."""
        game = game.lower()
        if game not in GAMES:
            return await ctx.send(self._unknown_game_message())
        async with self.config.guild(ctx.guild).games() as games:
            entry = games.setdefault(game, {})
            entry["news_channel"] = channel.id if channel else None
            entry.setdefault("enabled", True)
        if channel:
            warn = self._channel_permission_warning(channel)
            await ctx.send(
                f"{GAMES[game]['name']} patch notes will post to {channel.mention}.{warn}"
            )
        else:
            await ctx.send(f"{GAMES[game]['name']} patch notes disabled.")

    @gamewatch.command(name="status")
    @commands.admin_or_permissions(manage_guild=True)
    @app_commands.describe(
        game="Game key with status monitoring (lol, tft, valorant, eft, cs2)",
        channel="Channel or thread for status alerts (omit to disable)",
    )
    @app_commands.autocomplete(game=_status_game_autocomplete)
    async def gw_status(
        self, ctx: commands.Context, game: str, channel: Optional[TargetChannel] = None
    ) -> None:
        """Set the status/incident channel for a game. Omit the channel to clear it."""
        game = game.lower()
        if game not in GAMES:
            return await ctx.send(self._unknown_game_message())
        cfg = GAMES[game]
        if cfg["status_kind"] is None:
            return await ctx.send(
                f"{cfg['name']} has no public status API, so status monitoring "
                "is not available for it. Patch notes still work."
            )
        req_service = cfg.get("requires_api_key")
        if channel is not None and req_service and not await self._get_api_key(req_service):
            return await ctx.send(self._missing_api_key_message(game, ctx.clean_prefix))
        async with self.config.guild(ctx.guild).games() as games:
            entry = games.setdefault(game, {})
            entry["status_channel"] = channel.id if channel else None
            entry.setdefault("region", cfg["default_region"])
            entry.setdefault("enabled", True)
            active_region = entry["region"]
        if channel:
            warn = self._channel_permission_warning(channel)
            region_hint = ""
            if cfg["status_kind"] == "riot":
                region_hint = (
                    f" (region: `{active_region}` \u2014 change with "
                    f"`{ctx.clean_prefix}gamewatch region {game} <region>`)"
                )
            await ctx.send(
                f"{cfg['name']} status alerts will post to {channel.mention}{region_hint}.{warn}"
            )
        else:
            await ctx.send(f"{cfg['name']} status alerts disabled.")

    @gamewatch.command(name="role")
    @commands.admin_or_permissions(manage_guild=True)
    @app_commands.describe(
        game="Game key (e.g. lol, cs2, valorant, robrun)",
        role="Role to ping on announcements (omit to clear)",
    )
    @app_commands.autocomplete(game=_game_autocomplete)
    async def gw_role(
        self, ctx: commands.Context, game: str, role: Optional[discord.Role] = None
    ) -> None:
        """Set the role pinged for a game. Omit the role to clear it."""
        game = game.lower()
        if game not in GAMES:
            return await ctx.send(self._unknown_game_message())
        if role is not None and role.is_default():
            return await ctx.send(
                "Cannot use `@everyone` as a GameWatch ping role. Choose a dedicated role instead."
            )
        async with self.config.guild(ctx.guild).games() as games:
            entry = games.setdefault(game, {})
            entry["role"] = role.id if role else None
        warn = ""
        if role is not None and not role.mentionable:
            me = getattr(ctx.guild, "me", None)
            guild_perms = getattr(me, "guild_permissions", None)
            if guild_perms is not None and not guild_perms.mention_everyone:
                warn = (
                    "\n\u26a0\ufe0f Note: that role is not mentionable and I lack the "
                    "Mention Everyone permission, so pings will not notify members until "
                    "one of those is enabled."
                )
        await ctx.send(
            f"{GAMES[game]['name']} pings {role.mention if role else 'nobody'}.{warn}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @gamewatch.command(name="region")
    @commands.admin_or_permissions(manage_guild=True)
    @app_commands.describe(
        game="Riot game key (lol, tft, valorant)",
        region="Riot region ID (e.g. eun1, euw1, na1 for LoL/TFT; eu, na, ap for VALORANT)",
    )
    @app_commands.autocomplete(game=_riot_game_autocomplete, region=_region_autocomplete)
    async def gw_region(self, ctx: commands.Context, game: str, region: str) -> None:
        """Set the region for a Riot game.

        LoL and TFT use platform IDs (note: LoL uses kr1/ru1/pbe; TFT uses
        kr/ru/pbe1). VALORANT uses deployments (eu, na, ap, br, kr, latam).
        """
        game = game.lower()
        if game not in GAMES or GAMES[game]["status_kind"] != "riot":
            return await ctx.send("That game has no configurable region.")
        region = region.lower()
        if not re.fullmatch(r"[a-z0-9]{1,16}", region):
            return await ctx.send("Regions are short alphanumeric IDs like `eun1` or `eu`.")
        if region in RIOT_RETIRED_REGIONS and RIOT_RETIRED_REGIONS[
            region
        ] in RIOT_VALID_REGIONS.get(game, ()):
            replacement = RIOT_RETIRED_REGIONS[region]
            return await ctx.send(
                f"Region `{region}` has been retired by Riot and merged into `{replacement}`. "
                f"Use `{replacement}` instead."
            )
        async with ctx.typing():
            try:
                parsed, _, http = await fetch_riot_status(self._get_session(), game, region)
            except FetchError as exc:
                return await ctx.send(f"Could not reach Riot: {exc}")
        if http != 200 or parsed is None:
            valid = ", ".join(f"`{r}`" for r in RIOT_VALID_REGIONS.get(game, ()))
            return await ctx.send(
                f"Riot returned HTTP {http} for region `{region}`. Valid regions for "
                f"{GAMES[game]['name']}: {valid}."
            )
        async with self.config.guild(ctx.guild).games() as games:
            games.setdefault(game, {})["region"] = region
        await ctx.send(
            f"{GAMES[game]['name']} region set to `{region}` ({parsed.get('region_name')})."
        )

    @gamewatch.command(name="locations")
    @commands.admin_or_permissions(manage_guild=True)
    @app_commands.describe(
        game="Game key supporting location filtering (lol, tft, valorant, cs2)",
        action="Action: add, remove, clear, or reset (omit to view)",
        location="Server/datacenter location(s), comma-separated (e.g. warsaw, eu north)",
    )
    @app_commands.autocomplete(
        game=_location_game_autocomplete, action=_location_action_autocomplete
    )
    async def gw_locations(
        self,
        ctx: commands.Context,
        game: str,
        action: Optional[str] = None,
        *,
        location: Optional[str] = None,
    ) -> None:
        """Manage the status location allowlist for a game."""
        game = game.lower()
        if game not in GAMES or GAMES[game]["status_kind"] not in _LOCATION_FILTER_KINDS:
            return await ctx.send(
                "Location filtering applies to Riot status games (`lol`, `tft`, `valorant`) "
                "and Counter-Strike 2 (`cs2`)."
            )
        entry = await self._guild_game_cfg(ctx.guild, game)
        current = self._effective_allowlist(entry, game)
        default = DEFAULT_LOCATION_ALLOW.get(game, [])
        name = GAMES[game]["name"]

        if action is None:
            state = (
                "disabled - all locations announce"
                if not current
                else humanize_list([f"`{a}`" for a in current])
            )
            src = "custom" if entry.get("location_allow") is not None else "default"
            dflt = (
                humanize_list([f"`{a}`" for a in default])
                if default
                else "none (all locations announce)"
            )
            return await ctx.send(f"{name} allowlist ({src}): {state}\nDefault: {dflt}")

        action = action.lower()
        if action in ("add", "remove"):
            if not location:
                return await ctx.send(
                    f"Usage: `{ctx.clean_prefix}gamewatch locations {game} {action} <location>`"
                )
            locs = [part.strip() for part in location.lower().split(",") if part.strip()]
            if not locs or any(not re.fullmatch(r"[a-z0-9 .\-]{1,40}", loc) for loc in locs):
                return await ctx.send(
                    "Locations are short plain-text names like `warsaw` "
                    "(separate multiple with commas)."
                )
            new_set = set(current)
            if action == "add":
                new_set.update(locs)
                if len(new_set) > MAX_LOCATIONS_PER_GAME:
                    return await ctx.send(
                        f"Allowlist cannot exceed {MAX_LOCATIONS_PER_GAME} locations."
                    )
            else:
                missing = [loc for loc in locs if loc not in new_set]
                if len(missing) == len(locs):
                    return await ctx.send(
                        f"{humanize_list([f'`{m}`' for m in missing])} not on the list."
                    )
                new_set.difference_update(locs)
            updated = sorted(new_set)
            async with self.config.guild(ctx.guild).games() as games:
                games.setdefault(game, {})["location_allow"] = updated
            shown = (
                humanize_list([f"`{a}`" for a in updated]) if updated else "empty (filter disabled)"
            )
            return await ctx.send(f"{name} allowlist is now: {shown}")
        if action == "clear":
            async with self.config.guild(ctx.guild).games() as games:
                games.setdefault(game, {})["location_allow"] = []
            return await ctx.send(f"{name} location filtering disabled; every location announces.")
        if action == "reset":
            async with self.config.guild(ctx.guild).games() as games:
                games.setdefault(game, {}).pop("location_allow", None)
            dflt = (
                humanize_list([f"`{a}`" for a in default])
                if default
                else "none (all locations announce)"
            )
            return await ctx.send(f"{name} allowlist reset to default: {dflt}")
        await ctx.send("Actions: `add`, `remove`, `clear`, `reset`, or no action to show.")

    @gamewatch.command(name="pingseverity")
    @commands.admin_or_permissions(manage_guild=True)
    @app_commands.describe(level="Minimum incident severity that pings the configured role")
    @app_commands.autocomplete(level=_severity_autocomplete)
    async def gw_pingseverity(self, ctx: commands.Context, level: str) -> None:
        """Minimum incident severity that triggers a role ping: info, warning or critical."""
        level = level.lower()
        if level not in RIOT_SEVERITY_ORDER:
            return await ctx.send("Choose one of: `info`, `warning`, `critical`.")
        await self.config.guild(ctx.guild).ping_severity.set(level)
        await ctx.send(f"Role pings now fire at `{level}` and above.")

    @gamewatch.command(name="settings")
    async def gw_settings(self, ctx: commands.Context) -> None:
        """Show the current configuration for this server."""
        games = await self.config.guild(ctx.guild).games()
        severity = await self.config.guild(ctx.guild).ping_severity()
        key_cache: dict[str, bool] = {}
        lines = []
        for key, cfg in GAMES.items():
            entry = games.get(key) or {}
            news = f"<#{entry['news_channel']}>" if entry.get("news_channel") else "\u2014"
            status = f"<#{entry['status_channel']}>" if entry.get("status_channel") else "\u2014"
            req_service = cfg.get("requires_api_key")
            if entry.get("status_channel") and req_service:
                if req_service not in key_cache:
                    key_cache[req_service] = bool(await self._get_api_key(req_service))
                if not key_cache[req_service]:
                    status += f" *(paused: no `{req_service}` API key)*"
            role = f"<@&{entry['role']}>" if entry.get("role") else "\u2014"
            region = entry.get("region") or cfg.get("default_region") or "\u2014"
            extra = ""
            if cfg["status_kind"] in _LOCATION_FILTER_KINDS:
                allow = self._effective_allowlist(entry, key)
                extra = (
                    f"\n\u2003locations: {', '.join(allow)}" if allow else "\n\u2003locations: all"
                )
            lines.append(
                f"**{cfg['emoji']} {cfg['name']}** (`{key}`)\n"
                f"\u2003news: {news} \u2022 status: {status}\n"
                f"\u2003role: {role} \u2022 region: `{region}`" + extra
            )
        embed = discord.Embed(
            title="GameWatch settings",
            description="\n".join(lines),
            colour=await ctx.embed_colour(),
        )
        embed.set_footer(text=f"Ping severity threshold: {severity}")
        await self._send_ctx_embed(
            ctx, fit_embed(embed), allowed_mentions=discord.AllowedMentions.none()
        )

    @gamewatch.command(name="check")
    @commands.cooldown(1, 10, commands.BucketType.channel)
    @app_commands.describe(game="Game key with status monitoring (lol, tft, valorant, eft, cs2)")
    @app_commands.autocomplete(game=_status_game_autocomplete)
    async def gw_check(self, ctx: commands.Context, game: str) -> None:
        """Check a game's live status right now."""
        game = game.lower()
        if game not in GAMES:
            return await ctx.send(self._unknown_game_message())
        cfg = GAMES[game]
        if cfg["status_kind"] is None:
            return await ctx.send(
                f"{cfg['name']} publishes no public status API. Only patch notes are available."
            )
        req_service = cfg.get("requires_api_key")
        api_key: Optional[str] = None
        if req_service:
            api_key = await self._get_api_key(req_service)
            if not api_key:
                return await ctx.send(self._missing_api_key_message(game, ctx.clean_prefix))
        entry = await self._guild_game_cfg(ctx.guild, game)
        region = entry.get("region") or cfg["default_region"]
        async with ctx.typing():
            session = self._get_session()
            try:
                kwargs: dict[str, Any] = {}
                if api_key is not None:
                    kwargs["api_key"] = api_key
                parsed, _, http = await fetch_game_status(session, game, region, **kwargs)
            except FetchError as exc:
                return await ctx.send(f"Fetch failed: {exc}")
        if http != 200 or parsed is None:
            if req_service and http in (401, 403):
                label = _API_KEY_PORTALS.get(req_service, (req_service.title(), ""))[0]
                return await ctx.send(
                    f"{label} rejected the configured key (HTTP {http}). "
                    f"The bot owner should verify `{ctx.clean_prefix}set api {req_service}`."
                )
            return await ctx.send(f"Upstream returned HTTP {http}.")

        allow = self._effective_allowlist(entry, game)
        view = filter_status_view(parsed, allow)
        embed = status_panel_embed(game, region, view)
        dropped = view.get("filtered_items") or []
        if dropped:
            locs = ", ".join(sorted({extract_location(d) or "unknown" for d in dropped}))
            embed.add_field(
                name=f"Filtered ({len(dropped)})",
                value=f"Locations outside your allowlist: {locs}"[:1024],
                inline=False,
            )
            embed = fit_embed(embed)
        await self._send_ctx_embed(ctx, embed)

    @gamewatch.command(name="latest")
    @commands.cooldown(1, 10, commands.BucketType.channel)
    @app_commands.describe(game="Game key (e.g. lol, cs2, valorant, robrun)")
    @app_commands.autocomplete(game=_game_autocomplete)
    async def gw_latest(self, ctx: commands.Context, game: str) -> None:
        """Show the most recent patch notes for a game."""
        game = game.lower()
        if game not in GAMES:
            return await ctx.send(self._unknown_game_message())
        cfg = GAMES[game]
        async with ctx.typing():
            session = self._get_session()
            try:
                items, _, http = await fetch_game_news(session, game)
            except FetchError as exc:
                return await ctx.send(f"Fetch failed: {exc}")
            if http != 200 or not items:
                return await ctx.send("Nothing found.")
            latest_item: NewsItem = items[0]
            if (
                cfg["news_kind"] == "abi_cms"
                and latest_item.get("content_id")
                and not latest_item.get("raw_html")
            ):
                detail_html = await fetch_abi_news_detail(session, latest_item["content_id"])
                if detail_html:
                    latest_item = {**latest_item, "raw_html": detail_html}
        await self._send_ctx_embed(
            ctx, news_embed(game, latest_item, accent=accent_int(latest_item.get("accent")))
        )

    @gamewatch.command(name="diag")
    @commands.is_owner()
    async def gw_diag(self, ctx: commands.Context) -> None:
        """Show fetch health for every source."""
        all_guilds = await self.config.all_guilds()
        api_key_state: dict[str, bool] = {}
        for cfg in GAMES.values():
            req_service = cfg.get("requires_api_key")
            if req_service and req_service not in api_key_state:
                api_key_state[req_service] = bool(await self._get_api_key(req_service))
        lines = []
        for game, cfg in GAMES.items():
            nkey = f"news-{game}"
            nhealth = self._last_health.get(nkey)
            nstat = f" http={nhealth['http']} {nhealth['ms']}ms" if nhealth else ""
            lines.append(
                f"{nkey:16} kind={cfg['news_kind']:12} "
                f"failures={self._failures.get(nkey, 0)}{nstat}"
            )
            if cfg["status_kind"]:
                req_service = cfg.get("requires_api_key")
                regions = {
                    (gcfg.get("region") or cfg["default_region"])
                    for data in all_guilds.values()
                    for gcfg in [((data.get("games") or {}).get(game) or {})]
                    if gcfg.get("status_channel")
                }
                if not regions:
                    regions = {cfg["default_region"]}
                for region in sorted(regions):
                    skey = f"status-{game}-{region}"
                    if req_service and not api_key_state.get(req_service):
                        lines.append(f"{skey:20} skipped (no {req_service} api_key configured)")
                        continue
                    shealth = self._last_health.get(skey)
                    sstat = f" http={shealth['http']} {shealth['ms']}ms" if shealth else ""
                    lines.append(
                        f"{skey:20} failures={self._failures.get(skey, 0)} "
                        f"pending={self._pending.get(f'{game}-{region}', 0)} "
                        f"sched={self._pending_scheduled.get(f'{game}-{region}', 0)}{sstat}"
                    )
        seen = await self.config.seen()
        boot = await self.config.bootstrapped()
        lines.append("")
        if api_key_state:
            key_summary = ", ".join(
                f"{svc}={'configured' if ok else 'not set'}"
                for svc, ok in sorted(api_key_state.items())
            )
            lines.append(f"api keys: {key_summary}")
        lines.append(f"validators cached: {len(self._validators)}")
        lines.append(f"seen uids: {sum(len(v) for v in seen.values())}")
        lines.append(f"bootstrapped: {sorted(k for k, v in boot.items() if v)}")
        for page in pagify("\n".join(lines), page_length=1900):
            await ctx.send(box(page, lang="ini"))

    @gamewatch.command(name="forcecheck")
    @commands.is_owner()
    async def gw_forcecheck(self, ctx: commands.Context) -> None:
        """Run both loops once, immediately."""
        async with ctx.typing():
            await self._check_status_once()
            await self._check_news_once()
        await ctx.send("Status and news checks completed.")

    @gamewatch.command(name="resetseen")
    @commands.is_owner()
    @app_commands.describe(game="Game key whose dedupe history should be cleared")
    @app_commands.autocomplete(game=_game_autocomplete)
    async def gw_resetseen(self, ctx: commands.Context, game: str) -> None:
        """Clear dedupe history for a game and re-seed news silently on next poll."""
        game = game.lower()
        if game not in GAMES:
            return await ctx.send(self._unknown_game_message())
        async with self.config.seen() as seen:
            for key in (game, f"news:{game}", f"status:{game}"):
                seen.pop(key, None)
        async with self.config.bootstrapped() as boot:
            boot[game] = False
        prefix = f"{game}-"
        async with self.config.last_status() as store:
            for skey in [k for k in store if k.startswith(prefix)]:
                store.pop(skey, None)
        self._validators.pop(f"news-{game}", None)
        for skey in [k for k in self._validators if k.startswith(f"status-{game}-")]:
            self._validators.pop(skey, None)
        for skey in [k for k in self._last_parsed if k.startswith(f"status-{game}-")]:
            self._last_parsed.pop(skey, None)
        for mapping in (self._pending, self._pending_scheduled, self._notified_guilds):
            for skey in [k for k in mapping if k.startswith(prefix)]:
                mapping.pop(skey, None)
        await ctx.send(
            f"Dedupe history cleared for {GAMES[game]['name']}. "
            "Existing patch notes will be re-seeded silently on the next poll; "
            "only newer updates will announce."
        )
