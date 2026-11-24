"""Shared shape contracts for GameWatch."""

from __future__ import annotations

from typing import Optional, TypedDict


class CacheValidator(TypedDict, total=False):
    """HTTP conditional-request validators returned by an upstream."""

    etag: Optional[str]
    last_modified: Optional[str]


class StatusItem(TypedDict, total=False):
    """One incident or maintenance item from a status source."""

    uid: str  # dedupe key; unique per incident update
    kind: str  # "incidents" | "maintenances"
    id: object  # upstream id, opaque
    severity: str  # "info" | "warning" | "critical"
    maintenance_status: Optional[str]  # "scheduled" | "in_progress" | None
    title: str
    body: str
    location: Optional[str]  # explicit datacenter/server location when known
    platforms: list[str]
    created_at: Optional[str]
    updated_at: Optional[str]


class ParsedStatus(TypedDict, total=False):
    """Normalised result of any status fetch (Riot, EFT, or Steam CS2)."""

    ok: bool
    region_id: Optional[str]
    region_name: Optional[str]
    items: list[StatusItem]
    services: list[dict]
    datacenters: list[dict]
    matchmaking: dict
    global_message: Optional[str]
    worst: int
    # Populated by sources.filter_status_view (location allowlist):
    filtered_count: int
    filtered_items: list[StatusItem]


class NewsItem(TypedDict, total=False):
    """One patch note from Steam, Riot, BSG, or Level Infinite CMS.

    Steam and ABI items carry timestamp (unix); Riot and EFT BSG items carry
    published_at (ISO 8601). embeds._parse_ts handles both.
    """

    uid: str
    title: str
    url: str
    body: str
    raw_bbcode: str
    raw_html: str
    content_id: str
    author: str
    timestamp: int
    published_at: str
    image: Optional[str]
    accent: Optional[str]
    category: str
