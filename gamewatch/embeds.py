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
from html.parser import HTMLParser
from typing import Callable, Optional

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
HTML_MAX_INPUT = 131_072  # 128 KiB O(n) HTMLParser guard (covers 71 KB ABI patches + <style>)

SEVERITY_STYLE = {
    "info": {"colour": 0x3B88C3, "emoji": "\U0001f535", "label": "Info"},
    "warning": {"colour": 0xF2A93B, "emoji": "\U0001f7e0", "label": "Warning"},
    "critical": {"colour": 0xE4572E, "emoji": "\U0001f534", "label": "Critical"},
}
MAINTENANCE_LABELS = {
    "scheduled": "Scheduled Maintenance",
    "in_progress": "Maintenance in Progress",
}
OK_COLOUR = 0x43B581
RECOVERY_EMOJI = "\U0001f7e2"

# EFT service/datacenter status ints -> presentation. Mapping confirmed against
# BSG's Nuxt bundle and the-hideout/tarkov-data-manager's statusMap.
EFT_STATUS_STYLE = {
    0: ("\U0001f7e2", "OK"),
    1: ("\U0001f535", "Updating"),
    2: ("\U0001f7e1", "Unstable"),
    3: ("\U0001f534", "Down"),
}

# Precompiled markdown / BBCode regexes
_UNCLOSED_MASKED_LINK_RE = re.compile(r"\[[^\]]*\]\([^)]*$")
_UNCLOSED_BRACKET_RE = re.compile(r"\[[^\]]*$")
_VERSION_RE = re.compile(r"(\d+\.\d+(?:\.\d+)*)")

_STEAM_CLAN_IMG = "https://clan.akamai.steamstatic.com/images/"
_BB_LIST_TOKEN_RE = re.compile(r"((?<!\\)\[/?o?list(?:[\s=][^\]]*)?\]|(?<!\\)\[/?\*\])", re.I)
_BB_IMG_SRC_RE = re.compile(r'\[img\s+src="([^"]+)"\s*\]', re.I)
_BB_IMG_PAIR_EXTRACT_RE = re.compile(r"\[img\]([^\[]+)\[/img\]", re.I)
_BB_IMG_PAIR_STRIP_RE = re.compile(r"\[img(?:[\s=][^\]]*)?\][^\[]*\[/img\]", re.I | re.S)
_BB_IMG_BARE_STRIP_RE = re.compile(r"(?<!\\)\[/?img(?:[\s=][^\]]*)?\]", re.I)
_BB_H12_RE = re.compile(r"\[h[12]\](.*?)\[/h[12]\]", re.I | re.S)
_BB_H36_RE = re.compile(r"\[h[3-6]\](.*?)\[/h[3-6]\]", re.I | re.S)
_BB_BOLD_RE = re.compile(r"\[b\](.*?)\[/b\]", re.I | re.S)
_BB_ITALIC_RE = re.compile(r"\[i\](.*?)\[/i\]", re.I | re.S)
_BB_UNDERLINE_RE = re.compile(r"\[u\](.*?)\[/u\]", re.I | re.S)
_BB_STRIKE_RE = re.compile(r"\[strike\](.*?)\[/strike\]", re.I | re.S)
_BB_SPOILER_RE = re.compile(r"\[spoiler\](.*?)\[/spoiler\]", re.I | re.S)
_BB_CODE_RE = re.compile(r"\[code\](.*?)\[/code\]", re.I | re.S)
_BB_QUOTE_RE = re.compile(r"\[quote(?:[\s=][^\]]*)?\](.*?)\[/quote\]", re.I | re.S)
_BB_URL_RE = re.compile(r"\[url=([^\]]+)\](.*?)\[/url\]", re.I | re.S)
_BB_URL_LEFTOVER_RE = re.compile(r"(?<!\\)\[/?url(?:[\s=][^\]]*)?\](?!\()", re.I)
_BB_CELL_BETWEEN_RE = re.compile(r"\[/(?:td|th)\]\s*\[(?:td|th)(?:[\s=][^\]]*)?\]", re.I)
_BB_ROW_OPEN_CELL_RE = re.compile(r"\[tr(?:[\s=][^\]]*)?\]\s*\[(?:td|th)(?:[\s=][^\]]*)?\]", re.I)
_BB_ROW_CLOSE_CELL_RE = re.compile(r"\[/(?:td|th)\]\s*\[/tr\]", re.I)
_BB_CELL_CLOSE_RE = re.compile(r"\[/(?:td|th)\]", re.I)
_BB_ROW_CLOSE_RE = re.compile(r"\[/tr\]", re.I)
_BB_P_CLOSE_RE = re.compile(r"(?<!\\)\[/p\]", re.I)
_BB_P_OPEN_RE = re.compile(r"(?<!\\)\[p(?:[\s=][^\]]*)?\](?!\()", re.I)
_BB_HR_RE = re.compile(r"(?<!\\)\[/?hr\]", re.I)
_BB_LEFTOVER_RE = re.compile(
    r"(?<!\\)\[/?(?:(?:[biu]|\*)"
    r"|(?:previewyoutube|dynamiclink|table|tr|td|th|video|noparse|expand"
    r"|olist|list|quote|spoiler|strike|code|h[1-6]|p)(?:[\s=][^\]]*)?)\](?!\()",
    re.I,
)
_TRAILING_WS_RE = re.compile(r"(?<![ \t])[ \t]+\n")
_MULTI_NL_RE = re.compile(r"\n{3,}")
_BULLET_GAP_RE = re.compile(r"\n\n(\s*- )")
_BULLET_LEADING_SPACE_RE = re.compile(r"^([ \t]*- )[ \t]+", re.M)
_VALVE_SECTION_RE = re.compile(r"^\[ *([A-Z0-9][A-Z0-9 &/_'-]{2,40}?) *\]$", re.M)
_BOLD_HEADING_RE = re.compile(r"^(#{1,3}) \*\*((?:(?!\*\*).)+)\*\*$", re.M)
_TRAILING_PIPE_RE = re.compile(r"(?<![ \t])[ \t]+\|[ \t]*$", re.M)
_INLINE_MULTI_SPACE_RE = re.compile(r"(?<=\S)[ \t]{2,}")
_MAX_LIST_INDENT = 8  # cap nested bullet indentation to prevent amplification DoS


