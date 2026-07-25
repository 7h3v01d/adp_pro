"""Search providers: one adapter per upstream indexer source.

Deliberately synchronous and built on `requests` -- the same HTTP stack the
download engine already uses -- so provider code runs identically from the
GUI's worker thread, the REST API's threadpool, and MCP tool calls, with no
event-loop management in a process that already runs Qt's loop on the main
thread and uvicorn's loop on a background one. Concurrency across providers
comes from the service's thread fan-out, not from async.

Providers must raise ProviderError (never raw requests exceptions) so the
service can attribute failures and degrade gracefully: one dead provider
never fails the whole search. Malformed rows cost the row, not the search.
"""
from __future__ import annotations

import abc
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Type

import requests

from adp.search.models import SearchQuery, SearchResult, SourceHit, infohash_from_magnet, normalize_infohash

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 12.0


@dataclass(frozen=True)
class ProviderCapabilities:
    categories: bool = False       # honors SearchQuery.category
    magnets: bool = False          # returns magnet links directly
    infohash: bool = False         # returns infohashes

    def to_dict(self) -> dict:
        return asdict(self)


class ProviderError(Exception):
    def __init__(self, provider: str, message: str):
        super().__init__(f"[{provider}] {message}")
        self.provider = provider
        self.message = message


class SearchProvider(abc.ABC):
    """Base class. A provider is disabled unless its settings say
    enabled=true (deny-first, same as everything else in this app)."""

    name: str = "unnamed"
    kind: str = "native"
    capabilities: ProviderCapabilities = ProviderCapabilities()

    def __init__(self, settings: Optional[Dict[str, Any]] = None):
        self.settings = settings or {}
        self.timeout = float(self.settings.get("timeout", DEFAULT_TIMEOUT_SECONDS))

    @abc.abstractmethod
    def search(self, session: requests.Session, query: SearchQuery) -> List[SearchResult]:
        raise NotImplementedError

    def _get_json(self, session: requests.Session, url: str, params: dict) -> Any:
        try:
            response = session.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            raise ProviderError(self.name, f"request failed: {e}") from e
        except ValueError as e:
            raise ProviderError(self.name, f"upstream returned non-JSON: {e}") from e


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
_REGISTRY: Dict[str, Type[SearchProvider]] = {}


def register(cls: Type[SearchProvider]) -> Type[SearchProvider]:
    if cls.name in _REGISTRY:
        raise ValueError(f"Duplicate search provider name: {cls.name}")
    _REGISTRY[cls.name] = cls
    return cls


def available_providers() -> Dict[str, Type[SearchProvider]]:
    return dict(_REGISTRY)


# ---------------------------------------------------------------------------
# Jackett (Torznab aggregate JSON endpoint)
# ---------------------------------------------------------------------------
_TORZNAB_CATEGORIES = {
    "software": "4000", "audio": "3000", "video": "2000",
    "books": "7000", "games": "1000", "other": "8000",
}


@register
class JackettProvider(SearchProvider):
    """One adapter, everything a local Jackett instance proxies:

        GET {base_url}/api/v2.0/indexers/{indexer}/results?apikey=...&Query=...
    """
    name = "jackett"
    kind = "torznab"
    capabilities = ProviderCapabilities(categories=True, magnets=True, infohash=True)

    def __init__(self, settings: Optional[Dict[str, Any]] = None):
        super().__init__(settings)
        self.base_url = str(self.settings.get("base_url", "http://127.0.0.1:9117")).rstrip("/")
        self.api_key = str(self.settings.get("api_key", ""))
        self.indexer = str(self.settings.get("indexer", "all"))

    def search(self, session: requests.Session, query: SearchQuery) -> List[SearchResult]:
        if not self.api_key:
            raise ProviderError(self.name, "api_key is not configured (Settings -> Search).")
        params = {"apikey": self.api_key, "Query": query.text}
        if query.category in _TORZNAB_CATEGORIES:
            params["Category[]"] = _TORZNAB_CATEGORIES[query.category]
        payload = self._get_json(
            session, f"{self.base_url}/api/v2.0/indexers/{self.indexer}/results", params
        )
        results = []
        for row in payload.get("Results", []) if isinstance(payload, dict) else []:
            parsed = self._parse_row(row)
            if parsed is not None:
                results.append(parsed)
        return results

    def _parse_row(self, row: dict) -> Optional[SearchResult]:
        title = (row.get("Title") or "").strip()
        if not title:
            return None
        magnet = row.get("MagnetUri") or None
        infohash = normalize_infohash(row.get("InfoHash"))
        if infohash is None and magnet:
            infohash = infohash_from_magnet(magnet)
        seeders = _to_int(row.get("Seeders")) or 0
        peers = _to_int(row.get("Peers")) or 0
        return SearchResult(
            title=title, infohash=infohash, magnet=magnet,
            size_bytes=_to_int(row.get("Size")),
            published=_parse_iso_date(row.get("PublishDate")),
            category=row.get("CategoryDesc") or None,
            seeders=seeders, leechers=peers,
            sources=[SourceHit(
                provider=self.name, tracker=row.get("Tracker"),
                seeders=seeders, leechers=peers, details_url=row.get("Details"),
            )],
        )


# ---------------------------------------------------------------------------
# torrents-csv (native; also the reference implementation for new adapters)
# ---------------------------------------------------------------------------
@register
class TorrentsCsvProvider(SearchProvider):
    """Public JSON API, no key needed:

        GET {base_url}/service/search?q=...&size=...
    """
    name = "torrents_csv"
    kind = "native"
    capabilities = ProviderCapabilities(infohash=True)

    def __init__(self, settings: Optional[Dict[str, Any]] = None):
        super().__init__(settings)
        self.base_url = str(self.settings.get("base_url", "https://torrents-csv.com")).rstrip("/")

    def search(self, session: requests.Session, query: SearchQuery) -> List[SearchResult]:
        payload = self._get_json(
            session, f"{self.base_url}/service/search",
            {"q": query.text, "size": min(query.limit, 100)},
        )
        rows = payload.get("torrents", []) if isinstance(payload, dict) else payload
        results = []
        for row in rows if isinstance(rows, list) else []:
            parsed = self._parse_row(row)
            if parsed is not None:
                results.append(parsed)
        return results

    def _parse_row(self, row: dict) -> Optional[SearchResult]:
        title = (row.get("name") or "").strip()
        infohash = normalize_infohash(row.get("infohash"))
        if not title or not infohash:
            return None
        published = None
        created = row.get("created_unix")
        if isinstance(created, (int, float)) and created > 0:
            published = datetime.fromtimestamp(created, tz=timezone.utc)
        seeders = _to_int(row.get("seeders")) or 0
        leechers = _to_int(row.get("leechers")) or 0
        return SearchResult(
            title=title, infohash=infohash, size_bytes=_to_int(row.get("size_bytes")),
            published=published, seeders=seeders, leechers=leechers,
            sources=[SourceHit(provider=self.name, seeders=seeders, leechers=leechers)],
        )


def _to_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_iso_date(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
