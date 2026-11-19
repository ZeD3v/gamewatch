"""Embed construction for GameWatch.

Verified against the discord.py 2.7.1 Embed source and live upstream payloads.

Discord's hard limits (enforced here so a long patch note can never trigger a
400 Invalid Form Body):
    title 256 | description 4096 | field name 256 | field value 1024
    footer 2048 | author 256 | 25 fields | 6000 total across the whole embed
len(Embed) counts title + description + fields + footer + author, so we check
it before sending and degrade gracefully instead of raising.

Markdown inside embed descriptions supports bold/italic/lists/masked links and
- since the 2023 formatting update - headings (#, ##, ###) and subtext (-#).
Tables and images-by-syntax do NOT render; use fields and set_image instead.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

import discord

from .constants import GAMES
from .constants import RIOT_SEVERITY_ORDER as SEVERITY_ORDER

MAX_TITLE = 256
MAX_DESC = 4096
MAX_FIELD_NAME = 256
MAX_FIELD_VALUE = 1024
MAX_FOOTER = 2048
MAX_AUTHOR = 256
MAX_TOTAL = 6000
MAX_FIELDS = 25

# Soft budget for a news body. Well under MAX_DESC so section headings and the
# "read the full notes" line always fit.
NEWS_BODY_BUDGET = 2600

SEVERITY_STYLE = {
    "info": {"colour": 0x3B88C3, "emoji": "\U0001f535", "label": "Info"},
    "warning": {"colour": 0xF2A93B, "emoji": "\U0001f7e0", "label": "Warning"},
    "critical": {"colour": 0xE4572E, "emoji": "\U0001f534", "label": "Critical"},
}
OK_COLOUR = 0x43B581
RECOVERY_EMOJI = "\U0001f7e2"

# EFT service status ints -> presentation. Mapping confirmed against
# the-hideout/tarkov-data-manager's statusMap.
EFT_STATUS_STYLE = {
    0: ("\U0001f7e2", "OK"),
    1: ("\U0001f535", "Updating"),
    2: ("\U0001f7e1", "Unstable"),
    3: ("\U0001f534", "Down"),
}


def _close_markdown(text: str) -> str:
    """Repair markdown broken by truncation.

    A cut can land inside **bold**, ||spoiler||, a `code` span or a
    [masked](link); unbalanced tokens render as literal garbage, and an
    unterminated masked link swallows everything after it.
    """
    # Drop an unterminated masked link ("[text](url" or a bare "[text").
    m = re.search(r"\[[^\]]*\]\([^)]*$", text)
    if m:
        text = text[: m.start()].rstrip()
    m = re.search(r"\[[^\]]*$", text)
    if m:
        text = text[: m.start()].rstrip()
    # Close dangling pair tokens.
    for tok in ("||", "~~", "**", "__"):
        if text.count(tok) % 2:
            text += tok
    if text.count("`") % 2:
        text += "`"
    return text


def _trunc(text: str, limit: int) -> str:
    """Truncate on a word boundary with an ellipsis, never mid-word."""
    if not text:
        return ""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if " " in cut:
        candidate = cut.rsplit(" ", 1)[0]
        # Guard: a leading-space or single-word string would degenerate to "".
        if candidate.strip():
            cut = candidate
    cut = cut.rstrip(" ,.;:-")
    if not cut:
        cut = text[: limit - 1].strip()
    return _close_markdown(cut) + "\u2026"


def fit_embed(embed: discord.Embed) -> discord.Embed:
    """Force an embed under the 6000-char aggregate limit.

    discord.py computes len(Embed) exactly as Discord does, so we trim the
    description (the only unbounded part) until it fits. Without this a long
    patch note raises 400 Invalid Form Body and the announcement is lost.
    """
    if len(embed) <= MAX_TOTAL:
        return embed
    overflow = len(embed) - MAX_TOTAL
    desc = embed.description or ""
    if desc:
        embed.description = _trunc(desc, max(len(desc) - overflow - 32, 200))
    if len(embed) > MAX_TOTAL:
        while embed.fields and len(embed) > MAX_TOTAL:
            embed.remove_field(len(embed.fields) - 1)
    return embed


def _parse_ts(item: dict) -> Optional[datetime]:
    if item.get("timestamp"):
        try:
            return datetime.fromtimestamp(int(item["timestamp"]), tz=timezone.utc)
        except (ValueError, OSError, TypeError):
            return None
    if item.get("published_at"):
        try:
            return datetime.fromisoformat(str(item["published_at"]).replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _relative(dt: Optional[datetime]) -> str:
    """Discord relative timestamp - renders in each viewer's own locale."""
    if not dt:
        return ""
    return f"<t:{int(dt.timestamp())}:R>"


