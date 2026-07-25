# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
"""REST /search routes and controller search wiring.

Search deliberately bypasses GuiBridge (it never touches Qt objects), so
unlike the other API tests these need no Qt event loop, no panels, and no
background-thread request helper -- a plain TestClient works.
"""
import pytest
from fastapi.testclient import TestClient

from adp.api.auth import ApiKeyStore
from adp.api.controller import ApiError, AppController
from adp.api.rest_server import build_app
from adp.search.models import SearchResult, SourceHit
from adp.search.providers import ProviderError, SearchProvider
from adp.search.service import SearchService

HASH_A = "e" * 40


class StubProvider(SearchProvider):
    def __init__(self, name, results=None, error=None):
        super().__init__({})
        self.name = name
        self._results = results or []
        self._error = error

    def search(self, session, query):
        if self._error is not None:
            raise self._error
        return list(self._results)


def make_service(providers) -> SearchService:
    service = SearchService(lambda: {"search_providers": {}})
    service.enabled_providers = lambda: dict(providers)
    return service


def make_controller(providers) -> AppController:
    return AppController(
        bridge=None, download_panel=None, torrent_panel=None, stats_panel=None,
        search_service=make_service(providers),
    )


@pytest.fixture
def key_store(tmp_path):
    return ApiKeyStore(str(tmp_path / "keydir"))


def make_client(key_store, providers):
    controller = make_controller(providers)
    return TestClient(build_app(controller, key_store))


def stub_result() -> SearchResult:
    return SearchResult(
        title="debian-13.1.0-amd64-netinst.iso", infohash=HASH_A,
        seeders=55, leechers=3, size_bytes=700_000_000,
        sources=[SourceHit(provider="stub", seeders=55, leechers=3)],
    )


class TestControllerSearch:
    def test_search_returns_serialized_outcome(self):
        controller = make_controller({"stub": StubProvider("stub", [stub_result()])})
        payload = controller.search_torrents(text="debian")
        assert payload["query"] == "debian"
        assert payload["count"] == 1
        assert payload["results"][0]["infohash"] == HASH_A
        assert payload["results"][0]["magnet"].startswith("magnet:?xt=urn:btih:")

    def test_no_search_service_is_503(self):
        controller = AppController(bridge=None, download_panel=None,
                                    torrent_panel=None, stats_panel=None)
        with pytest.raises(ApiError) as excinfo:
            controller.search_torrents(text="x")
        assert excinfo.value.status_code == 503

    def test_no_enabled_providers_is_503(self):
        controller = make_controller({})
        with pytest.raises(ApiError) as excinfo:
            controller.search_torrents(text="x")
        assert excinfo.value.status_code == 503

    def test_empty_query_is_422(self):
        controller = make_controller({"stub": StubProvider("stub")})
        with pytest.raises(ApiError) as excinfo:
            controller.search_torrents(text="   ")
        assert excinfo.value.status_code == 422


class TestSearchRoutes:
    def test_search_requires_api_key(self, key_store):
        client = make_client(key_store, {"stub": StubProvider("stub", [stub_result()])})
        assert client.post("/search", json={"text": "debian"}).status_code == 401

    def test_search_route_end_to_end(self, key_store):
        client = make_client(key_store, {"stub": StubProvider("stub", [stub_result()])})
        response = client.post("/search", json={"text": "debian"},
                                headers={"X-API-Key": key_store.key})
        assert response.status_code == 200
        body = response.json()
        assert body["count"] == 1
        assert body["providers_queried"] == ["stub"]
        assert body["results"][0]["seeders"] == 55

    def test_provider_failure_reported_not_fatal(self, key_store):
        client = make_client(key_store, {
            "good": StubProvider("good", [stub_result()]),
            "bad": StubProvider("bad", error=ProviderError("bad", "down")),
        })
        body = client.post("/search", json={"text": "x"},
                            headers={"X-API-Key": key_store.key}).json()
        assert body["count"] == 1
        assert body["errors"] == {"bad": "down"}

    def test_search_providers_listing(self, key_store):
        client = make_client(key_store, {})
        response = client.get("/search/providers", headers={"X-API-Key": key_store.key})
        assert response.status_code == 200
        names = {p["name"] for p in response.json()}
        assert {"jackett", "torrents_csv"} <= names

    def test_body_validation(self, key_store):
        client = make_client(key_store, {"stub": StubProvider("stub")})
        headers = {"X-API-Key": key_store.key}
        assert client.post("/search", json={}, headers=headers).status_code == 422
        assert client.post("/search", json={"text": "x", "limit": 0},
                            headers=headers).status_code == 422


def test_mcp_server_exposes_search_tools():
    import anyio
    from adp.api.mcp_tools import build_mcp_server
    mcp = build_mcp_server(make_controller({"stub": StubProvider("stub", [stub_result()])}))
    tool_names = {t.name for t in anyio.run(mcp.list_tools)}
    assert {"search_torrents", "list_search_providers"} <= tool_names
