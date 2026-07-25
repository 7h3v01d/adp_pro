# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
"""Search providers (against canned upstream payloads) and SearchService
fan-out behavior. No real network anywhere -- providers take the session as
a parameter precisely so tests can hand them a fake."""
import json

import pytest
import requests

from adp.search.models import SearchQuery, SearchResult, SourceHit
from adp.search.providers import (
    JackettProvider, ProviderError, SearchProvider, TorrentsCsvProvider,
    available_providers,
)
from adp.search.service import SearchService

HASH_A = "c" * 40
HASH_B = "d" * 40


class FakeResponse:
    def __init__(self, payload=None, status_code=200, body=b""):
        self._payload = payload
        self.status_code = status_code
        self._body = body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        if self._payload is None:
            raise ValueError("not JSON")
        return self._payload


class FakeSession:
    """Stands in for requests.Session -- records the call, returns a canned
    response or raises."""
    def __init__(self, payload=None, exc=None, body=b""):
        self.payload = payload
        self.exc = exc
        self.body = body
        self.last_url = None
        self.last_params = None

    def get(self, url, params=None, timeout=None):
        self.last_url = url
        self.last_params = params or {}
        if self.exc is not None:
            raise self.exc
        return FakeResponse(self.payload, body=self.body)


JACKETT_PAYLOAD = {
    "Results": [
        {
            "Title": "linuxmint-22-cinnamon-64bit.iso",
            "Tracker": "SomeTracker", "CategoryDesc": "PC/ISO",
            "PublishDate": "2026-05-01T10:00:00Z", "Size": 2_800_000_000,
            "Seeders": 120, "Peers": 14, "InfoHash": HASH_A.upper(),
            "MagnetUri": f"magnet:?xt=urn:btih:{HASH_A}&dn=mint",
            "Details": "https://tracker.example/details/1",
        },
        {"Title": "", "Seeders": 5},                        # dropped: no title
        {"Title": "no-hash entry", "Seeders": "notanint"},  # kept, tolerant ints
    ]
}

CSV_PAYLOAD = {
    "torrents": [
        {"infohash": HASH_A, "name": "debian-13.1.0-amd64-netinst.iso",
         "size_bytes": 700_000_000, "created_unix": 1750000000,
         "seeders": 55, "leechers": 3},
        {"infohash": "", "name": "dropped -- no hash"},
    ]
}


class TestJackettProvider:
    def test_parses_rows_and_drops_malformed(self):
        session = FakeSession(JACKETT_PAYLOAD)
        provider = JackettProvider({"base_url": "http://jack:9117", "api_key": "k"})
        results = provider.search(session, SearchQuery(text="mint"))
        assert "indexers/all/results" in session.last_url
        assert session.last_params["apikey"] == "k"
        assert len(results) == 2
        top = results[0]
        assert top.infohash == HASH_A
        assert top.seeders == 120 and top.leechers == 14
        assert top.sources[0].tracker == "SomeTracker"
        assert results[1].seeders == 0

    def test_missing_api_key_raises_provider_error(self):
        with pytest.raises(ProviderError):
            JackettProvider({}).search(FakeSession({}), SearchQuery(text="x"))

    def test_network_failure_wrapped_as_provider_error(self):
        session = FakeSession(exc=requests.ConnectionError("refused"))
        with pytest.raises(ProviderError):
            JackettProvider({"api_key": "k"}).search(session, SearchQuery(text="x"))

    def test_category_hint_mapped_to_torznab_number(self):
        session = FakeSession({"Results": []})
        JackettProvider({"api_key": "k"}).search(
            session, SearchQuery(text="x", category="software"))
        assert session.last_params["Category[]"] == "4000"


class TestTorrentsCsvProvider:
    def test_parses_rows_and_requires_hash(self):
        session = FakeSession(CSV_PAYLOAD)
        results = TorrentsCsvProvider({}).search(session, SearchQuery(text="debian"))
        assert session.last_params["q"] == "debian"
        assert len(results) == 1
        result = results[0]
        assert result.infohash == HASH_A
        assert result.published is not None and result.published.year >= 2025
        assert result.sources[0].provider == "torrents_csv"

    def test_non_json_upstream_wrapped_as_provider_error(self):
        session = FakeSession(payload=None)
        with pytest.raises(ProviderError):
            TorrentsCsvProvider({}).search(session, SearchQuery(text="x"))


