# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
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


# --- sortable / resizable columns ----------------------------------------

def _multi_result(title, seeders, size_bytes, infohash):
    return SearchResult(
        title=title, infohash=infohash, seeders=seeders, leechers=1,
        size_bytes=size_bytes,
        sources=[SourceHit(provider="stub", seeders=seeders, leechers=1)],
    )


def _panel_with_results(qtbot, results, torrent_panel=None):
    from adp.gui.search_panel import SearchPanel
    from adp.search.service import rank
    panel = SearchPanel(make_service({}), torrent_panel=torrent_panel)
    qtbot.addWidget(panel)
    panel._results = rank(results)
    panel._populate_table(panel._results)
    return panel


def test_columns_are_sortable_and_resizable(qtbot):
    from PyQt6.QtWidgets import QHeaderView
    panel = _panel_with_results(qtbot, [_multi_result("a", 5, 100, "a" * 40)])
    assert panel.table.isSortingEnabled()
    header = panel.table.horizontalHeader()
    # Numeric/text columns are Interactive (user-draggable), not fixed.
    for col in (panel.COL_SIZE, panel.COL_SEEDERS, panel.COL_LEECHERS, panel.COL_SOURCES):
        assert header.sectionResizeMode(col) == QHeaderView.ResizeMode.Interactive


def test_numeric_sort_orders_by_value_not_text(qtbot):
    from PyQt6.QtCore import Qt
    # Seeders 305 vs 1240: string sort would put 1240 < 305 ("1" < "3").
    results = [
        _multi_result("small-swarm", 305, 100, "a" * 40),
        _multi_result("big-swarm", 1240, 200, "b" * 40),
    ]
    panel = _panel_with_results(qtbot, results)
    panel.table.sortItems(panel.COL_SEEDERS, Qt.SortOrder.AscendingOrder)
    # Ascending: 305 first, then 1240 -- numerically, not "1240" before "305".
    top = panel.table.item(0, panel.COL_SEEDERS).text()
    bottom = panel.table.item(1, panel.COL_SEEDERS).text()
    assert (top, bottom) == ("305", "1240")


def test_size_sort_is_numeric_across_units(qtbot):
    from PyQt6.QtCore import Qt
    # 700 MB vs 4 GB: text sort of "4.56 GB" vs "667 MB" would be wrong.
    results = [
        _multi_result("bigfile", 10, 4_000_000_000, "a" * 40),
        _multi_result("smallfile", 10, 700_000_000, "b" * 40),
    ]
    panel = _panel_with_results(qtbot, results)
    panel.table.sortItems(panel.COL_SIZE, Qt.SortOrder.AscendingOrder)
    # Smaller bytes first regardless of unit label.
    assert "smallfile" in panel.table.item(0, panel.COL_TITLE).text()
    assert "bigfile" in panel.table.item(1, panel.COL_TITLE).text()


def test_add_after_sort_targets_the_correct_result(qtbot):
    """The critical regression: after sorting, the double-click / context-menu
    actions must resolve the result at the *visual* row, not treat the visual
    row as an index into the (differently-ordered) results list.

    rank() orders high-seed first, so _results = [high-seed(b), low-seed(a)].
    After an ascending sort, visual row 0 shows low-seed(a). The old code did
    add_result_to_torrents(item.row()) -> _results[0] -> high-seed(b), the
    WRONG result. The fix translates visual row -> stored result index."""
    from PyQt6.QtCore import Qt
    torrents = FakeTorrentPanel()
    results = [
        _multi_result("low-seed", 5, 100, "a" * 40),
        _multi_result("high-seed", 999, 200, "b" * 40),
    ]
    panel = _panel_with_results(qtbot, results, torrent_panel=torrents)
    # rank() puts high-seed first; _results[0] is high-seed (hash b).
    assert panel._results[0].infohash == "b" * 40

    # Sort ascending: visual row 0 is now low-seed (hash a).
    panel.table.sortItems(panel.COL_SEEDERS, Qt.SortOrder.AscendingOrder)
    assert "low-seed" in panel.table.item(0, panel.COL_TITLE).text()

    # Simulate the double-click path on visual row 0. It must add low-seed
    # (hash a), NOT _results[0] (high-seed, hash b) as the old code did.
    panel.add_result_to_torrents(panel._row_result_index(0))
    assert torrents.added == [("magnet", f"magnet:?xt=urn:btih:{'a' * 40}")]


def test_context_menu_index_after_sort(qtbot):
    from PyQt6.QtCore import Qt
    results = [
        _multi_result("low-seed", 5, 100, "a" * 40),
        _multi_result("high-seed", 999, 200, "b" * 40),
    ]
    panel = _panel_with_results(qtbot, results)
    panel.table.sortItems(panel.COL_SEEDERS, Qt.SortOrder.DescendingOrder)
    # Descending: high-seed at row 0. Its result index must resolve to hash b.
    idx = panel._row_result_index(0)
    assert panel._results[idx].infohash == "b" * 40