def _close_markdown(text: str) -> str:
    """Repair markdown broken by truncation.

    A cut can land inside **bold**, ||spoiler||, a `code` span or a
    [masked](link); unbalanced tokens render as literal garbage, and an
    unterminated masked link swallows everything after it.
    """
    m = _UNCLOSED_MASKED_LINK_RE.search(text)
    if m:
        text = text[: m.start()].rstrip()
    m = _UNCLOSED_BRACKET_RE.search(text)
    if m:
        text = text[: m.start()].rstrip()
    for tok in ("||", "~~", "**", "__"):
        if text.count(tok) % 2:
            text += tok
    if text.count("`") % 2:
        text += "`"
    return text


def _trunc(text: str, limit: int) -> str:
    """Truncate on a word boundary with an ellipsis, strictly <= limit after closing markdown."""
    if not text:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 1:
        return "\u2026"[:limit]
    budget = limit - 1
    while budget > 0:
        cut = text[:budget]
        if " " in cut:
            candidate = cut.rsplit(" ", 1)[0]
            if candidate.strip():
                cut = candidate
        cut = cut.rstrip(" ,.;:-")
        if not cut:
            cut = text[:budget].strip()
        closed = _close_markdown(cut)
        if len(closed) + 1 <= limit:
            return closed + "\u2026"
        excess = (len(closed) + 1) - limit
        budget = min(len(cut) - excess, budget - 1)
    return text[: limit - 1] + "\u2026"


def fit_embed(embed: discord.Embed) -> discord.Embed:
    """Force an embed under Discord's description (4096) and aggregate (6000) limits.

    discord.py computes len(Embed) exactly as Discord does, so we trim the
    description (the only unbounded part) until it fits. Without this a long
    patch note raises 400 Invalid Form Body and the announcement is lost.
    """
    if embed.description and len(embed.description) > MAX_DESC:
        embed.description = _trunc(embed.description, MAX_DESC)
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


def _wrap_inline(marker: str) -> Callable[[re.Match[str]], str]:
    """Preserve outer whitespace outside Discord inline emphasis markers."""

    def _repl(m: re.Match[str]) -> str:
        raw = m.group(1)
        inner = raw.strip()
        if not inner:
            return raw
        lead = raw[: len(raw) - len(raw.lstrip())]
        trail = raw[len(raw.rstrip()) :]
        return f"{lead}{marker}{inner}{marker}{trail}"

    return _repl


