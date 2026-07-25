import os
import time

import pytest

lt = pytest.importorskip("libtorrent", reason="libtorrent not installed; run `pip install libtorrent`")

from PyQt6.QtCore import Qt

from adp.gui.torrent_panel import TorrentPanel
from adp.gui.torrent_dialogs import AddTorrentDialog, FileSelectionWidget
from adp.torrent.models import TorrentState

pytestmark = pytest.mark.torrent


def pump(qtbot, condition, timeout=20000):
    qtbot.waitUntil(condition, timeout=timeout)


@pytest.fixture
def torrent_panel(qtbot, tmp_path):
    panel = TorrentPanel(state_dir=str(tmp_path), listen_port=0, enable_dht=False)
    qtbot.addWidget(panel)
    yield panel
    for torrent_id in list(panel.engine.handles.keys()):
        panel.engine.remove(torrent_id, delete_files=False)


def test_seed_ratio_limit_auto_pauses_when_reached(qtbot, torrent_panel, local_seed, tmp_path):
    """Regression test: seed_ratio_limit was accepted in the Add Torrent
    dialog and stored on the record, but nothing ever enforced it -- a
    torrent would seed forever regardless of the configured limit."""
    content = os.urandom(10_000)
    torrent_bytes = local_seed.seed_file("ratio_test.bin", content)
    torrent_path = str(tmp_path / "ratio_test.bin.torrent")
    local_seed.write_torrent_file(torrent_bytes, torrent_path)

    save_path = str(tmp_path / "leech_ratio")
    torrent_id = torrent_panel.add_torrent(
        mode="file", torrent_file_path=torrent_path, save_path=save_path, seed_ratio_limit=2.0,
    )
    torrent_panel.engine.connect_peer(torrent_id, "127.0.0.1", local_seed.port)

    finished = {}
    torrent_panel.torrent_completed.connect(lambda tid, name: finished.update(done=True) if tid == torrent_id else None)
    pump(qtbot, lambda: bool(finished))

    handle = torrent_panel.engine.handles[torrent_id]
    assert not handle.status().paused  # not yet over the ratio limit

    # Simulate having uploaded enough to cross the configured ratio -- doing
    # this for real would require a second peer actually pulling data back
    # from our completed download, which real network conditions can't
    # reliably guarantee inside a fast, deterministic test.
    fake_status = {"is_seeding": True, "ratio": 3.0}
    torrent_panel._enforce_seed_ratio_limit(torrent_id, fake_status)
    qtbot.waitUntil(lambda: handle.status().paused, timeout=5000)


def test_seed_ratio_limit_of_zero_means_unlimited(qtbot, torrent_panel, local_seed, tmp_path):
    content = os.urandom(10_000)
    torrent_bytes = local_seed.seed_file("unlimited.bin", content)
    torrent_path = str(tmp_path / "unlimited.bin.torrent")
    local_seed.write_torrent_file(torrent_bytes, torrent_path)

    save_path = str(tmp_path / "leech_unlimited")
    torrent_id = torrent_panel.add_torrent(
        mode="file", torrent_file_path=torrent_path, save_path=save_path, seed_ratio_limit=0.0,
    )
    torrent_panel.engine.connect_peer(torrent_id, "127.0.0.1", local_seed.port)

    finished = {}
    torrent_panel.torrent_completed.connect(lambda tid, name: finished.update(done=True) if tid == torrent_id else None)
    pump(qtbot, lambda: bool(finished))

    handle = torrent_panel.engine.handles[torrent_id]
    torrent_panel._enforce_seed_ratio_limit(torrent_id, {"is_seeding": True, "ratio": 1000.0})
    time.sleep(0.3)
    qtbot.wait(50)
    assert not handle.status().paused


def test_panel_starts_empty(torrent_panel):
    assert torrent_panel.torrent_list.count() == 0
    assert torrent_panel.category_filter.itemText(0) == "All Categories"


