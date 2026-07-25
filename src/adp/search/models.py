"""Data types for the torrent search subsystem.

Every provider, whatever its upstream shape, normalizes into these types.
The service, REST API, MCP tools, and Search tab speak only this vocabulary
-- providers are the sole place upstream formats exist.

Kept separate from torrent/models.py deliberately: a search result is a
candidate (may not even have resolved metadata), a TorrentRecord is a
commitment the engine is managing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

_INFOHASH_V1 = re.compile(r"^[0-9a-fA-F]{40}$")
_INFOHASH_V2 = re.compile(r"^[0-9a-fA-F]{64}$")
_MAGNET_BTIH = re.compile(
    r"xt=urn:bt[im]h:([0-9a-fA-F]{40}|[0-9a-fA-F]{64}|[A-Z2-7]{32})", re.IGNORECASE
)


def normalize_infohash(value: Optional[str]) -> Optional[str]:
    """Lowercased hex infohash, or None if it isn't one. Never raises --
    a provider handing us a junk hash should cost us that field, not the row."""
    if not value:
        return None
    value = value.strip().lower()
    if _INFOHASH_V1.match(value) or _INFOHASH_V2.match(value):
        return value
    return None


def infohash_from_magnet(magnet: str) -> Optional[str]:
    """Extract a hex infohash from a magnet URI, if present (hex forms only;
    base32 magnets are valid but not decoded here)."""
    m = _MAGNET_BTIH.search(magnet or "")
    if not m:
        return None
    return normalize_infohash(m.group(1))


@dataclass
class SearchQuery:
    text: str
    category: Optional[str] = None            # provider-neutral hint, e.g. "software"
    providers: Optional[List[str]] = None     # restrict to these names; None = all enabled
    limit: int = 50

    def __post_init__(self):
        self.text = (self.text or "").strip()
        if not self.text:
            raise ValueError("Search query text is empty.")
        self.limit = max(1, min(int(self.limit), 200))


@dataclass
class SourceHit:
    """Where a result was seen. One SearchResult may have several."""
    provider: str
    tracker: Optional[str] = None
    seeders: Optional[int] = None
    leechers: Optional[int] = None
    details_url: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "provider": self.provider, "tracker": self.tracker,
            "seeders": self.seeders, "leechers": self.leechers,
            "details_url": self.details_url,
        }


@dataclass
class SearchResult:
    """A single (deduplicated) torrent search result."""
    title: str
    infohash: Optional[str] = None            # lowercase hex when known
    magnet: Optional[str] = None
    size_bytes: Optional[int] = None
    published: Optional[datetime] = None
    category: Optional[str] = None
    seeders: int = 0                          # best-known (max across sources)
    leechers: int = 0
    sources: List[SourceHit] = field(default_factory=list)
    score: float = 0.0                        # filled by the ranker

    def identity(self) -> str:
        """Dedupe key: infohash when known, else a normalized title key."""
        if self.infohash:
            return f"hash:{self.infohash}"
        return "title:" + re.sub(r"[^a-z0-9]+", ".", self.title.lower()).strip(".")

    def ensure_magnet(self) -> Optional[str]:
        """A magnet link, synthesized from the infohash if necessary."""
        if self.magnet:
            return self.magnet
        if self.infohash:
            return f"magnet:?xt=urn:btih:{self.infohash}"
        return None

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "infohash": self.infohash,
            "magnet": self.ensure_magnet(),
            "size_bytes": self.size_bytes,
            "published": self.published.isoformat() if self.published else None,
            "category": self.category,
            "seeders": self.seeders,
            "leechers": self.leechers,
            "score": self.score,
            "sources": [s.to_dict() for s in self.sources],
        }


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