def _heading_repl(prefix: str) -> Callable[[re.Match[str]], str]:
    """Strip leading/trailing newlines inside headings so Discord renders them."""

    def _repl(m: re.Match[str]) -> str:
        inner = re.sub(r"\s+", " ", m.group(1)).strip()
        return f"\n{prefix} {inner}\n" if inner else "\n"

    return _repl


# ---------------------------------------------------------------------------
# Steam BBCode -> Discord markdown
# ---------------------------------------------------------------------------
def _bb_lists(text: str) -> str:
    """Convert [list]/[*] blocks to markdown bullets, preserving nesting."""
    out = []
    depth = 0
    for idx, token in enumerate(_BB_LIST_TOKEN_RE.split(text)):
        if not token:
            continue
        if idx % 2 == 0:
            out.append(token)
            continue
        low = token.lower()
        if low.startswith(("[list", "[olist")):
            depth += 1
        elif low.startswith(("[/list", "[/olist")):
            depth = max(0, depth - 1)
        elif low == "[*]":
            indent = "  " * min(max(0, depth - 1), _MAX_LIST_INDENT)
            out.append(f"\n{indent}- ")
        elif low == "[/*]":
            continue
    return "".join(out)


def _code_repl(m: re.Match[str]) -> str:
    inner = m.group(1)
    if "\n" in inner:
        return f"\n```\n{inner.strip()}\n```\n"
    return f"`{inner}`"


def _url_repl(m: re.Match[str]) -> str:
    href = m.group(1).strip().strip('"').strip("'").strip()
    label = m.group(2)
    if not label.strip():
        return ""
    if not href.startswith(("http://", "https://")):
        return label
    lead = " " if label != label.lstrip() else ""
    trail = " " if label != label.rstrip() else ""
    return f"{lead}[{label.strip()}]({href}){trail}"


