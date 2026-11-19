"""GameWatch - per-game status and patch-note announcements for Red-DiscordBot."""

from __future__ import annotations

import asyncio
import io
import logging
import re
from datetime import datetime, timezone
from typing import Literal, Optional, Union

import aiohttp
import discord
from discord.ext import tasks
from redbot.core import Config, app_commands, commands
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import box, humanize_list, pagify

from .constants import DEFAULT_LOCATION_ALLOW, GAMES, RIOT_SEVERITY_ORDER, USER_AGENT
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
    fetch_eft_status,
    fetch_riot_news,
    fetch_riot_status,
    fetch_steam_news,
    filter_status_view,
    location_allowed,
)
from .types import NewsItem, ParsedStatus

log = logging.getLogger("red.zed3v.gamewatch")

TargetChannel = Union[discord.TextChannel, discord.Thread]
LOCATION_ACTIONS = ("add", "remove", "clear", "reset")


STATUS_INTERVAL = 90  # seconds
NEWS_INTERVAL = 900  # seconds
CONFIRM_POLLS = 2  # consecutive bad polls before announcing an incident
RECOVERY_MIN_SECONDS = 120  # suppress "recovered" for blips shorter than this
MAX_SEEN_PER_GAME = 80  # ring buffer of announced uids
FAILURE_ALERT_AFTER = 12  # consecutive errors before the owner is DMed


