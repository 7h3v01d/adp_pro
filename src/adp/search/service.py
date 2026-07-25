# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
"""SearchService: the one implementation of "search for torrents", called by
the Search tab, the REST API, and the MCP tools alike -- same principle as
AppController for downloads: exactly one answer to what a search means.

Fan-out runs providers concurrently on a small thread pool. A failing
provider is reported in SearchOutcome.errors but never fails the search.
Results are deduplicated by infohash (seeders merged with max(), not sum --
two indexers reporting the same swarm are one swarm) and ranked.
"""

from __future__ import annotations

import logging
import math
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import timezone
from typing import Callable, Dict, List, Optional

import requests

from adp.search.models import SearchQuery, SearchResult, utcnow
from adp.search.providers import (
    ProviderError,
    SearchProvider,
    available_providers,
)

logger = logging.getLogger(__name__)

MAX_CONCURRENT_PROVIDERS = 6
_RECENCY_HALF_LIFE_DAYS = 365.0


@dataclass
class SearchOutcome:
    results: List[SearchResult] = field(default_factory=list)
    providers_queried: List[str] = field(default_factory=list)
    errors: Dict[str, str] = field(default_factory=dict)   # provider name -> message

    def to_dict(self) -> dict:
        return {
            "count": len(self.results),
            "providers_queried": self.providers_queried,
            "errors": self.errors,
            "results": [r.to_dict() for r in self.results],
        }


class SearchService:
    def __init__(self, settings_getter: Callable[[], dict]):
        """settings_getter returns the *current* app settings dict, so
        Settings-dialog changes take effect on the next search without any
        restart or re-wiring."""
        self._settings_getter = settings_getter
        # requests.Session isn't documented thread-safe for concurrent use;
        # one session per worker thread via thread-local storage.
        self._local = threading.local()

    # -- provider wiring ----------------------------------------------------
    def _provider_settings(self) -> dict:
        return (self._settings_getter() or {}).get("search_providers", {}) or {}

    def enabled_providers(self) -> Dict[str, SearchProvider]:
        registry = available_providers()
        enabled: Dict[str, SearchProvider] = {}
        for name, settings in self._provider_settings().items():
            if not isinstance(settings, dict) or not settings.get("enabled", False):
                continue
            cls = registry.get(name)
            if cls is None:
                logger.warning(f"Unknown search provider in settings ignored: {name}")
                continue
            enabled[name] = cls(settings)
        return enabled

    def provider_infos(self) -> List[dict]:
        """Every registered provider with its enabled state -- for the GUI's
        provider filter and the /search/providers route."""
        provider_settings = self._provider_settings()
        infos = []
        for name, cls in sorted(available_providers().items()):
            settings = provider_settings.get(name, {})
            infos.append({
                "name": name,
                "kind": cls.kind,
                "enabled": bool(isinstance(settings, dict) and settings.get("enabled", False)),
                "capabilities": cls.capabilities.to_dict(),
            })
        return infos

    # -- searching ----------------------------------------------------------
    def search(self, query: SearchQuery) -> SearchOutcome:
        providers = self.enabled_providers()
        if query.providers:
            wanted = set(query.providers)
            providers = {n: p for n, p in providers.items() if n in wanted}

        outcome = SearchOutcome(providers_queried=sorted(providers))
        if not providers:
            return outcome

        def run(provider: SearchProvider) -> List[SearchResult]:
            return provider.search(self._session(), query)

        with ThreadPoolExecutor(
            max_workers=min(len(providers), MAX_CONCURRENT_PROVIDERS),
            thread_name_prefix="adp-search",
        ) as pool:
            futures = {name: pool.submit(run, p) for name, p in providers.items()}
            for name, future in futures.items():
                try:
                    outcome.results.extend(future.result())
                except ProviderError as e:
                    outcome.errors[name] = e.message
                except Exception as e:  # noqa: BLE001 -- one provider never kills the search
                    logger.exception(f"Search provider {name} raised unexpectedly")
                    outcome.errors[name] = f"unexpected error: {e!r}"

        outcome.results = rank(dedupe(outcome.results))[: query.limit]
        return outcome

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            self._local.session = session
        return session


# ---------------------------------------------------------------------------
# Dedupe + ranking (pure functions, unit-tested directly)
# ---------------------------------------------------------------------------
def dedupe(results: List[SearchResult]) -> List[SearchResult]:
    merged: Dict[str, SearchResult] = {}
    for result in results:
        key = result.identity()
        existing = merged.get(key)
        if existing is None:
            merged[key] = SearchResult(**{**result.__dict__, "sources": list(result.sources)})
            continue
        existing.sources.extend(result.sources)
        existing.seeders = max(existing.seeders, result.seeders)
        existing.leechers = max(existing.leechers, result.leechers)
        existing.magnet = existing.magnet or result.magnet
        existing.size_bytes = existing.size_bytes or result.size_bytes
        existing.published = existing.published or result.published
        existing.category = existing.category or result.category
    return list(merged.values())


def score(result: SearchResult) -> float:
    """Seeders dominate (log-scaled so 5000 vs 4000 doesn't drown everything
    else), recency helps with a one-year half-life, sub-16KiB "torrents" are
    penalized as near-certain junk/fakes, and independent corroboration --
    the same infohash reported by multiple providers -- earns a bonus."""
    value = math.log1p(max(result.seeders, 0)) * 10.0
    if result.published is not None:
        published = result.published
        # Defend against a naive datetime arriving from any source (a
        # provider we don't control, a directly-constructed result): mixing
        # naive and aware datetimes raises TypeError. Assume UTC if naive.
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        age_days = max((utcnow() - published).total_seconds() / 86400.0, 0.0)
        value += 5.0 * math.pow(0.5, age_days / _RECENCY_HALF_LIFE_DAYS)
    if result.size_bytes is not None and result.size_bytes < 16 * 1024:
        value -= 20.0
    value += 1.5 * (len({hit.provider for hit in result.sources}) - 1)
    return round(value, 4)


def rank(results: List[SearchResult]) -> List[SearchResult]:
    for result in results:
        result.score = score(result)
    return sorted(results, key=lambda r: (-r.score, r.title.lower()))