def steam_bbcode_to_markdown(
    text: str, budget: int = NEWS_BODY_BUDGET
) -> tuple[str, Optional[str]]:
    """Return (markdown_body, first_image_url).

    The image is pulled out for set_image() because Discord embeds do not
    render ![](url) syntax.
    """
    if not text:
        return "", None

    # DoS guard: lazy-quantifier substitutions are O(n^2) on hostile unclosed
    # tags. Output budget is ~2.6k chars, so capping raw BBCode at 16k keeps
    # worst-case conversion well under 20ms.
    if len(text) > 16000:
        text = text[:16000]

    image: Optional[str] = None
    m = _BB_IMG_SRC_RE.search(text) or _BB_IMG_PAIR_EXTRACT_RE.search(text)
    if m:
        raw = m.group(1).strip()
        raw = raw.replace("{STEAM_CLAN_IMAGE}/", _STEAM_CLAN_IMG)
        raw = raw.replace("{STEAM_CLAN_IMAGE}", _STEAM_CLAN_IMG)
        if raw.startswith("http"):
            image = raw

    body = text
    body = _BB_IMG_PAIR_STRIP_RE.sub("", body)
    body = _BB_IMG_BARE_STRIP_RE.sub("", body)
    body = _bb_lists(body)

    # Tables: normalize multiline cell/row boundaries before stripping tags.
    body = _BB_CELL_BETWEEN_RE.sub(" | ", body)
    body = _BB_ROW_OPEN_CELL_RE.sub("", body)
    body = _BB_ROW_CLOSE_CELL_RE.sub("\n", body)
    body = _BB_CELL_CLOSE_RE.sub(" | ", body)
    body = _BB_ROW_CLOSE_RE.sub("\n", body)

    # Headings and inline styles: guard each paired regex on the presence of its
    # closing tag so hostile inputs full of unclosed openers (e.g. "[b]" * 5000)
    # run in O(n) instead of quadratic backtracking.
    low_body = body.lower()
    if "[/h1]" in low_body or "[/h2]" in low_body:
        body = _BB_H12_RE.sub(_heading_repl("##"), body)
    if any(f"[/h{n}]" in low_body for n in "3456"):
        body = _BB_H36_RE.sub(_heading_repl("###"), body)

    # Inline styles (move outer spaces outside markers for Discord rendering).
    if "[/b]" in low_body:
        body = _BB_BOLD_RE.sub(_wrap_inline("**"), body)
    if "[/i]" in low_body:
        body = _BB_ITALIC_RE.sub(_wrap_inline("*"), body)
    if "[/u]" in low_body:
        body = _BB_UNDERLINE_RE.sub(_wrap_inline("__"), body)
    if "[/strike]" in low_body:
        body = _BB_STRIKE_RE.sub(_wrap_inline("~~"), body)
    if "[/spoiler]" in low_body:
        body = _BB_SPOILER_RE.sub(_wrap_inline("||"), body)
    if "[/code]" in low_body:
        body = _BB_CODE_RE.sub(_code_repl, body)
    if "[/quote]" in low_body:
        body = _BB_QUOTE_RE.sub(r"> \1", body)

    # Paragraphs become line breaks (supports [p align="center"] etc.).
    body = _BB_P_CLOSE_RE.sub("\n", body)
    body = _BB_P_OPEN_RE.sub("", body)
    body = _BB_HR_RE.sub("\n", body)

    # Strip known leftover Steam tags BEFORE converting [url=...] -> [label](href)
    # so markdown link labels can never be misread as BBCode tags.
    body = _BB_LEFTOVER_RE.sub("", body)
    if "[/url]" in body.lower():
        body = _BB_URL_RE.sub(_url_repl, body)
    body = _BB_URL_LEFTOVER_RE.sub("", body)

    body = body.replace("\\[", "[").replace("\\]", "]")
    body = body.replace("\u00a0", " ")
    body = _TRAILING_WS_RE.sub("\n", body)
    body = _TRAILING_PIPE_RE.sub("", body)
    body = _BULLET_LEADING_SPACE_RE.sub(r"\1", body)
    body = _MULTI_NL_RE.sub("\n\n", body)
    body = _BULLET_GAP_RE.sub(r"\n\1", body)
    body = _VALVE_SECTION_RE.sub(r"## \1", body)
    body = _BOLD_HEADING_RE.sub(r"\1 \2", body)
    body = _MULTI_NL_RE.sub("\n\n", body)
    body = body.strip()

    if len(body) > budget:
        body = _trunc(body, budget)
    return body, image


# ---------------------------------------------------------------------------
# HTML -> Discord markdown (stdlib HTMLParser, O(n), zero external deps)
# ---------------------------------------------------------------------------
_HTML_SKIP_TAGS = frozenset({"head", "style", "script", "noscript", "title"})
_HTML_INLINE_MARKERS: dict[str, str] = {
    "strong": "**",
    "b": "**",
    "em": "*",
    "i": "*",
    "u": "__",
    "s": "~~",
    "del": "~~",
    "strike": "~~",
    "code": "`",
}


_MAX_INLINE_DEPTH = 32  # keeps _inline_stack scans & unwinds strictly O(1)
_MAX_HEADING_CHARS = 512