class GameWatch(commands.Cog):
    """Per-game server status and patch notes for LoL, TFT, VALORANT, CS2, EFT,
    ARC Raiders and Arena Breakout.

    Status and news are posted to separate, per-game channels. Nothing is
    announced until a channel has been configured for that game.
    """

    __author__ = "TheD3v_"
    __version__ = "1.0.0"

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x47414D4557415443, force_registration=True)

        self.config.register_guild(
            games={},  # game -> {news_channel, status_channel, role, region, enabled}
            ping_severity="warning",
        )
        self.config.register_global(
            seen={},  # game -> [uid, ...]
            bootstrapped={},  # game -> bool (cold-start guard)
            last_status={},  # game -> {"ok": bool, "since": iso, "items": [...]}
        )

        self.session: Optional[aiohttp.ClientSession] = None
        self._etags: dict[str, str] = {}
        self._pending: dict[str, int] = {}  # game -> consecutive bad polls
        # Last successful parse per status source, so a 304 (unchanged payload)
        # can still be fed through the debounce/recovery state machine.
        self._last_parsed: dict[str, dict] = {}
        self._failures: dict[str, int] = {}  # source key -> consecutive errors
        self._alerted: set[str] = set()
        # One lock per pipeline, serialising each loop against a manual
        # [p]gamewatch forcecheck (unlocked, both could pass _is_seen before
        # either marks, announcing the same item twice). Deliberately NOT one
        # shared lock: news fetches use 30s timeouts, and a hung news upstream
        # must never stall the 90s status loop - outage detection matters most
        # exactly when upstreams are degraded.
        self._status_lock = asyncio.Lock()
        self._news_lock = asyncio.Lock()

    def _get_session(self) -> aiohttp.ClientSession:
        """Return an open ClientSession, creating one lazily if needed."""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers={"User-Agent": USER_AGENT},
                # Default ttl_dns_cache is 10s; every host would be re-resolved on
                # each 90s poll. Keepalive across 90s gaps is not winnable, so DNS
                # TTL is the only connector knob worth setting.
                connector=aiohttp.TCPConnector(ttl_dns_cache=300, limit=20),
            )
        return self.session

    async def cog_load(self) -> None:
        # Starting the session and loops here (not __init__) means a failed
        # add_cog never leaks running tasks or an open ClientSession - the
        # modern discord.py/Red 3.5 idiom.
        self._get_session()
        self.status_loop.start()
        self.news_loop.start()

    def format_help_for_context(self, ctx: commands.Context) -> str:
        base = super().format_help_for_context(ctx)
        return f"{base}\n\nAuthor: {self.__author__}\nVersion: {self.__version__}"

    async def cog_unload(self) -> None:
        self.status_loop.cancel()
        self.news_loop.cancel()
        # Give a mid-request iteration a moment to unwind before the session
        # is pulled out from under it.
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

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    async def _guild_game_cfg(self, guild: discord.Guild, game: str) -> dict:
        games = await self.config.guild(guild).games()
        return games.get(game) or {}

    async def _targets(
        self, game: str, field: str, all_guilds: Optional[dict] = None
    ) -> list[tuple[discord.Guild, dict]]:
        """All (guild, cfg) pairs that have a channel configured for this game.

        Pass a prefetched ``await self.config.all_guilds()`` mapping from loop
        code so one Config read serves every game in the pass, instead of one
        deepcopied read per guild per game.
        """
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

    # Seen-uid rings are namespaced "news:{game}" / "status:{game}". Sharing
    # one ring per game let Riot incident-update churn (each update mints a new
    # uid, one per watched region) evict still-announceable news uids, causing
    # duplicate patch-note posts. The legacy un-namespaced "{game}" key is
    # migrated on first write.
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
                # One-time migration: the legacy ring held news uids.
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

    async def _send_ctx_embed(
        self,
        ctx: commands.Context,
        embed: discord.Embed,
        *,
        allowed_mentions: Optional[discord.AllowedMentions] = None,
    ) -> None:
        """Send an embed in a command response, honouring ctx.embed_requested()."""
        if await ctx.embed_requested():
            await ctx.send(embed=embed, allowed_mentions=allowed_mentions)
            return
        text = embed_to_plain_text(embed) or "No details available."
        for page in pagify(text, page_length=1990):
            await ctx.send(page, allowed_mentions=allowed_mentions)

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
        if not can_send:
            log.warning("Missing send permission in #%s (%s)", channel.name, channel.guild.name)
            return
        # Only the explicitly configured role may resolve. roles=True would let
        # a "<@&id>" string inside upstream-controlled article text ping
        # arbitrary roles (Steam/Riot bodies are third-party content).
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
                # No embed permission or embeds disabled via [p]embedset:
                # degrade cleanly to markdown rather than staying silent.
                text = embed_to_plain_text(embed)
                full = f"{content}\n{text}" if content else text
                for page in pagify(full.strip(), page_length=1990):
                    await channel.send(content=page, allowed_mentions=allowed)
        except discord.HTTPException as exc:
            # 50035 = Invalid Form Body, i.e. an embed that slipped past the
            # size guard. Retry once as plain text so the update is not lost.
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
    # status loop
    # ------------------------------------------------------------------
    @tasks.loop(seconds=STATUS_INTERVAL)
    async def status_loop(self) -> None:
        try:
            await self._check_status_once()
        except Exception:
            # tasks.loop stops on uncaught errors; a flaky upstream must not
            # kill the loop, so log and carry on.
            log.exception("Unhandled error in status loop")

    @status_loop.before_loop
    async def _status_before(self) -> None:
        await self.bot.wait_until_red_ready()

    async def _check_status_once(self) -> None:
        async with self._status_lock:
            await self._check_status_inner()

    async def _check_status_inner(self) -> None:
        all_guilds = await self.config.all_guilds()
        for game, cfg in GAMES.items():
            if cfg["status_kind"] is None:
                continue
            targets = await self._targets(game, "status_channel", all_guilds)
            if not targets:
                continue

            # Region is per-guild, but the upstream call is per-region: group.
            regions = {(gcfg.get("region") or cfg["default_region"]) for _, gcfg in targets}
            for region in regions:
                key = f"status-{game}-{region}"
                session = self._get_session()
                try:
                    if cfg["status_kind"] == "riot":
                        parsed, etag, http = await fetch_riot_status(
                            session, game, region, self._etags.get(key)
                        )
                    else:
                        parsed, etag, http = await fetch_eft_status(session, self._etags.get(key))
                except FetchError as exc:
                    await self._note_failure(key, exc)
                    continue

                if http == 304:
                    self._note_success(key)
                    # 304 means the payload is byte-identical to the last 200,
                    # so the parsed state is unchanged - but the debounce and
                    # recovery logic still has to SEE that state on every poll.
                    # Skipping _process_status here would freeze the pending
                    # counter at 1 forever for any incident Riot does not
                    # actively update, so a sustained outage would never be
                    # announced. Re-process the cached parse instead.
                    cached = self._last_parsed.get(key)
                    if cached is not None:
                        await self._process_status(game, region, cached, targets)
                    continue
                if http != 200 or parsed is None:
                    await self._note_failure(key, f"HTTP {http}")
                    continue

                self._note_success(key)
                if etag:
                    self._etags[key] = etag
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

        # Location filter: build the effective view for this region's targets.
        # Union semantics - an incident allowed by ANY matching-region guild
        # counts as region-degraded; zero matching targets => no filtering
        # (also keeps the statetest harness, which passes targets=[], intact).
        cfg = GAMES[game]
        region_targets = [
            (g, gc)
            for g, gc in targets
            if (gc.get("region") or cfg.get("default_region")) == region
        ]
        if region_targets:
            allowlists = [self._effective_allowlist(gc, game) for _, gc in region_targets]
            if any(not a for a in allowlists):
                pass  # some guild disables the filter -> full view for the union
            else:
                union: list[str] = sorted({loc for a in allowlists for loc in a})
                parsed = filter_status_view(parsed, union)
                # Mark filtered uids seen NOW, before the ok early-return below
                # (all-filtered flips ok True and returns early). Guarded by
                # _is_seen so a lingering filtered incident polled every 90s
                # is appended once, not every poll - unguarded appends would
                # flood the 80-slot ring and evict real incident uids.
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

        last = (await self.config.last_status()).get(state_key) or {}
        was_ok = last.get("ok", True)
        now = datetime.now(timezone.utc)

        if parsed["ok"]:
            self._pending.pop(state_key, None)
            if not was_ok:
                # Degraded -> healthy transition.
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
                # Persist only the transition; rewriting an identical healthy
                # record every 90s per game/region is pointless disk churn.
                async with self.config.last_status() as store:
                    store[state_key] = {"ok": True, "since": now.isoformat()}
            return

        # Degraded. Debounce so a single blip does not fire an alert.
        self._pending[state_key] = self._pending.get(state_key, 0) + 1
        if self._pending[state_key] < CONFIRM_POLLS:
            log.debug("Status for %s degraded, awaiting confirmation", state_key)
            return

        for item in parsed["items"]:
            # Scope dedupe per region: the same Riot incident id can appear in
            # several regions' status documents, and guilds are filtered by
            # region inside _announce_incident - a game-global uid would let
            # the first region processed swallow the announcement for all
            # the others.
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
        severity = (item.get("severity") or "info").lower()
        embed = incident_embed(game, region, parsed, item)

        for guild, gcfg in targets:
            if (gcfg.get("region") or cfg.get("default_region")) != region:
                continue
            # Per-guild location check: _process_status applies union semantics
            # across all guilds watching this region, so a guild that disables
            # filtering must not leak filtered locations into other guilds.
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

    async def _announce_recovery(self, game: str, region: str, parsed: dict, targets: list) -> None:
        cfg = GAMES[game]
        # Surface how long the outage lasted, read back from the state store
        # that the status loop maintains. Key format must match
        # _process_status: "{game}-{region}".
        state_key = f"{game}-{region}"
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
            channel = self._resolve_channel(guild, gcfg.get("status_channel"))
            if channel:
                await self._send(channel, embed)

    # ------------------------------------------------------------------
    # news loop
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
        # Stagger behind the status loop's first pass so both pipelines do not
        # burst-fetch at the same moment on startup.
        await asyncio.sleep(10)

    async def _check_news_once(self) -> None:
        async with self._news_lock:
            await self._check_news_inner()

    async def _bootstrap_seen(self, game: str, items: list[NewsItem]) -> None:
        """Cold start: record every visible item WITHOUT announcing.

        A fresh install or a cleared ring must not dump the last 20 patch
        notes into the channel.

        IMPORTANT: mark in chronological order (oldest first). The seen list
        is a ring buffer trimmed from the FRONT, so whatever is appended last
        survives. Riot feeds return ~150 items newest-first; appending in feed
        order would push the NEWEST uids out of the ring and make the next
        poll re-announce months-old patch notes. Steam feeds are short enough
        that the bug never showed there. One Config transaction, not one per
        item.
        """
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
        for game, cfg in GAMES.items():
            targets = await self._targets(game, "news_channel", all_guilds)
            if not targets:
                continue
            key = f"news-{game}"
            session = self._get_session()
            try:
                if cfg["news_kind"] == "steam":
                    items, etag, http = await fetch_steam_news(
                        session, game, etag=self._etags.get(key)
                    )
                else:
                    items, etag, http = await fetch_riot_news(
                        session, game, etag=self._etags.get(key)
                    )
            except FetchError as exc:
                await self._note_failure(key, exc)
                continue

            if http == 304:
                self._note_success(key)
                continue
            if http != 200 or items is None:
                await self._note_failure(key, f"HTTP {http}")
                continue

            self._note_success(key)
            if etag:
                self._etags[key] = etag

            if not bootstrapped.get(game):
                await self._bootstrap_seen(game, items)
                continue

            for item in reversed(items[:10]):
                if await self._is_seen("news", game, item["uid"]):
                    continue
                await self._announce_news(game, item, targets)
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
        current: str, *, status_only: bool = False, riot_only: bool = False
    ) -> list[app_commands.Choice[str]]:
        needle = current.lower().strip()
        choices: list[app_commands.Choice[str]] = []
        for key, cfg in GAMES.items():
            if status_only and cfg["status_kind"] is None:
                continue
            if riot_only and cfg["status_kind"] != "riot":
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
        tracked, news_only = [], []
        for key, cfg in GAMES.items():
            line = f"{cfg['emoji']} **{cfg['name']}** \u2014 `{key}`"
            (tracked if cfg["status_kind"] else news_only).append(line)
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
            text="ARC Raiders and Arena Breakout publish no status API, so they are news only."
        )
        await self._send_ctx_embed(ctx, fit_embed(embed))

    @gamewatch.command(name="news")
    @commands.admin_or_permissions(manage_guild=True)
    @app_commands.describe(
        game="Game key (e.g. lol, cs2, valorant)",
        channel="Channel or thread for patch notes (omit to disable)",
    )
    @app_commands.autocomplete(game=_game_autocomplete)
    async def gw_news(
        self, ctx: commands.Context, game: str, channel: Optional[TargetChannel] = None
    ) -> None:
        """Set the patch-notes channel for a game. Omit the channel to clear it."""
        game = game.lower()
        if game not in GAMES:
            return await ctx.send(
                f"Unknown game. Valid keys: {humanize_list([f'`{g}`' for g in GAMES])}"
            )
        async with self.config.guild(ctx.guild).games() as games:
            entry = games.setdefault(game, {})
            entry["news_channel"] = channel.id if channel else None
            entry.setdefault("enabled", True)
        if channel:
            await ctx.send(f"{GAMES[game]['name']} patch notes will post to {channel.mention}.")
        else:
            await ctx.send(f"{GAMES[game]['name']} patch notes disabled.")

    @gamewatch.command(name="status")
    @commands.admin_or_permissions(manage_guild=True)
    @app_commands.describe(
        game="Game key with status monitoring (lol, tft, valorant, eft)",
        channel="Channel or thread for status alerts (omit to disable)",
    )
    @app_commands.autocomplete(game=_status_game_autocomplete)
    async def gw_status(
        self, ctx: commands.Context, game: str, channel: Optional[TargetChannel] = None
    ) -> None:
        """Set the status/incident channel for a game."""
        game = game.lower()
        if game not in GAMES:
            return await ctx.send(
                f"Unknown game. Valid keys: {humanize_list([f'`{g}`' for g in GAMES])}"
            )
        if GAMES[game]["status_kind"] is None:
            return await ctx.send(
                f"{GAMES[game]['name']} has no public status API, so status monitoring "
                "is not available for it. Patch notes still work."
            )
        async with self.config.guild(ctx.guild).games() as games:
            entry = games.setdefault(game, {})
            entry["status_channel"] = channel.id if channel else None
            entry.setdefault("region", GAMES[game]["default_region"])
            entry.setdefault("enabled", True)
        if channel:
            await ctx.send(f"{GAMES[game]['name']} status alerts will post to {channel.mention}.")
        else:
            await ctx.send(f"{GAMES[game]['name']} status alerts disabled.")

    @gamewatch.command(name="role")
    @commands.admin_or_permissions(manage_guild=True)
    @app_commands.describe(
        game="Game key (e.g. lol, cs2, valorant)",
        role="Role to ping on announcements (omit to clear)",
    )
    @app_commands.autocomplete(game=_game_autocomplete)
    async def gw_role(
        self, ctx: commands.Context, game: str, role: Optional[discord.Role] = None
    ) -> None:
        """Set the role pinged for a game. Omit the role to clear it."""
        game = game.lower()
        if game not in GAMES:
            return await ctx.send("Unknown game.")
        async with self.config.guild(ctx.guild).games() as games:
            entry = games.setdefault(game, {})
            entry["role"] = role.id if role else None
        await ctx.send(
            f"{GAMES[game]['name']} pings {role.mention if role else 'nobody'}.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @gamewatch.command(name="region")
    @commands.admin_or_permissions(manage_guild=True)
    @app_commands.describe(
        game="Riot game key (lol, tft, valorant)",
        region="Riot region ID (e.g. eun1, euw1, na1 for LoL/TFT; eu, na, ap for VALORANT)",
    )
    @app_commands.autocomplete(game=_riot_game_autocomplete)
    async def gw_region(self, ctx: commands.Context, game: str, region: str) -> None:
        """Set the region for a Riot game.

        LoL and TFT use platform IDs (eun1, euw1, na1). VALORANT uses
        deployments (eu, na, ap, br, kr, latam).
        """
        game = game.lower()
        if game not in GAMES or GAMES[game]["status_kind"] != "riot":
            return await ctx.send("That game has no configurable region.")
        region = region.lower()
        if not re.fullmatch(r"[a-z0-9]{1,16}", region):
            return await ctx.send("Regions are short alphanumeric IDs like `eun1` or `eu`.")
        async with ctx.typing():
            try:
                parsed, _, http = await fetch_riot_status(self._get_session(), game, region)
            except FetchError as exc:
                return await ctx.send(f"Could not reach Riot: {exc}")
        if http != 200 or parsed is None:
            return await ctx.send(
                f"Riot returned HTTP {http} for region `{region}`. "
                "LoL/TFT want platform IDs like `eun1`; VALORANT wants `eu`."
            )
        async with self.config.guild(ctx.guild).games() as games:
            games.setdefault(game, {})["region"] = region
        await ctx.send(
            f"{GAMES[game]['name']} region set to `{region}` ({parsed.get('region_name')})."
        )

    @gamewatch.command(name="locations")
    @commands.admin_or_permissions(manage_guild=True)
    @app_commands.describe(
        game="Riot game key (e.g. valorant)",
        action="Action: add, remove, clear, or reset (omit to view)",
        location="Server location name (e.g. warsaw, madrid)",
    )
    @app_commands.autocomplete(game=_riot_game_autocomplete, action=_location_action_autocomplete)
    async def gw_locations(
        self,
        ctx: commands.Context,
        game: str,
        action: Optional[str] = None,
        *,
        location: Optional[str] = None,
    ) -> None:
        """Manage the status location allowlist for a game.

        Incidents naming a game-server location outside the allowlist are
        suppressed (still deduped, never re-announced later). Incidents with
        no recognisable location always announce.

        `[p]gamewatch locations valorant` - show
        `[p]gamewatch locations valorant add warsaw`
        `[p]gamewatch locations valorant remove tokyo`
        `[p]gamewatch locations valorant clear` - disable filtering
        `[p]gamewatch locations valorant reset` - back to default
        """
        game = game.lower()
        if game not in GAMES or GAMES[game]["status_kind"] != "riot":
            return await ctx.send("Location filtering only applies to Riot status games.")
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
            dflt = humanize_list([f"`{a}`" for a in default]) if default else "none"
            return await ctx.send(f"{name} allowlist ({src}): {state}\nDefault: {dflt}")

        action = action.lower()
        if action in ("add", "remove"):
            if not location:
                return await ctx.send(f"Usage: `locations {game} {action} <location>`")
            loc = location.lower().strip()
            if not re.fullmatch(r"[a-z0-9 ,.\-]{1,40}", loc):
                return await ctx.send("Locations are short plain-text names like `warsaw`.")
            new = [a for a in current if a != loc]
            if action == "add":
                new.append(loc)
            elif len(new) == len(current):
                return await ctx.send(f"`{loc}` is not on the list.")
            async with self.config.guild(ctx.guild).games() as games:
                games.setdefault(game, {})["location_allow"] = sorted(new)
            shown = (
                humanize_list([f"`{a}`" for a in sorted(new)]) if new else "empty (filter disabled)"
            )
            return await ctx.send(f"{name} allowlist is now: {shown}")
        if action == "clear":
            async with self.config.guild(ctx.guild).games() as games:
                games.setdefault(game, {})["location_allow"] = []
            return await ctx.send(f"{name} location filtering disabled; every location announces.")
        if action == "reset":
            async with self.config.guild(ctx.guild).games() as games:
                games.setdefault(game, {}).pop("location_allow", None)
            dflt = humanize_list([f"`{a}`" for a in default]) if default else "none (no default)"
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
        lines = []
        for key, cfg in GAMES.items():
            entry = games.get(key) or {}
            news = f"<#{entry['news_channel']}>" if entry.get("news_channel") else "\u2014"
            status = f"<#{entry['status_channel']}>" if entry.get("status_channel") else "\u2014"
            role = f"<@&{entry['role']}>" if entry.get("role") else "\u2014"
            region = entry.get("region") or cfg.get("default_region") or "\u2014"
            extra = ""
            if cfg["status_kind"] == "riot":
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
    @app_commands.describe(game="Game key with status monitoring (lol, tft, valorant, eft)")
    @app_commands.autocomplete(game=_status_game_autocomplete)
    async def gw_check(self, ctx: commands.Context, game: str) -> None:
        """Check a game's live status right now."""
        game = game.lower()
        if game not in GAMES:
            return await ctx.send("Unknown game.")
        cfg = GAMES[game]
        if cfg["status_kind"] is None:
            return await ctx.send(
                f"{cfg['name']} publishes no status API. Only patch notes are available for it."
            )
        entry = await self._guild_game_cfg(ctx.guild, game)
        region = entry.get("region") or cfg["default_region"]
        async with ctx.typing():
            session = self._get_session()
            try:
                if cfg["status_kind"] == "riot":
                    parsed, _, http = await fetch_riot_status(session, game, region)
                else:
                    parsed, _, http = await fetch_eft_status(session)
            except FetchError as exc:
                return await ctx.send(f"Fetch failed: {exc}")
        if http != 200 or parsed is None:
            return await ctx.send(f"Upstream returned HTTP {http}.")

        # Apply this guild's location filter so check mirrors what the loop
        # would announce; filtered incidents stay visible in a quiet field.
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
    @app_commands.describe(game="Game key (e.g. lol, cs2, valorant)")
    @app_commands.autocomplete(game=_game_autocomplete)
    async def gw_latest(self, ctx: commands.Context, game: str) -> None:
        """Show the most recent patch notes for a game."""
        game = game.lower()
        if game not in GAMES:
            return await ctx.send("Unknown game.")
        cfg = GAMES[game]
        async with ctx.typing():
            session = self._get_session()
            try:
                if cfg["news_kind"] == "steam":
                    items, _, http = await fetch_steam_news(session, game)
                else:
                    items, _, http = await fetch_riot_news(session, game)
            except FetchError as exc:
                return await ctx.send(f"Fetch failed: {exc}")
        if http != 200 or not items:
            return await ctx.send("Nothing found.")
        await self._send_ctx_embed(
            ctx, news_embed(game, items[0], accent=accent_int(items[0].get("accent")))
        )

    @gamewatch.command(name="diag")
    @commands.is_owner()
    async def gw_diag(self, ctx: commands.Context) -> None:
        """Show fetch health for every source."""
        all_guilds = await self.config.all_guilds()
        lines = []
        for game, cfg in GAMES.items():
            news_fail = self._failures.get(f"news-{game}", 0)
            lines.append(f"news-{game:9} failures={news_fail}")
            if cfg["status_kind"]:
                regions = {
                    (gcfg.get("region") or cfg["default_region"])
                    for data in all_guilds.values()
                    for gcfg in [((data.get("games") or {}).get(game) or {})]
                    if gcfg.get("status_channel")
                }
                if not regions:
                    regions = {cfg["default_region"]}
                for region in sorted(regions):
                    key = f"status-{game}-{region}"
                    lines.append(
                        f"status-{game}-{region:5} failures={self._failures.get(key, 0)} "
                        f"pending={self._pending.get(f'{game}-{region}', 0)}"
                    )
        seen = await self.config.seen()
        boot = await self.config.bootstrapped()
        lines.append("")
        lines.append(f"etags cached: {len(self._etags)}")
        lines.append(f"seen uids: {sum(len(v) for v in seen.values())}")
        lines.append(f"bootstrapped: {sorted(k for k, v in boot.items() if v)}")
        await ctx.send(box("\n".join(lines), lang="ini"))

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
        """Clear the dedupe history for a game (it will re-announce the next item)."""
        game = game.lower()
        if game not in GAMES:
            return await ctx.send("Unknown game.")
        async with self.config.seen() as seen:
            # Clear every namespace for the game, including any legacy
            # un-namespaced ring from before the news/status split.
            for key in (game, f"news:{game}", f"status:{game}"):
                seen.pop(key, None)
        async with self.config.bootstrapped() as boot:
            boot[game] = False
        await ctx.send(f"Dedupe history cleared for {GAMES[game]['name']}.")
