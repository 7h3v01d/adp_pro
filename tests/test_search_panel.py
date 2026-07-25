"""Search tab GUI tests -- worker threading, table population, and the
add-to-torrents path. No real network: the service gets stub providers.
"""
import pytest

from adp.search.models import SearchResult, SourceHit
from adp.search.providers import ProviderError, SearchProvider
from adp.search.service import SearchService
from adp.gui.search_panel import SearchPanel

pytestmark = pytest.mark.gui

HASH_A = "f" * 40


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


class FakeTorrentPanel:
    def __init__(self):
        self.added = []

    def add_torrent(self, mode, magnet_uri=None, **kwargs):
        self.added.append((mode, magnet_uri))
        return "torrent-1"


def stub_result(title="debian-13.1.0-amd64-netinst.iso", seeders=55) -> SearchResult:
    return SearchResult(
        title=title, infohash=HASH_A, seeders=seeders, leechers=3,
        size_bytes=700_000_000,
        sources=[SourceHit(provider="stub", seeders=seeders, leechers=3)],
    )


def make_service(providers) -> SearchService:
    service = SearchService(lambda: {"search_providers": {}})
    service.enabled_providers = lambda: dict(providers)
    return service


def make_panel(qtbot, providers, torrent_panel=None) -> SearchPanel:
    panel = SearchPanel(make_service(providers), torrent_panel=torrent_panel)
    qtbot.addWidget(panel)
    return panel


def run_search(qtbot, panel, text="debian"):
    panel.query_edit.setText(text)
    panel.start_search()
    qtbot.waitUntil(lambda: panel.search_button.isEnabled(), timeout=10000)


def test_search_populates_table(qtbot):
    panel = make_panel(qtbot, {"stub": StubProvider("stub", [stub_result()])})
    run_search(qtbot, panel)
    assert panel.table.rowCount() == 1
    assert "debian" in panel.table.item(0, panel.COL_TITLE).text()
    assert panel.table.item(0, panel.COL_SEEDERS).text() == "55"
    assert "1 results from stub" in panel.result_summary.text()
    assert panel._worker_refs == []  # worker released after finished()


def test_provider_error_shown_but_results_still_render(qtbot):
    panel = make_panel(qtbot, {
        "good": StubProvider("good", [stub_result()]),
        "bad": StubProvider("bad", error=ProviderError("bad", "upstream down")),
    })
    run_search(qtbot, panel)
    assert panel.table.rowCount() == 1
    assert "bad failed: upstream down" in panel.result_summary.text()


def test_add_button_hands_magnet_to_torrent_panel(qtbot):
    torrents = FakeTorrentPanel()
    panel = make_panel(qtbot, {"stub": StubProvider("stub", [stub_result()])},
                        torrent_panel=torrents)
    run_search(qtbot, panel)
    panel.add_result_to_torrents(0)
    assert torrents.added == [("magnet", f"magnet:?xt=urn:btih:{HASH_A}")]


def test_add_disabled_without_torrent_support(qtbot):
    panel = make_panel(qtbot, {"stub": StubProvider("stub", [stub_result()])},
                        torrent_panel=None)
    run_search(qtbot, panel)
    button = panel.table.cellWidget(0, panel.COL_ADD)
    assert button is not None and not button.isEnabled()
    panel.add_result_to_torrents(0)  # must be a no-op, not a crash


def test_empty_query_does_not_search(qtbot):
    panel = make_panel(qtbot, {"stub": StubProvider("stub", [stub_result()])})
    panel.query_edit.setText("   ")
    panel.start_search()
    assert panel.search_button.isEnabled()
    assert panel.table.rowCount() == 0


def test_no_enabled_providers_message(qtbot):
    panel = make_panel(qtbot, {})
    run_search(qtbot, panel)
    assert "No search providers are enabled" in panel.result_summary.text()