class _MarkdownHTMLParser(HTMLParser):
    """Linear-time HTML-to-Discord-markdown converter for BSG and ABI CMS feeds."""

    def __init__(self, *, max_output: int) -> None:
        super().__init__(convert_charrefs=True)
        self._max_output = max_output
        self._out: list[str] = []
        self._out_len = 0
        self._skip_depth = 0
        self._inline_stack: list[tuple[str, str, list[str]]] = []
        self._inline_chars = 0
        self._list_stack: list[tuple[str, int]] = []
        self._in_li_start = False
        self._in_pre = False
        self._in_heading: Optional[str] = None
        self._heading_buf: list[str] = []
        self._heading_len = 0
        self._row_cells = 0
        self.first_image: Optional[str] = None

    def _emit(self, text: str) -> None:
        if not text:
            return
        if self._inline_stack:
            if self._inline_chars < self._max_output:
                self._inline_stack[-1][2].append(text)
                self._inline_chars += len(text)
            return
        if self._in_heading is not None:
            if self._heading_len < _MAX_HEADING_CHARS:
                self._heading_buf.append(text)
                self._heading_len += len(text)
            return
        if self._out_len >= self._max_output:
            return
        self._out.append(text)
        self._out_len += len(text)

    def _flush_heading(self) -> None:
        if self._in_heading is None:
            return
        prefix = self._in_heading
        heading_text = re.sub(r"\s+", " ", "".join(self._heading_buf)).strip()
        self._in_heading = None
        self._heading_buf = []
        self._heading_len = 0
        if heading_text:
            self._emit(f"\n\n{prefix} {heading_text}\n")

    def _flush_inline(self, tag: str) -> None:
        idx = -1
        for i in range(len(self._inline_stack) - 1, -1, -1):
            if self._inline_stack[i][0] == tag:
                idx = i
                break
        if idx == -1:
            return
        # Unwind up to matching tag cleanly on malformed HTML.
        while len(self._inline_stack) > idx:
            t, meta, buf = self._inline_stack.pop()
            raw = "".join(buf)
            self._inline_chars = max(0, self._inline_chars - len(raw))
            if not self._inline_stack:
                self._inline_chars = 0
            if t == "a":
                inner = raw.strip()
                if not inner:
                    continue
                lead = " " if raw != raw.lstrip() else ""
                trail = " " if raw != raw.rstrip() else ""
                if meta.startswith(("http://", "https://")):
                    self._emit(f"{lead}[{inner}]({meta}){trail}")
                else:
                    self._emit(raw)
            else:
                inner = raw.strip()
                if not inner:
                    continue
                if not meta:
                    self._emit(raw)
                    continue
                lead = " " if raw != raw.lstrip() else ""
                trail = " " if raw != raw.rstrip() else ""
                self._emit(f"{lead}{meta}{inner}{meta}{trail}")

    def _flush_all_inlines(self) -> None:
        while self._inline_stack:
            self._flush_inline(self._inline_stack[-1][0])

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        if tag in _HTML_SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth > 0:
            return

        if tag == "img":
            if self.first_image is None:
                for k, v in attrs:
                    if k.lower() == "src" and v:
                        src = v.strip()
                        if src.startswith(("http://", "https://")):
                            self.first_image = src
                            break
            return

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._flush_all_inlines()
            self._flush_heading()
            self._in_heading = "##" if tag in ("h1", "h2") else "###"
            self._heading_buf = []
            self._heading_len = 0
            self._in_li_start = False
            return

        if tag in ("ul", "ol"):
            self._flush_all_inlines()
            self._flush_heading()
            self._list_stack.append((tag, 0))
            return

        if tag == "li":
            self._flush_all_inlines()
            self._flush_heading()
            if self._list_stack:
                ltype, count = self._list_stack[-1]
                count += 1
                self._list_stack[-1] = (ltype, count)
                indent = "  " * min(max(0, len(self._list_stack) - 1), _MAX_LIST_INDENT)
                bullet = f"{count}. " if ltype == "ol" else "- "
            else:
                indent = ""
                bullet = "- "
            self._emit(f"\n{indent}{bullet}")
            self._in_li_start = True
            return

        if tag in ("p", "div"):
            if self._in_li_start:
                self._in_li_start = False
                return
            self._emit("\n")
            return

        if tag in ("br", "hr"):
            self._emit("\n")
            self._in_li_start = False
            return

        if tag == "pre":
            self._flush_all_inlines()
            self._in_pre = True
            self._emit("\n```\n")
            return

        if tag == "tr":
            self._row_cells = 0
            return

        if tag in ("td", "th"):
            if self._row_cells > 0:
                self._emit(" | ")
            self._row_cells += 1
            self._in_li_start = False
            return

        if tag == "a":
            if len(self._inline_stack) < _MAX_INLINE_DEPTH:
                href = ""
                for k, v in attrs:
                    if k.lower() == "href" and v:
                        href = v.strip()
                        break
                self._inline_stack.append(("a", href, []))
            self._in_li_start = False
            return

        if tag in _HTML_INLINE_MARKERS and not self._in_pre:
            if len(self._inline_stack) < _MAX_INLINE_DEPTH:
                marker = _HTML_INLINE_MARKERS[tag]
                # Avoid emitting nested duplicate markers like ****bold****.
                if any(m == marker for _, m, _ in self._inline_stack):
                    marker = ""
                self._inline_stack.append((tag, marker, []))
            self._in_li_start = False

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _HTML_SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth > 0:
            return

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6") and self._in_heading is not None:
            self._flush_all_inlines()
            self._flush_heading()
            return

        if tag in ("ul", "ol"):
            if self._list_stack:
                self._list_stack.pop()
            self._emit("\n")
            return

        if tag == "li":
            self._flush_all_inlines()
            self._in_li_start = False
            return

        if tag in ("p", "div"):
            self._flush_all_inlines()
            self._emit("\n")
            return

        if tag == "pre" and self._in_pre:
            self._in_pre = False
            self._emit("\n```\n")
            return

        if tag == "tr":
            self._flush_all_inlines()
            if self._row_cells > 0:
                self._emit("\n")
            self._row_cells = 0
            return

        if tag == "a" or tag in _HTML_INLINE_MARKERS:
            self._flush_inline(tag)

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0 or not data:
            return
        text = data.replace("\u00a0", " ")
        if not self._in_pre:
            # Collapse raw HTML source indentation/newlines to single spaces.
            text = re.sub(r"[ \t\r\n]+", " ", text)
            if self._in_li_start:
                text = text.lstrip()
                if text:
                    self._in_li_start = False
        self._emit(text)

    def get_markdown(self) -> str:
        self._flush_all_inlines()
        self._flush_heading()
        return "".join(self._out)