def test_add_torrent_file_and_download_completes(qtbot, torrent_panel, local_seed, tmp_path):
    content = os.urandom(200_000)
    torrent_bytes = local_seed.seed_file("show.mp4", content)
    torrent_path = str(tmp_path / "show.mp4.torrent")
    local_seed.write_torrent_file(torrent_bytes, torrent_path)

    save_path = str(tmp_path / "leech_out")
    os.makedirs(save_path, exist_ok=True)
    torrent_id = torrent_panel.add_torrent(mode="file", torrent_file_path=torrent_path, save_path=save_path)
    assert torrent_panel.torrent_list.count() == 1

    torrent_panel.engine.connect_peer(torrent_id, "127.0.0.1", local_seed.port)

    finished = {}
    torrent_panel.torrent_completed.connect(lambda tid, name: finished.update(done=True) if tid == torrent_id else None)
    pump(qtbot, lambda: bool(finished))

    with open(os.path.join(save_path, "show.mp4"), "rb") as f:
        assert f.read() == content

    widget = torrent_panel.find_widget(torrent_id)
    assert widget is not None
    assert "show.mp4" in widget.name_label.text()


def test_category_auto_assigned_and_filterable(qtbot, torrent_panel, local_seed, tmp_path):
    content = os.urandom(10_000)
    torrent_bytes = local_seed.seed_file("archive.zip", content)
    torrent_path = str(tmp_path / "archive.zip.torrent")
    local_seed.write_torrent_file(torrent_bytes, torrent_path)

    save_path = str(tmp_path / "leech_out2")
    torrent_id = torrent_panel.add_torrent(
        mode="file", torrent_file_path=torrent_path, save_path=save_path, category="Archives"
    )
    assert torrent_panel.category_filter.findText("Archives") >= 0

    idx = torrent_panel.category_filter.findText("Archives")
    torrent_panel.category_filter.setCurrentIndex(idx)
    item = torrent_panel.torrent_list.item(0)
    assert not item.isHidden()

    torrent_panel.category_filter.addItem("Video")
    torrent_panel.category_filter.setCurrentText("Video")
    assert item.isHidden()


def test_remove_torrent_removes_list_item(qtbot, torrent_panel, local_seed, tmp_path):
    content = os.urandom(10_000)
    torrent_bytes = local_seed.seed_file("removeme.bin", content)
    torrent_path = str(tmp_path / "removeme.bin.torrent")
    local_seed.write_torrent_file(torrent_bytes, torrent_path)

    save_path = str(tmp_path / "leech_out3")
    torrent_id = torrent_panel.add_torrent(mode="file", torrent_file_path=torrent_path, save_path=save_path)
    assert torrent_panel.torrent_list.count() == 1

    torrent_panel.torrent_list.setCurrentRow(0)
    torrent_panel.remove_selected(delete_files=False)
    assert torrent_panel.torrent_list.count() == 0
    assert torrent_id not in torrent_panel.records


def test_session_persistence_round_trip(qtbot, tmp_path, local_seed):
    content = os.urandom(10_000)
    torrent_bytes = local_seed.seed_file("persisted.bin", content)
    torrent_path = str(tmp_path / "persisted.bin.torrent")
    local_seed.write_torrent_file(torrent_bytes, torrent_path)

    state_dir = str(tmp_path / "state")
    save_path = str(tmp_path / "leech_out4")
    panel1 = TorrentPanel(state_dir=state_dir, listen_port=0, enable_dht=False)
    torrent_id = panel1.add_torrent(
        mode="file", torrent_file_path=torrent_path, save_path=save_path, category="Video"
    )
    panel1.save_session(wait_for_resume_data=False)
    panel1.engine.stop()

    panel2 = TorrentPanel(state_dir=state_dir, listen_port=0, enable_dht=False)
    qtbot.addWidget(panel2)
    assert panel2.torrent_list.count() == 1
    restored_id = panel2.torrent_list.item(0).data(Qt.ItemDataRole.UserRole)
    assert panel2.records[restored_id].category == "Video"
    for tid in list(panel2.engine.handles.keys()):
        panel2.engine.remove(tid, delete_files=False)


def test_add_torrent_dialog_prefills_default_seed_ratio(qtbot):
    dialog = AddTorrentDialog(default_seed_ratio_limit=1.5)
    qtbot.addWidget(dialog)
    assert dialog.seed_ratio_input.text() == "1.5"


def test_add_torrent_dialog_rejects_invalid_magnet(qtbot):
    dialog = AddTorrentDialog()
    qtbot.addWidget(dialog)
    dialog.magnet_input.setText("not a magnet link")
    dialog._on_accept()
    assert dialog._error == "invalid_magnet"


def test_add_torrent_dialog_accepts_valid_magnet(qtbot):
    dialog = AddTorrentDialog()
    qtbot.addWidget(dialog)
    dialog.magnet_input.setText("magnet:?xt=urn:btih:" + "a" * 40)
    dialog._on_accept()
    assert dialog._error is None
    data = dialog.get_data()
    assert data["mode"] == "magnet"