# ---------------------------------------------------------------------------
# Steam BBCode -> Discord markdown
# ---------------------------------------------------------------------------
# Steam announcements are BBCode, not HTML. Flattening them to plain text (the
# naive approach) destroys the structure of a patch note - CS2 notes are almost
# entirely nested [list] bullets under [p]\[ SECTION ][/p] headers, so stripped
# output is an unreadable wall. We translate to Discord markdown instead.
_STEAM_CLAN_IMG = "https://clan.akamai.steamstatic.com/images/"


def _bb_lists(text: str) -> str:
    """Convert [list]/[*] blocks to markdown bullets, preserving nesting."""
    out = []
    depth = 0
    # Tokenise on the tags we care about so nesting depth can be tracked.
    for token in re.split(r"(\[/?o?list[^\]]*\]|\[\*\]|\[/\*\])", text, flags=re.I):
        if not token:
            continue
        low = token.lower()
        if low.startswith(("[list", "[olist")):
            depth += 1
        elif low.startswith(("[/list", "[/olist")):
            depth = max(0, depth - 1)
        elif low == "[*]":
            indent = "  " * max(0, depth - 1)
            out.append(f"\n{indent}- ")
        elif low == "[/*]":
            continue
        else:
            out.append(token)
    return "".join(out)


def steam_bbcode_to_markdown(
    text: str, budget: int = NEWS_BODY_BUDGET
) -> tuple[str, Optional[str]]:
    """Return (markdown_body, first_image_url).

    The image is pulled out for set_image() because Discord embeds do not
    render ![](url) syntax - verified: image-by-syntax is unsupported.
    """
    if not text:
        return "", None

    # DoS guard: the lazy-quantifier substitutions below are O(n^2) on
    # pathological unclosed-tag input, and Steam bodies are third-party
    # content fetched uncapped (maxlength=0 is required to keep BBCode).
    # The output budget is ~2.6k chars, so parsing more than 16k buys
    # nothing and a crafted 180KB payload would block the event loop for
    # ~30s. Measured before capping.
    if len(text) > 16000:
        text = text[:16000]

    image: Optional[str] = None
    m = re.search(r"\[img\s+src=\"([^\"]+)\"\s*\]", text, flags=re.I)
    if not m:
        m = re.search(r"\[img\]([^\[]+)\[/img\]", text, flags=re.I)
    if m:
        raw = m.group(1).strip()
        # Steam embeds a {STEAM_CLAN_IMAGE} placeholder; the akamai host is the
        # one that serves it directly (cloudflare 301s, cdn 302s).
        raw = raw.replace("{STEAM_CLAN_IMAGE}/", _STEAM_CLAN_IMG)
        raw = raw.replace("{STEAM_CLAN_IMAGE}", _STEAM_CLAN_IMG)
        if raw.startswith("http"):
            image = raw

    body = text
    # Paired form first, then bare openers. A single lazy pattern with an
    # optional closer matches zero body chars and leaks the raw URL into
    # the description.
    body = re.sub(r"\[img[^\]]*\](.*?)\[/img\]", "", body, flags=re.I | re.S)
    body = re.sub(r"\[img[^\]]*\]", "", body, flags=re.I)
    body = _bb_lists(body)

    # Headings. Discord embeds support ##/### since the 2023 formatting update.
    body = re.sub(r"\[h1\](.*?)\[/h1\]", r"\n## \1\n", body, flags=re.I | re.S)
    body = re.sub(r"\[h2\](.*?)\[/h2\]", r"\n## \1\n", body, flags=re.I | re.S)
    body = re.sub(r"\[h3\](.*?)\[/h3\]", r"\n### \1\n", body, flags=re.I | re.S)
    body = re.sub(r"\[h[4-6]\](.*?)\[/h[4-6]\]", r"\n### \1\n", body, flags=re.I | re.S)

    # Inline styles.
    body = re.sub(r"\[b\](.*?)\[/b\]", r"**\1**", body, flags=re.I | re.S)
    body = re.sub(r"\[i\](.*?)\[/i\]", r"*\1*", body, flags=re.I | re.S)
    body = re.sub(r"\[u\](.*?)\[/u\]", r"__\1__", body, flags=re.I | re.S)
    body = re.sub(r"\[strike\](.*?)\[/strike\]", r"~~\1~~", body, flags=re.I | re.S)
    body = re.sub(r"\[spoiler\](.*?)\[/spoiler\]", r"||\1||", body, flags=re.I | re.S)

    def _code_repl(m: "re.Match[str]") -> str:
        inner = m.group(1)
        if "\n" in inner:
            return f"\n```\n{inner.strip()}\n```\n"
        return f"`{inner}`"

    body = re.sub(r"\[code\](.*?)\[/code\]", _code_repl, body, flags=re.I | re.S)
    body = re.sub(r"\[quote[^\]]*\](.*?)\[/quote\]", r"> \1", body, flags=re.I | re.S)
    body = re.sub(r"\[url=([^\]]+)\](.*?)\[/url\]", r"[\2](\1)", body, flags=re.I | re.S)

    # Paragraphs become line breaks.
    body = re.sub(r"\[/p\]", "\n", body, flags=re.I)
    body = re.sub(r"\[p\]", "", body, flags=re.I)
    body = re.sub(r"\[/?hr\]", "\n", body, flags=re.I)

    # Strip known leftover Steam tags only. A catch-all [alnum...] pattern
    # also deletes legitimate prose brackets like [2023] or [1.2.3].
    body = re.sub(
        r"\[/?(?:previewyoutube|dynamiclink|table|tr|td|th|video|noparse|expand"
        r"|olist|list|url|quote|spoiler|strike|code|h[1-6]|[biup]|\*)[^\]]*\]",
        "",
        body,
        flags=re.I,
    )

    # Steam escapes its own brackets as \[ SECTION ]; unescape for readability.
    body = body.replace("\\[", "[").replace("\\]", "]")
    body = body.replace("\u00a0", " ")
    body = re.sub(r"[ \t]+\n", "\n", body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    # A bullet directly after a heading reads better without the blank line,
    # and nested bullets must not be separated by blank lines or Discord
    # renders each as its own list.
    body = re.sub(r"\n\n(\s*- )", r"\n\1", body)
    # Valve writes section headers as an escaped literal "\[ MAPS ]" paragraph
    # rather than a [h2]; promote those to real Discord headings.
    body = re.sub(r"^\[ *([A-Z0-9][A-Z0-9 &/_'-]{2,40}?) *\]$", r"## \1", body, flags=re.M)
    # "## **Foo**" is redundant - headings are already bold.
    body = re.sub(r"^(#{1,3}) \*\*(.+?)\*\*$", r"\1 \2", body, flags=re.M)
    # Collapse a heading immediately followed by another heading.
    body = re.sub(r"\n{3,}", "\n\n", body)
    body = body.strip()

    if len(body) > budget:
        body = _trunc(body, budget)
    return body, image


def _patch_version(title: str) -> Optional[str]:
    """Pull a version number out of a patch title for the author line."""
    m = re.search(r"(\d+\.\d+(?:\.\d+)*)", title or "")
    return m.group(1) if m else None


def news_embed(game: str, item: dict, accent: Optional[int] = None) -> discord.Embed:
    """Build the patch-notes embed.

    Layout:
        author  -> game name + icon (small, consistent branding)
        title   -> patch title, hyperlinked to the source
        desc    -> converted markdown body with real headings and bullets
        image   -> article hero art (Riot) or first inline image (Steam)
        fields  -> version / published, inline
        footer  -> source attribution + game art thumbnail
    """
    cfg = GAMES[game]
    ts = _parse_ts(item)

    body = item.get("body") or ""
    image = item.get("image")
    if item.get("raw_bbcode"):
        body, bb_image = steam_bbcode_to_markdown(item["raw_bbcode"])
        image = image or bb_image
    else:
        body = _trunc(body, NEWS_BODY_BUDGET)

    url = item.get("url") or None
    if url and body:
        body = f"{body}\n\n[**Read the full notes \u2192**]({url})"

    embed = discord.Embed(
        title=_trunc(item.get("title") or "Update", MAX_TITLE),
        url=url,
        description=_trunc(body, MAX_DESC) or None,
        colour=discord.Colour(accent if accent is not None else cfg["colour"]),
        timestamp=ts,
    )

    icon = cfg.get("icon")
    embed.set_author(name=cfg["name"], url=cfg.get("site"), icon_url=icon)

    if image:
        embed.set_image(url=image)
    elif cfg.get("art"):
        embed.set_image(url=cfg["art"])

    version = _patch_version(item.get("title") or "")
    if version:
        embed.add_field(name="Version", value=f"`{version}`", inline=True)
    if ts:
        embed.add_field(name="Published", value=_relative(ts), inline=True)
    if item.get("author"):
        embed.add_field(name="Posted by", value=_trunc(item["author"], 64), inline=True)

    embed.set_footer(text=f"{cfg['source_label']} \u2022 GameWatch", icon_url=icon)
    return fit_embed(embed)


def incident_embed(game: str, region: str, parsed: dict, item: dict) -> discord.Embed:
    """Build the status-incident embed.

    Colour and the leading emoji both encode severity, so the alert is
    readable at a glance and still distinguishable for colour-blind members.
    """
    cfg = GAMES[game]
    severity = (item.get("severity") or "info").lower()
    style = SEVERITY_STYLE.get(severity, SEVERITY_STYLE["info"])
    region_name = _trunc(str(parsed.get("region_name") or region.upper()), 80)

    title = _trunc(f"{style['emoji']} {item.get('title') or 'Service issue'}", MAX_TITLE)
    embed = discord.Embed(
        title=title,
        description=_trunc(item.get("body") or "No further detail provided.", MAX_DESC) or None,
        colour=discord.Colour(style["colour"]),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_author(
        name=_trunc(f"{cfg['name']} \u2014 {region_name}", MAX_AUTHOR),
        icon_url=cfg.get("icon"),
    )

    embed.add_field(name="Severity", value=f"{style['emoji']} {style['label']}", inline=True)
    embed.add_field(name="Region", value=_trunc(str(region_name), MAX_FIELD_VALUE), inline=True)
    if item.get("platforms"):
        plats = ", ".join(str(p).upper() for p in item["platforms"])
        embed.add_field(name="Platforms", value=_trunc(plats, MAX_FIELD_VALUE), inline=True)

    started = _parse_ts({"published_at": item.get("created_at")})
    if started:
        embed.add_field(name="Started", value=_relative(started), inline=True)

    services = parsed.get("services") or []
    degraded = [s for s in services if (s.get("status") or 0) > 0]
    if degraded:
        lines = []
        for svc in degraded[:12]:
            emoji, label = EFT_STATUS_STYLE.get(svc.get("status") or 0, ("\u26aa", "Unknown"))
            lines.append(f"{emoji} **{svc.get('name') or 'Service'}** \u2014 {label}")
        embed.add_field(
            name=f"Affected services ({len(degraded)}/{len(services)})",
            value=_trunc("\n".join(lines), MAX_FIELD_VALUE),
            inline=False,
        )

    if cfg.get("status_url"):
        embed.add_field(
            name="\u200b",
            value=f"[Official status page \u2192]({cfg['status_url']})",
            inline=False,
        )

    if cfg.get("icon"):
        embed.set_thumbnail(url=cfg["icon"])
    embed.set_footer(text=f"{cfg['status_label']} \u2022 GameWatch", icon_url=cfg.get("icon"))
    return fit_embed(embed)


def recovery_embed(
    game: str, region: str, parsed: dict, down_since: Optional[datetime]
) -> discord.Embed:
    cfg = GAMES[game]
    region_name = _trunc(str(parsed.get("region_name") or region.upper()), 80)
    embed = discord.Embed(
        title=f"{RECOVERY_EMOJI} Services back to normal",
        description=f"All monitored services for **{region_name}** are reporting OK.",
        colour=discord.Colour(OK_COLOUR),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_author(
        name=_trunc(f"{cfg['name']} \u2014 {region_name}", MAX_AUTHOR),
        icon_url=cfg.get("icon"),
    )
    if down_since:
        embed.add_field(name="Issue started", value=_relative(down_since), inline=True)
        delta = datetime.now(timezone.utc) - down_since
        mins = int(delta.total_seconds() // 60)
        dur = f"{mins // 60}h {mins % 60}m" if mins >= 60 else f"{mins}m"
        embed.add_field(name="Duration", value=f"`{dur}`", inline=True)
    if cfg.get("icon"):
        embed.set_thumbnail(url=cfg["icon"])
    embed.set_footer(text=f"{cfg['status_label']} \u2022 GameWatch", icon_url=cfg.get("icon"))
    return fit_embed(embed)


def status_panel_embed(game: str, region: str, parsed: dict) -> discord.Embed:
    """On-demand status snapshot for [p]gamewatch check."""
    cfg = GAMES[game]
    region_name = _trunc(str(parsed.get("region_name") or region.upper()), 80)
    ok = parsed.get("ok", True)
    incidents = parsed.get("items") or []

    if ok:
        colour, headline = OK_COLOUR, f"{RECOVERY_EMOJI} **All systems operational**"
    else:
        worst = "info"
        for inc in incidents:
            sev = (inc.get("severity") or "info").lower()
            if SEVERITY_ORDER.get(sev, 0) > SEVERITY_ORDER.get(worst, 0):
                worst = sev
        style = SEVERITY_STYLE.get(worst, SEVERITY_STYLE["info"])
        colour = style["colour"]
        headline = f"{style['emoji']} **{len(incidents)} active incident(s)**"

    embed = discord.Embed(
        title=_trunc(f"{cfg['name']} \u2014 {region_name}", MAX_TITLE),
        url=cfg.get("status_url"),
        description=headline,
        colour=discord.Colour(colour),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_author(name="Live status", icon_url=cfg.get("icon"))

    services = parsed.get("services") or []
    if services:
        lines = []
        for svc in services[:15]:
            emoji, label = EFT_STATUS_STYLE.get(svc.get("status") or 0, ("\u26aa", "Unknown"))
            lines.append(f"{emoji} {svc.get('name') or 'Service'}")
        embed.add_field(
            name=f"Services ({len(services)})",
            value=_trunc("\n".join(lines), MAX_FIELD_VALUE),
            inline=False,
        )

    for inc in incidents[:4]:
        sev = (inc.get("severity") or "info").lower()
        style = SEVERITY_STYLE.get(sev, SEVERITY_STYLE["info"])
        embed.add_field(
            name=_trunc(f"{style['emoji']} {inc.get('title') or 'Incident'}", MAX_FIELD_NAME),
            value=_trunc(inc.get("body") or "\u2014", 300),
            inline=False,
        )

    if cfg.get("icon"):
        embed.set_thumbnail(url=cfg["icon"])
    embed.set_footer(text=f"{cfg['status_label']} \u2022 GameWatch", icon_url=cfg.get("icon"))
    return fit_embed(embed)


def accent_int(value: object) -> Optional[int]:
    """Convert a Riot CMS '#rrggbb' accent string to an int for discord.Colour."""
    if not value or not isinstance(value, str):
        return None
    value = value.strip().lstrip("#")
    if len(value) != 6:
        return None
    try:
        return int(value, 16)
    except ValueError:
        return None


def embed_to_plain_text(embed: discord.Embed) -> str:
    """Render an embed cleanly as plain markdown for non-embed fallback."""
    parts: list[str] = []
    if embed.author and embed.author.name:
        parts.append(f"*{embed.author.name}*")
    if embed.title:
        parts.append(f"**{embed.title}**")
    if embed.description:
        parts.append(embed.description)
    for field in embed.fields:
        name = (field.name or "").strip()
        val = (field.value or "").strip()
        if not val:
            continue
        if name and name != "\u200b":
            parts.append(f"**{name}:** {val}")
        else:
            parts.append(val)
    if embed.url and (not embed.description or embed.url not in embed.description):
        parts.append(embed.url)
    return "\n\n".join(parts).strip()