def html_to_markdown(html: str, budget: int = NEWS_BODY_BUDGET) -> tuple[str, Optional[str]]:
    """Convert BSG / Level Infinite CMS HTML into Discord markdown.

    Returns (markdown_body, first_image_url). Uses stdlib HTMLParser for O(n)
    ReDoS-proof parsing with zero third-party dependencies.
    """
    if not html:
        return "", None

    if len(html) > HTML_MAX_INPUT:
        html = html[:HTML_MAX_INPUT]

    parser = _MarkdownHTMLParser(max_output=budget * 4)
    parser.feed(html)
    parser.close()

    body = parser.get_markdown()
    body = body.replace("\u00a0", " ")
    # Normalize leading spaces on non-bullet lines while preserving nested list indentation.
    lines = []
    for line in body.splitlines():
        stripped = line.rstrip()
        if re.match(r"^\s*(?:- |\d+\. )", stripped):
            lines.append(_INLINE_MULTI_SPACE_RE.sub(" ", stripped))
        else:
            lines.append(_INLINE_MULTI_SPACE_RE.sub(" ", stripped.lstrip()))
    body = "\n".join(lines)
    body = _TRAILING_WS_RE.sub("\n", body)
    body = _TRAILING_PIPE_RE.sub("", body)
    body = _MULTI_NL_RE.sub("\n\n", body)
    body = _BULLET_GAP_RE.sub(r"\n\1", body)
    body = _BOLD_HEADING_RE.sub(r"\1 \2", body)
    body = _MULTI_NL_RE.sub("\n\n", body)
    body = body.strip()

    if len(body) > budget:
        body = _trunc(body, budget)
    return body, parser.first_image


def _patch_version(title: str) -> Optional[str]:
    """Pull a version number out of a patch title for the author line."""
    m = _VERSION_RE.search(title or "")
    return m.group(1) if m else None


def news_embed(game: str, item: dict, accent: Optional[int] = None) -> discord.Embed:
    """Build the patch-notes embed.

    Layout:
        author  -> game name + icon (small, consistent branding)
        title   -> patch title, hyperlinked to the source
        desc    -> converted markdown body with real headings and bullets
        image   -> article hero/capsule art, falling back to first inline image
                   then static store header art
        fields  -> version / published / author, inline
        footer  -> source attribution + icon
    """
    cfg = GAMES[game]
    ts = _parse_ts(item)

    plain_body = item.get("body") or ""
    body = plain_body
    image = item.get("image")
    if item.get("raw_bbcode"):
        bb_body, bb_image = steam_bbcode_to_markdown(item["raw_bbcode"])
        body = bb_body or _trunc(plain_body, NEWS_BODY_BUDGET)
        image = image or bb_image
    elif item.get("raw_html"):
        html_body, html_image = html_to_markdown(item["raw_html"])
        body = html_body or _trunc(plain_body, NEWS_BODY_BUDGET)
        image = image or html_image
    else:
        body = _trunc(plain_body, NEWS_BODY_BUDGET)

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