def test_add_torrent_dialog_previews_torrent_file(qtbot, tmp_path):
    seed_dir = str(tmp_path / "preview_seed")
    os.makedirs(seed_dir, exist_ok=True)
    with open(os.path.join(seed_dir, "content.iso"), "wb") as f:
        f.write(os.urandom(20_000))
    fs = lt.file_storage()
    lt.add_files(fs, os.path.join(seed_dir, "content.iso"))
    ct = lt.create_torrent(fs)
    lt.set_piece_hashes(ct, seed_dir)
    torrent_path = str(tmp_path / "content.torrent")
    with open(torrent_path, "wb") as f:
        f.write(lt.bencode(ct.generate()))

    dialog = AddTorrentDialog()
    qtbot.addWidget(dialog)
    dialog.file_path_input.setText(torrent_path)
    dialog.file_radio.setChecked(True)
    # Simulate what browse_torrent_file does without opening a real file dialog:
    from adp.torrent.engine import TorrentEngine
    entries = TorrentEngine.preview_torrent_file(torrent_path)
    dialog.file_selection.set_entries(entries)
    dialog.file_selection.setVisible(True)

    assert dialog.file_selection.list_widget.count() == 1
    priorities = dialog.file_selection.get_priorities()
    assert list(priorities.values())[0] == 4  # normal/selected by default


def test_force_recheck_on_paused_torrent_re_verifies_data(qtbot, torrent_panel, local_seed, tmp_path):
    """Regression: force-rechecking a *paused* torrent used to leave it stuck
    at 0%. libtorrent won't run a recheck while paused -- force_recheck()
    queues the check but it never processes, and progress collapses to 0 and
    stays there. The engine now resumes a paused torrent before rechecking so
    the verification actually runs and the data re-verifies to 100%."""
    content = os.urandom(200_000)
    torrent_bytes = local_seed.seed_file("recheck.bin", content)
    torrent_path = str(tmp_path / "recheck.bin.torrent")
    local_seed.write_torrent_file(torrent_bytes, torrent_path)

    save_path = str(tmp_path / "leech_recheck")
    torrent_id = torrent_panel.add_torrent(
        mode="file", torrent_file_path=torrent_path, save_path=save_path)
    torrent_panel.engine.connect_peer(torrent_id, "127.0.0.1", local_seed.port)

    handle = torrent_panel.engine.handles[torrent_id]
    pump(qtbot, lambda: handle.status().is_finished)
    assert handle.status().progress == 1.0

    # Pause, then force recheck -- the exact flow that used to break.
    torrent_panel.engine.pause(torrent_id)
    qtbot.waitUntil(lambda: handle.status().paused, timeout=5000)

    issued = torrent_panel.engine.force_recheck(torrent_id)
    assert issued is True

    # The data must re-verify back to 100%, not get stuck at 0%.
    qtbot.waitUntil(
        lambda: int(handle.status().state) != 1 and handle.status().progress >= 1.0,
        timeout=15000,
    )
    assert handle.status().progress == 1.0


def test_force_recheck_active_torrent_still_verifies(qtbot, torrent_panel, local_seed, tmp_path):
    """A torrent that's already active is rechecked in place and stays at
    100% -- the fix for the paused case must not regress the normal case."""
    content = os.urandom(200_000)
    torrent_bytes = local_seed.seed_file("recheck_active.bin", content)
    torrent_path = str(tmp_path / "recheck_active.bin.torrent")
    local_seed.write_torrent_file(torrent_bytes, torrent_path)

    save_path = str(tmp_path / "leech_recheck_active")
    torrent_id = torrent_panel.add_torrent(
        mode="file", torrent_file_path=torrent_path, save_path=save_path)
    torrent_panel.engine.connect_peer(torrent_id, "127.0.0.1", local_seed.port)
    handle = torrent_panel.engine.handles[torrent_id]
    pump(qtbot, lambda: handle.status().is_finished)

    assert torrent_panel.engine.force_recheck(torrent_id) is True
    qtbot.waitUntil(
        lambda: int(handle.status().state) != 1 and handle.status().progress >= 1.0,
        timeout=15000,
    )
    assert handle.status().progress == 1.0


def test_force_recheck_unknown_torrent_returns_false(torrent_panel):
    assert torrent_panel.engine.force_recheck("nonexistent-id") is False