def test_registry_contains_bundled_providers():
    assert {"jackett", "torrents_csv"} <= set(available_providers())


# ---------------------------------------------------------------------------
# SearchService fan-out
# ---------------------------------------------------------------------------
class StubProvider(SearchProvider):
    kind = "native"

    def __init__(self, name, results=None, error=None):
        super().__init__({})
        self.name = name
        self._results = results or []
        self._error = error

    def search(self, session, query):
        if self._error is not None:
            raise self._error
        return list(self._results)


def stub_result(infohash: str, seeders: int, provider: str) -> SearchResult:
    return SearchResult(
        title=f"result-{infohash[:6]}", infohash=infohash, seeders=seeders,
        sources=[SourceHit(provider=provider, seeders=seeders)],
    )


def service_with(providers: dict) -> SearchService:
    service = SearchService(lambda: {"search_providers": {}})
    service.enabled_providers = lambda: dict(providers)  # bypass registry wiring
    return service


class TestSearchService:
    def test_failing_provider_never_fails_the_search(self):
        service = service_with({
            "good": StubProvider("good", [stub_result(HASH_A, 10, "good")]),
            "bad": StubProvider("bad", error=ProviderError("bad", "upstream down")),
        })
        outcome = service.search(SearchQuery(text="x"))
        assert len(outcome.results) == 1
        assert outcome.errors == {"bad": "upstream down"}
        assert outcome.providers_queried == ["bad", "good"]

    def test_unexpected_exception_also_contained(self):
        service = service_with({
            "good": StubProvider("good", [stub_result(HASH_A, 10, "good")]),
            "boom": StubProvider("boom", error=RuntimeError("kaboom")),
        })
        outcome = service.search(SearchQuery(text="x"))
        assert len(outcome.results) == 1
        assert "boom" in outcome.errors

    def test_cross_provider_dedupe_and_ranking(self):
        service = service_with({
            "p1": StubProvider("p1", [stub_result(HASH_A, 10, "p1")]),
            "p2": StubProvider("p2", [stub_result(HASH_A, 30, "p2"),
                                       stub_result(HASH_B, 5, "p2")]),
        })
        outcome = service.search(SearchQuery(text="x"))
        assert len(outcome.results) == 2
        top = outcome.results[0]
        assert top.infohash == HASH_A and top.seeders == 30
        assert {hit.provider for hit in top.sources} == {"p1", "p2"}

    def test_provider_filter_restricts_fanout(self):
        service = service_with({
            "p1": StubProvider("p1", [stub_result(HASH_A, 10, "p1")]),
            "p2": StubProvider("p2", [stub_result(HASH_B, 10, "p2")]),
        })
        outcome = service.search(SearchQuery(text="x", providers=["p2"]))
        assert outcome.providers_queried == ["p2"]
        assert outcome.results[0].infohash == HASH_B

    def test_limit_applied_after_merge(self):
        results = [stub_result(f"{i:040x}", i, "p") for i in range(1, 30)]
        service = service_with({"p": StubProvider("p", results)})
        outcome = service.search(SearchQuery(text="x", limit=5))
        assert len(outcome.results) == 5

    def test_settings_wiring_respects_deny_first(self):
        service = SearchService(lambda: {"search_providers": {
            "torrents_csv": {"enabled": True},
            "jackett": {"enabled": False, "api_key": "k"},
            "unknown_provider": {"enabled": True},
        }})
        enabled = service.enabled_providers()
        assert set(enabled) == {"torrents_csv"}
        infos = {p["name"]: p["enabled"] for p in service.provider_infos()}
        assert infos["torrents_csv"] is True
        assert infos["jackett"] is False

    def test_outcome_to_dict_is_json_serializable(self):
        service = service_with({"p": StubProvider("p", [stub_result(HASH_A, 3, "p")])})
        payload = service.search(SearchQuery(text="x")).to_dict()
        json.dumps(payload)  # must not raise
        assert payload["count"] == 1