def _status_page_url(cfg: dict, region: str) -> Optional[str]:
    """Return the official status page URL, formatting the guild's region when templated."""
    template = cfg.get("status_url")
    if not template:
        return None
    if "{region}" in template:
        return template.format(region=(region or cfg.get("default_region") or "").lower())
    return template


def incident_embed(game: str, region: str, parsed: dict, item: dict) -> discord.Embed:
    """Build the status-incident embed.

    Colour and the leading emoji both encode severity, so the alert is
    readable at a glance and still distinguishable for colour-blind members.
    """
    cfg = GAMES[game]
    severity = (item.get("severity") or "info").lower()
    style = SEVERITY_STYLE.get(severity, SEVERITY_STYLE["info"])
    mstat = item.get("maintenance_status")
    sev_label = MAINTENANCE_LABELS.get(str(mstat), style["label"]) if mstat else style["label"]
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

    embed.add_field(name="Severity", value=f"{style['emoji']} {sev_label}", inline=True)
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

    datacenters = parsed.get("datacenters") or []
    degraded_dcs = [d for d in datacenters if (d.get("status") or 0) > 0]
    if degraded_dcs:
        dc_lines = []
        for dc in degraded_dcs[:12]:
            emoji, label = EFT_STATUS_STYLE.get(dc.get("status") or 0, ("\u26aa", "Unknown"))
            dc_name = dc.get("region") or dc.get("name") or "Datacenter"
            dc_lines.append(f"{emoji} **{dc_name}** \u2014 {label}")
        embed.add_field(
            name=f"Affected datacenters ({len(degraded_dcs)}/{len(datacenters)})",
            value=_trunc("\n".join(dc_lines), MAX_FIELD_VALUE),
            inline=False,
        )

    status_url = _status_page_url(cfg, region)
    if status_url:
        embed.add_field(
            name="\u200b",
            value=f"[Official status page \u2192]({status_url})",
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
        url=_status_page_url(cfg, region),
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
        active_items = [i for i in incidents if i.get("maintenance_status") != "scheduled"]
        active_count = len(active_items) or len(incidents)
        worst = "info"
        for inc in active_items or incidents:
            sev = (inc.get("severity") or "info").lower()
            if SEVERITY_ORDER.get(sev, 0) > SEVERITY_ORDER.get(worst, 0):
                worst = sev
        style = SEVERITY_STYLE.get(worst, SEVERITY_STYLE["info"])
        colour = style["colour"]
        headline = f"{style['emoji']} **{active_count} active incident(s)**"

    embed = discord.Embed(
        title=_trunc(f"{cfg['name']} \u2014 {region_name}", MAX_TITLE),
        url=_status_page_url(cfg, region),
        description=headline,
        colour=discord.Colour(colour),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_author(name="Live status", icon_url=cfg.get("icon"))

    services = parsed.get("services") or []
    if services:
        lines = []
        for svc in services[:15]:
            emoji, _ = EFT_STATUS_STYLE.get(svc.get("status") or 0, ("\u26aa", "Unknown"))
            lines.append(f"{emoji} {svc.get('name') or 'Service'}")
        embed.add_field(
            name=f"Services ({len(services)})",
            value=_trunc("\n".join(lines), MAX_FIELD_VALUE),
            inline=False,
        )

    datacenters = parsed.get("datacenters") or []
    if datacenters:
        dc_lines = []
        for dc in datacenters[:12]:
            emoji, _ = EFT_STATUS_STYLE.get(dc.get("status") or 0, ("\u26aa", "Unknown"))
            dc_name = dc.get("region") or dc.get("name") or "Datacenter"
            dc_lines.append(f"{emoji} {dc_name}")
        embed.add_field(
            name=f"Datacenters ({len(datacenters)})",
            value=_trunc("\n".join(dc_lines), MAX_FIELD_VALUE),
            inline=False,
        )

    for inc in incidents[:4]:
        sev = (inc.get("severity") or "info").lower()
        style = SEVERITY_STYLE.get(sev, SEVERITY_STYLE["info"])
        prefix = (
            f"{style['emoji']} [Scheduled] "
            if inc.get("maintenance_status") == "scheduled"
            else f"{style['emoji']} "
        )
        embed.add_field(
            name=_trunc(f"{prefix}{inc.get('title') or 'Incident'}", MAX_FIELD_NAME),
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
