"""Shared shape contracts for GameWatch."""

from __future__ import annotations

from typing import Optional, TypedDict


class StatusItem(TypedDict, total=False):
    """One incident or maintenance item from a status source."""

    uid: str  # dedupe key; unique per incident update
    kind: str  # "incidents" | "maintenances"
    id: object  # upstream id, opaque
    severity: str  # "info" | "warning" | "critical"
    title: str
    body: str
    platforms: list[str]
    created_at: Optional[str]
    updated_at: Optional[str]


class ParsedStatus(TypedDict, total=False):
    """Normalised result of any status fetch (Riot or EFT)."""

    ok: bool
    region_id: Optional[str]
    region_name: Optional[str]
    items: list[StatusItem]
    services: list[dict]
    worst: int
    # Populated by sources.filter_status_view (location allowlist):
    filtered_count: int
    filtered_items: list[StatusItem]


class NewsItem(TypedDict, total=False):
    """One patch note from Steam or a Riot news page.

    Steam items carry timestamp (unix); Riot items carry published_at
    (ISO 8601). embeds._parse_ts handles both.
    """

    uid: str
    title: str
    url: str
    body: str
    raw_bbcode: str
    author: str
    timestamp: int
    published_at: str
    image: Optional[str]
    accent: Optional[str]
    category: str
