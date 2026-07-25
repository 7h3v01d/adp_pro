"""Regression tests for the disk-full incident:

1. Signals carrying byte counts must survive values >2 GiB (the field bug:
   a 4.26 GB torrent arrived as -33776884 through a 32-bit int signal).
2. A torrent that won't fit must be paused at metadata time, not left to
   die mid-write.
"""
import pytest

pytestmark = pytest.mark.gui


class TestQint64Signals:
    """A plain pyqtSignal(int) marshals as C++ 32-bit; these must not wrap
    negative for a value past 2**31. We emit a >2GiB value and confirm the
    slot receives it intact."""

    def _roundtrip(self, qtbot, signal_owner, signal, value):
        received = []
        signal.connect(lambda *args: received.append(args))
        signal.emit(*value)
        qtbot.waitUntil(lambda: len(received) == 1, timeout=2000)
        return received[0]

    def test_torrent_metadata_signal_survives_large_size(self, qtbot):
        from adp.torrent.engine import TorrentEngine
        engine = TorrentEngine.__new__(TorrentEngine)  # no libtorrent session needed
        from PyQt6.QtCore import QObject, pyqtSignal

        class Holder(QObject):
            metadata_received = pyqtSignal(str, str, 'qint64', list)
        holder = Holder()
        big = 4_261_190_412  # the exact size from the incident log
        args = self._roundtrip(qtbot, holder, holder.metadata_received,
                                ("hash", "Big Torrent", big, []))
        assert args[2] == big  # not negative, not truncated

    def test_download_progress_signal_survives_large_size(self, qtbot):
        from PyQt6.QtCore import QObject, pyqtSignal

        class Holder(QObject):
            progress_updated = pyqtSignal(str, 'qint64', 'qint64', float, str)
        holder = Holder()
        downloaded, total = 3_000_000_000, 6_000_000_000
        args = self._roundtrip(qtbot, holder, holder.progress_updated,
                                ("id", downloaded, total, 1.0, "Downloading"))
        assert args[1] == downloaded and args[2] == total


@pytest.mark.torrent
class TestTorrentPanelSpaceGuard:
    """Uses a real (offline) TorrentPanel -- listen_port=0, DHT off, no peers
    -- and drives on_metadata_received() directly. No swarm needed: the guard
    is pure logic over check_space(), which is monkeypatched. Marked 'torrent'
    only because constructing the panel spins up a libtorrent session."""

    @pytest.fixture
    def panel(self, qtbot, tmp_path):
        pytest.importorskip("libtorrent")
        from adp.gui.torrent_panel import TorrentPanel
        p = TorrentPanel(state_dir=str(tmp_path), listen_port=0, enable_dht=False)
        qtbot.addWidget(p)
        yield p
        for torrent_id in list(p.engine.handles.keys()):
            p.engine.remove(torrent_id, delete_files=False)

    def test_pauses_when_insufficient_space(self, qtbot, tmp_path, monkeypatch, panel):
        import adp.gui.torrent_panel as tp
        from adp.core.storage import SpaceCheck
        monkeypatch.setattr(tp, "check_space",
                            lambda *a, **k: SpaceCheck(False, 1024, 5 * 1024**3, 512 * 1024**2))
        paused = []
        monkeypatch.setattr(panel.engine, "pause", lambda tid: paused.append(tid))
        statuses = []
        panel.status_update_requested.connect(lambda msg, ms: statuses.append(msg))

        panel.on_metadata_received("tid-1", "Huge", 5 * 1024**3, [])
        assert paused == ["tid-1"]
        assert statuses and "not enough disk space" in statuses[0]

    def test_allows_when_space_is_fine(self, qtbot, tmp_path, monkeypatch, panel):
        import adp.gui.torrent_panel as tp
        from adp.core.storage import SpaceCheck
        monkeypatch.setattr(tp, "check_space",
                            lambda *a, **k: SpaceCheck(True, 10 * 1024**3, 1024, 512 * 1024**2))
        paused = []
        monkeypatch.setattr(panel.engine, "pause", lambda tid: paused.append(tid))
        panel.on_metadata_received("tid-2", "Small", 1024, [])
        assert paused == []

    def test_set_default_save_path_updates_target(self, tmp_path, panel):
        import os
        new_dir = str(tmp_path / "elsewhere")
        panel.set_default_save_path(new_dir)
        assert panel.default_save_path == new_dir
        assert os.path.isdir(new_dir)
