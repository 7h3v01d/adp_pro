# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
import os
import time

import pytest

lt = pytest.importorskip("libtorrent", reason="libtorrent not installed; run `pip install libtorrent`")

from PyQt6.QtCore import Qt

from adp.gui.torrent_dialogs import AddTorrentDialog, FileSelectionWidget
from adp.torrent.models import TorrentState

pytestmark = pytest.mark.torrent


def pump(qtbot, condition, timeout=20000):
    qtbot.waitUntil(condition, timeout=timeout)


@pytest.fixture
def torrent_panel(make_torrent_panel, tmp_path):
    return make_torrent_panel(state_dir=str(tmp_path))


def test_seed_ratio_limit_auto_pauses_when_reached(qtbot, torrent_panel, local_seed, tmp_path):
    """Regression test: seed_ratio_limit was accepted in the Add Torrent
    dialog and stored on the record, but nothing ever enforced it -- a
    torrent would seed forever regardless of the configured limit."""
    torrent_panel.set_stop_on_complete(False)  # isolate ratio behaviour from stop-on-complete
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
    torrent_panel.set_stop_on_complete(False)  # isolate ratio behaviour from stop-on-complete
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


def test_session_persistence_round_trip(qtbot, tmp_path, local_seed, make_torrent_panel):
    content = os.urandom(10_000)
    torrent_bytes = local_seed.seed_file("persisted.bin", content)
    torrent_path = str(tmp_path / "persisted.bin.torrent")
    local_seed.write_torrent_file(torrent_bytes, torrent_path)

    state_dir = str(tmp_path / "state")
    save_path = str(tmp_path / "leech_out4")
    panel1 = make_torrent_panel(add_widget=False, state_dir=state_dir)
    torrent_id = panel1.add_torrent(
        mode="file", torrent_file_path=torrent_path, save_path=save_path, category="Video"
    )
    panel1.save_session(wait_for_resume_data=False)
    panel1.engine.shutdown()

    panel2 = make_torrent_panel(state_dir=state_dir)
    assert panel2.torrent_list.count() == 1
    restored_id = panel2.torrent_list.item(0).data(Qt.ItemDataRole.UserRole)
    assert panel2.records[restored_id].category == "Video"


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


def test_unavailable_torrent_storage_blocks_add_not_falls_back(qtbot, tmp_path, make_torrent_panel):
    """Fail-closed torrent storage: when the configured folder is unavailable,
    the panel must NOT silently redirect to a default -- Add is blocked and the
    missing path is surfaced. Regression: window.py used to catch the
    ConfiguredPathUnavailableError and quietly fall back to the system drive."""
    panel = make_torrent_panel(state_dir=str(tmp_path), unavailable_path="E:\\Torrents")
    assert panel.default_save_path is None

    messages = []
    panel.status_update_requested.connect(lambda msg, *_: messages.append(msg))
    # Attempting to add with no explicit save_path must be refused, not
    # redirected to the app-data default.
    result = panel.add_torrent(mode="magnet", magnet_uri="magnet:?xt=urn:btih:" + "a" * 40)
    assert result is None
    assert any("unavailable" in m.lower() and "E:\\Torrents" in m for m in messages)


def test_setting_valid_path_recovers_unavailable_storage(qtbot, tmp_path, make_torrent_panel):
    """Setting a usable default path clears the unavailable state -- how the
    user recovers after reconnecting a drive / fixing Settings."""
    panel = make_torrent_panel(state_dir=str(tmp_path), unavailable_path="E:\\Torrents")
    recovered = str(tmp_path / "recovered")
    panel.set_default_save_path(recovered)
    assert panel._unavailable_path is None
    assert panel.default_save_path == recovered


# -- stop on complete -----------------------------------------------------------
def _download_to_completion(qtbot, panel, local_seed, tmp_path, name):
    content = os.urandom(10_000)
    torrent_bytes = local_seed.seed_file(name, content)
    torrent_path = str(tmp_path / f"{name}.torrent")
    local_seed.write_torrent_file(torrent_bytes, torrent_path)
    torrent_id = panel.add_torrent(
        mode="file", torrent_file_path=torrent_path, save_path=str(tmp_path / f"leech_{name}"),
    )
    panel.engine.connect_peer(torrent_id, "127.0.0.1", local_seed.port)
    finished = {}
    panel.torrent_completed.connect(
        lambda tid, _n: finished.update(done=True) if tid == torrent_id else None)
    pump(qtbot, lambda: bool(finished))
    return torrent_id


def test_stop_on_complete_stops_torrent_at_100_percent(qtbot, torrent_panel, local_seed, tmp_path):
    """The user's request: by default a torrent must stop when its download
    completes instead of seeding forever."""
    assert torrent_panel.stop_on_complete is True  # default on
    torrent_id = _download_to_completion(qtbot, torrent_panel, local_seed, tmp_path, "stop.bin")
    handle = torrent_panel.engine.handles[torrent_id]
    qtbot.waitUntil(lambda: handle.status().paused, timeout=5000)
    status = handle.status()
    assert status.is_finished and status.progress == pytest.approx(1.0)
    # Shown as Finished, not Paused (it isn't an interrupted download).
    assert torrent_panel.engine._status_dict(status)["state"] == TorrentState.FINISHED


def test_stop_on_complete_off_keeps_seeding(qtbot, torrent_panel, local_seed, tmp_path):
    torrent_panel.set_stop_on_complete(False)
    torrent_id = _download_to_completion(qtbot, torrent_panel, local_seed, tmp_path, "seed.bin")
    qtbot.wait(1500)
    assert not torrent_panel.engine.handles[torrent_id].status().paused


def test_manual_resume_after_complete_keeps_seeding(qtbot, torrent_panel, local_seed, tmp_path):
    """Resuming a completed torrent is a deliberate 'seed this one' -- the
    global setting must not re-stop it (also persisted for restarts)."""
    torrent_id = _download_to_completion(qtbot, torrent_panel, local_seed, tmp_path, "keep.bin")
    handle = torrent_panel.engine.handles[torrent_id]
    qtbot.waitUntil(lambda: handle.status().paused, timeout=5000)

    torrent_panel.resume_torrent(torrent_id)
    qtbot.waitUntil(lambda: not handle.status().paused, timeout=5000)
    assert torrent_panel.records[torrent_id].keep_seeding is True
    # A later finished alert (e.g. after a recheck or restart) is ignored.
    assert torrent_panel._enforce_stop_on_complete(torrent_id) is False
    qtbot.wait(500)
    assert not handle.status().paused


def test_enabling_setting_stops_already_seeding_torrents(qtbot, torrent_panel, local_seed, tmp_path):
    torrent_panel.set_stop_on_complete(False)
    torrent_id = _download_to_completion(qtbot, torrent_panel, local_seed, tmp_path, "sweep.bin")
    handle = torrent_panel.engine.handles[torrent_id]
    qtbot.wait(500)
    assert not handle.status().paused

    torrent_panel.set_stop_on_complete(True)
    qtbot.waitUntil(lambda: handle.status().paused, timeout=5000)


# -- confirmations & multi-select ------------------------------------------------
def _add_unseeded(panel, tmp_path, name):
    """A torrent nobody seeds: it stays incomplete for the whole test."""
    src = tmp_path / f"src_{name}"
    src.mkdir()
    (src / name).write_bytes(os.urandom(20_000))
    fs = lt.file_storage()
    lt.add_files(fs, str(src / name))
    ct = lt.create_torrent(fs)
    lt.set_piece_hashes(ct, str(src))
    torrent_path = str(tmp_path / f"{name}.torrent")
    with open(torrent_path, "wb") as f:
        f.write(lt.bencode(ct.generate()))
    return panel.add_torrent(mode="file", torrent_file_path=torrent_path,
                             save_path=str(tmp_path / f"leech_{name}"))


def _select(panel, torrent_ids):
    panel.torrent_list.clearSelection()
    for i in range(panel.torrent_list.count()):
        item = panel.torrent_list.item(i)
        if item.data(Qt.ItemDataRole.UserRole) in torrent_ids:
            item.setSelected(True)


class _Prompts:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def __call__(self, title, text):
        self.calls.append((title, text))
        return self.answer


def test_delete_files_asks_first_and_no_keeps_everything(qtbot, torrent_panel, local_seed, tmp_path, monkeypatch):
    torrent_id = _download_to_completion(qtbot, torrent_panel, local_seed, tmp_path, "keepme.bin")
    data_file = tmp_path / "leech_keepme.bin" / "keepme.bin"
    assert data_file.exists()
    prompts = _Prompts(answer=False)
    monkeypatch.setattr(torrent_panel, "_confirm", prompts)

    _select(torrent_panel, {torrent_id})
    assert torrent_panel.remove_selected(delete_files=True) == 0
    assert len(prompts.calls) == 1 and "keepme.bin" in prompts.calls[0][1]
    assert torrent_id in torrent_panel.records
    assert torrent_panel.torrent_list.count() == 1
    assert data_file.exists()


def test_delete_files_confirmed_removes_and_deletes(qtbot, torrent_panel, local_seed, tmp_path, monkeypatch):
    torrent_id = _download_to_completion(qtbot, torrent_panel, local_seed, tmp_path, "goner.bin")
    data_file = tmp_path / "leech_goner.bin" / "goner.bin"
    monkeypatch.setattr(torrent_panel, "_confirm", _Prompts(answer=True))

    _select(torrent_panel, {torrent_id})
    assert torrent_panel.remove_selected(delete_files=True) == 1
    assert torrent_id not in torrent_panel.records
    qtbot.waitUntil(lambda: not data_file.exists(), timeout=10000)


def test_single_remove_keep_files_does_not_prompt(qtbot, torrent_panel, tmp_path, monkeypatch):
    torrent_id = _add_unseeded(torrent_panel, tmp_path, "solo.bin")
    prompts = _Prompts(answer=False)
    monkeypatch.setattr(torrent_panel, "_confirm", prompts)
    _select(torrent_panel, {torrent_id})
    assert torrent_panel.remove_selected(delete_files=False) == 1
    assert prompts.calls == []


def test_multi_select_pause_and_remove(qtbot, torrent_panel, tmp_path, monkeypatch):
    ids = [_add_unseeded(torrent_panel, tmp_path, f"multi{i}.bin") for i in range(3)]
    _select(torrent_panel, set(ids[:2]))
    assert torrent_panel.get_selected_torrent_ids() == ids[:2]

    # A user pause takes a torrent out of auto-management (see engine.pause);
    # plain status().paused can't tell that apart from libtorrent's own idle
    # queueing of peerless torrents, so check the flag.
    handles = torrent_panel.engine.handles
    managed = lambda t: bool(handles[t].flags() & lt.torrent_flags.auto_managed)  # noqa: E731

    torrent_panel.pause_selected()
    qtbot.waitUntil(lambda: all(handles[t].status().paused for t in ids[:2]), timeout=5000)
    assert not managed(ids[0]) and not managed(ids[1])
    assert managed(ids[2])  # unselected torrent untouched

    torrent_panel.resume_selected()
    assert managed(ids[0]) and managed(ids[1])

    prompts = _Prompts(answer=True)
    monkeypatch.setattr(torrent_panel, "_confirm", prompts)
    assert torrent_panel.remove_selected(delete_files=False) == 2
    assert len(prompts.calls) == 1  # one prompt for the batch
    assert list(torrent_panel.records) == [ids[2]]
    assert torrent_panel.torrent_list.count() == 1


def test_selection_skips_rows_hidden_by_search(qtbot, torrent_panel, tmp_path):
    a = _add_unseeded(torrent_panel, tmp_path, "alpha.bin")
    b = _add_unseeded(torrent_panel, tmp_path, "bravo.bin")
    torrent_panel.records[a].name = "alpha.bin"
    torrent_panel.records[b].name = "bravo.bin"
    torrent_panel.search_input.setText("alpha")
    torrent_panel.torrent_list.selectAll()
    assert torrent_panel.get_selected_torrent_ids() == [a]


def test_remove_completed_only_removes_finished(qtbot, torrent_panel, local_seed, tmp_path, monkeypatch):
    done = _download_to_completion(qtbot, torrent_panel, local_seed, tmp_path, "done.bin")
    pending = _add_unseeded(torrent_panel, tmp_path, "pending.bin")
    prompts = _Prompts(answer=True)
    monkeypatch.setattr(torrent_panel, "_confirm", prompts)

    assert torrent_panel.remove_completed() == 1
    assert len(prompts.calls) == 1
    assert done not in torrent_panel.records and pending in torrent_panel.records
    assert (tmp_path / "leech_done.bin" / "done.bin").exists()  # files kept


def test_bulk_resume_leaves_completed_stopped_start_seeding_seeds(qtbot, torrent_panel, local_seed, tmp_path):
    done = _download_to_completion(qtbot, torrent_panel, local_seed, tmp_path, "bulk.bin")
    pending = _add_unseeded(torrent_panel, tmp_path, "bulkpend.bin")
    handles = torrent_panel.engine.handles
    qtbot.waitUntil(lambda: handles[done].status().paused, timeout=5000)  # stop-on-complete
    torrent_panel.engine.pause(pending)
    qtbot.waitUntil(lambda: handles[pending].status().paused, timeout=5000)

    _select(torrent_panel, {done, pending})
    torrent_panel.resume_selected()
    assert handles[pending].flags() & lt.torrent_flags.auto_managed  # resumed
    qtbot.wait(300)
    assert handles[done].status().paused
    assert torrent_panel.records[done].keep_seeding is False

    torrent_panel.start_seeding_selected()
    qtbot.waitUntil(lambda: not handles[done].status().paused, timeout=5000)
    assert torrent_panel.records[done].keep_seeding is True


# -- properties -------------------------------------------------------------------
from adp.gui.torrent_dialogs import TorrentPropertiesDialog  # noqa: E402
from adp.torrent.models import TorrentRecord  # noqa: E402


def _record(**kw):
    base = dict(torrent_id="ab" * 20, name="movie.mkv", save_path="D:/Torrents", category="Video")
    base.update(kw)
    return TorrentRecord(**base)


def test_properties_dialog_prefills_from_record(qtbot):
    rec = _record(download_limit_bps=2 * 1024 * 1024, upload_limit_bps=512 * 1024,
                  seed_ratio_limit=1.5, keep_seeding=True, category="My Shows")
    dialog = TorrentPropertiesDialog(record=rec, categories=["Video", "My Shows"])
    qtbot.addWidget(dialog)
    assert dialog.category_input.currentText() == "My Shows"
    assert dialog.download_limit_input.text() == "2 MB"
    assert dialog.upload_limit_input.text() == "512 KB"
    assert dialog.seed_ratio_input.text() == "1.5"
    assert dialog.keep_seeding_checkbox.isChecked()
    assert dialog.get_data() == {
        "category": "My Shows", "download_limit_bps": 2 * 1024 * 1024,
        "upload_limit_bps": 512 * 1024, "seed_ratio_limit": 1.5, "keep_seeding": True,
    }


def test_properties_dialog_blank_fields_mean_unlimited(qtbot):
    dialog = TorrentPropertiesDialog(record=_record())
    qtbot.addWidget(dialog)
    assert dialog.download_limit_input.text() == "" and dialog.seed_ratio_input.text() == ""
    data = dialog.get_data()
    assert data["download_limit_bps"] == 0 and data["upload_limit_bps"] == 0
    assert data["seed_ratio_limit"] == 0.0 and data["keep_seeding"] is False


@pytest.mark.parametrize("field,value,error", [
    ("download_limit_input", "fast please", "invalid_speed_limit"),
    ("upload_limit_input", "12 parsecs", "invalid_speed_limit"),
    ("seed_ratio_input", "lots", "invalid_ratio"),
    ("seed_ratio_input", "-1", "invalid_ratio"),
])
def test_properties_dialog_rejects_bad_input(qtbot, field, value, error):
    dialog = TorrentPropertiesDialog(record=_record())
    qtbot.addWidget(dialog)
    getattr(dialog, field).setText(value)
    dialog._on_accept()
    assert dialog._error == error
    assert dialog.result() == 0  # not accepted
    assert dialog.info_label.text()


def test_apply_properties_updates_live_torrent_and_persists(qtbot, torrent_panel, tmp_path):
    torrent_id = _add_unseeded(torrent_panel, tmp_path, "props.bin")
    ok = torrent_panel.apply_properties(
        torrent_id, category="Linux ISOs", download_limit_bps=300 * 1024,
        upload_limit_bps=100 * 1024, seed_ratio_limit=2.0, keep_seeding=True)
    assert ok
    handle = torrent_panel.engine.handles[torrent_id]
    assert handle.download_limit() == 300 * 1024
    assert handle.upload_limit() == 100 * 1024
    assert torrent_panel.find_widget(torrent_id).category_badge.text() == "Linux ISOs"
    assert torrent_panel.category_filter.findText("Linux ISOs") >= 0

    saved = {r.torrent_id: r for r in torrent_panel.session_store.load_records()}[torrent_id]
    assert (saved.category, saved.download_limit_bps, saved.upload_limit_bps,
            saved.seed_ratio_limit, saved.keep_seeding) == ("Linux ISOs", 300 * 1024, 100 * 1024, 2.0, True)

    # Clearing limits back to unlimited works too.
    torrent_panel.apply_properties(torrent_id, category="Linux ISOs", download_limit_bps=0,
                                   upload_limit_bps=0, seed_ratio_limit=0.0, keep_seeding=False)
    assert handle.download_limit() <= 0 and handle.upload_limit() <= 0


def test_turning_keep_seeding_off_stops_a_seeding_torrent(qtbot, torrent_panel, local_seed, tmp_path):
    torrent_id = _download_to_completion(qtbot, torrent_panel, local_seed, tmp_path, "propseed.bin")
    handle = torrent_panel.engine.handles[torrent_id]
    qtbot.waitUntil(lambda: handle.status().paused, timeout=5000)
    torrent_panel.resume_torrent(torrent_id)  # Start Seeding -> keep_seeding on
    qtbot.waitUntil(lambda: not handle.status().paused, timeout=5000)
    assert torrent_panel.records[torrent_id].keep_seeding

    torrent_panel.apply_properties(torrent_id, category="Torrents", download_limit_bps=0,
                                   upload_limit_bps=0, seed_ratio_limit=0.0, keep_seeding=False)
    qtbot.waitUntil(lambda: handle.status().paused, timeout=5000)


def test_apply_properties_unknown_torrent_is_noop(torrent_panel):
    assert torrent_panel.apply_properties("nope", category="x", download_limit_bps=0,
                                          upload_limit_bps=0, seed_ratio_limit=0.0,
                                          keep_seeding=False) is False


# -- download queue ---------------------------------------------------------------
from types import SimpleNamespace  # noqa: E402

from conftest import OFFLINE_ENGINE_OPTIONS  # noqa: E402
from adp.torrent.engine import TorrentEngine  # noqa: E402

AUTO = int(lt.torrent_flags.auto_managed)


@pytest.mark.parametrize("paused,finished,flags,lt_state,expected", [
    (False, False, AUTO, 3, TorrentState.DOWNLOADING),
    (True, False, AUTO, 3, TorrentState.QUEUED),     # waiting for a slot
    (True, False, 0, 3, TorrentState.PAUSED),        # user pause clears auto_managed
    (True, True, 0, 5, TorrentState.FINISHED),       # stopped on complete
    (True, True, AUTO, 5, TorrentState.FINISHED),    # complete beats queued
    (False, True, AUTO, 5, TorrentState.SEEDING),
])
def test_state_from_status(paused, finished, flags, lt_state, expected):
    status = SimpleNamespace(paused=paused, is_finished=finished, flags=flags, state=lt_state)
    assert TorrentEngine.state_from_status(status) == expected
    assert TorrentEngine.is_queued(status) == (expected == TorrentState.QUEUED)


@pytest.fixture
def queue_panel(make_torrent_panel, tmp_path):
    """Limit 1, with libtorrent's 'slow torrents don't take a slot' rule off
    (our peerless test torrents are all 'slow') and a 1s auto-manager tick."""
    options = dict(OFFLINE_ENGINE_OPTIONS,
                   extra_settings={"dont_count_slow_torrents": False, "auto_manage_interval": 1})
    return make_torrent_panel(state_dir=str(tmp_path), engine_options=options, max_active_downloads=1)


def _states(panel, ids):
    return [panel.engine.state_from_status(panel.engine.handles[t].status()) for t in ids]


def test_limit_queues_extra_torrents_and_start_now_skips_queue(qtbot, queue_panel, tmp_path):
    ids = [_add_unseeded(queue_panel, tmp_path, f"q{i}.bin") for i in range(3)]
    qtbot.waitUntil(lambda: _states(queue_panel, ids).count(TorrentState.QUEUED) == 2, timeout=15000)
    queued = [t for t, st in zip(ids, _states(queue_panel, ids)) if st == TorrentState.QUEUED]

    # The row says why it isn't moving.
    status = queue_panel.engine._status_dict(queue_panel.engine.handles[queued[0]].status())
    assert status["queued"] is True
    widget = queue_panel.find_widget(queued[0])
    widget.update_status(status)
    assert "waiting for a free download slot" in widget.info_label.text()

    _select(queue_panel, {queued[0]})
    queue_panel.start_now_selected()
    qtbot.waitUntil(lambda: not queue_panel.engine.handles[queued[0]].status().paused, timeout=5000)
    qtbot.wait(2500)  # a couple of auto-manager ticks: it must stay running
    assert not queue_panel.engine.handles[queued[0]].status().paused
    assert _states(queue_panel, [queued[1]]) == [TorrentState.QUEUED]


def test_raising_limit_starts_queued_torrents(qtbot, queue_panel, tmp_path):
    ids = [_add_unseeded(queue_panel, tmp_path, f"r{i}.bin") for i in range(3)]
    qtbot.waitUntil(lambda: _states(queue_panel, ids).count(TorrentState.QUEUED) == 2, timeout=15000)
    queue_panel.set_max_active_downloads(0)  # unlimited
    qtbot.waitUntil(lambda: TorrentState.QUEUED not in _states(queue_panel, ids), timeout=15000)


def test_pausing_a_queued_torrent_makes_it_paused_not_queued(qtbot, queue_panel, tmp_path):
    ids = [_add_unseeded(queue_panel, tmp_path, f"p{i}.bin") for i in range(2)]
    qtbot.waitUntil(lambda: TorrentState.QUEUED in _states(queue_panel, ids), timeout=15000)
    queued = ids[_states(queue_panel, ids).index(TorrentState.QUEUED)]
    _select(queue_panel, {queued})
    queue_panel.pause_selected()
    qtbot.waitUntil(lambda: _states(queue_panel, [queued]) == [TorrentState.PAUSED], timeout=5000)


def test_context_menu_offers_start_now_for_queued(qtbot, queue_panel, tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QMenu
    ids = [_add_unseeded(queue_panel, tmp_path, f"m{i}.bin") for i in range(2)]
    qtbot.waitUntil(lambda: TorrentState.QUEUED in _states(queue_panel, ids), timeout=15000)
    queued = ids[_states(queue_panel, ids).index(TorrentState.QUEUED)]
    _select(queue_panel, {queued})
    seen = {}
    monkeypatch.setattr(QMenu, "exec", lambda self, *a: seen.setdefault(
        "items", [x.text() for x in self.actions() if not x.isSeparator()]))
    row = next(i for i in range(queue_panel.torrent_list.count())
               if queue_panel.torrent_list.item(i).data(Qt.ItemDataRole.UserRole) == queued)
    queue_panel.show_context_menu(queue_panel.torrent_list.visualItemRect(
        queue_panel.torrent_list.item(row)).center())
    assert "Start Now (skip queue)" in seen["items"]
    assert "Pause" in seen["items"]
    assert "Resume" not in seen["items"]


# -- move completed ---------------------------------------------------------------
def _status_messages(panel):
    messages = []
    panel.status_update_requested.connect(lambda msg, *_: messages.append(msg))
    return messages


def test_completed_torrent_is_moved_to_configured_folder(qtbot, torrent_panel, local_seed, tmp_path):
    library = tmp_path / "library"
    torrent_panel.set_move_completed(str(library))
    torrent_id = _download_to_completion(qtbot, torrent_panel, local_seed, tmp_path, "moveme.bin")
    qtbot.waitUntil(lambda: (library / "moveme.bin").exists()
                    and torrent_panel.records[torrent_id].save_path == str(library), timeout=15000)
    assert not (tmp_path / "leech_moveme.bin" / "moveme.bin").exists()
    assert torrent_panel.records[torrent_id].name == "moveme.bin"  # not "moveme.bin.torrent"
    saved = {r.torrent_id: r for r in torrent_panel.session_store.load_records()}[torrent_id]
    assert saved.save_path == str(library)
    # Stop-on-complete still applies to the moved torrent.
    assert torrent_panel.engine.handles[torrent_id].status().paused


def test_move_by_category_uses_subfolder(qtbot, torrent_panel, local_seed, tmp_path):
    library = tmp_path / "library"
    torrent_panel.set_move_completed(str(library), by_category=True)
    torrent_bytes = local_seed.seed_file("cat.bin", os.urandom(10_000))
    torrent_path = str(tmp_path / "cat.torrent")
    local_seed.write_torrent_file(torrent_bytes, torrent_path)
    torrent_id = torrent_panel.add_torrent(mode="file", torrent_file_path=torrent_path,
                                           save_path=str(tmp_path / "scratch"), category="Video")
    torrent_panel.engine.connect_peer(torrent_id, "127.0.0.1", local_seed.port)
    qtbot.waitUntil(lambda: (library / "Video" / "cat.bin").exists(), timeout=20000)


def test_already_complete_torrent_is_not_moved_on_restore(qtbot, tmp_path, local_seed, make_torrent_panel):
    """Turning the setting on (or restarting with it on) must not relocate
    torrents that completed earlier -- only fresh completions move."""
    state_dir = str(tmp_path / "state")
    panel1 = make_torrent_panel(state_dir=state_dir)
    torrent_id = _download_to_completion(qtbot, panel1, local_seed, tmp_path, "old.bin")
    panel1.save_session(wait_for_resume_data=True)
    panel1.engine.shutdown()

    library = tmp_path / "library"
    panel2 = make_torrent_panel(state_dir=state_dir, move_completed_dir=str(library))
    handle = panel2.engine.handles[torrent_id]
    qtbot.waitUntil(lambda: handle.status().is_finished, timeout=15000)
    qtbot.wait(1500)
    assert (tmp_path / "leech_old.bin" / "old.bin").exists()
    assert not library.exists() or not any(library.iterdir())


def test_move_never_overwrites_existing_files(qtbot, torrent_panel, local_seed, tmp_path):
    torrent_id = _download_to_completion(qtbot, torrent_panel, local_seed, tmp_path, "clash.bin")
    original = tmp_path / "leech_clash.bin" / "clash.bin"
    original_bytes = original.read_bytes()
    library = tmp_path / "library"
    library.mkdir()
    (library / "clash.bin").write_bytes(b"precious, not ours")
    messages = _status_messages(torrent_panel)

    assert torrent_panel.move_torrents([torrent_id], str(library)) == 1
    qtbot.waitUntil(lambda: any("Couldn't move" in m for m in messages), timeout=15000)
    assert (library / "clash.bin").read_bytes() == b"precious, not ours"
    assert original.read_bytes() == original_bytes
    assert torrent_panel.records[torrent_id].save_path == str(tmp_path / "leech_clash.bin")
    assert torrent_id not in torrent_panel._moves_pending


def test_move_refused_when_it_wont_fit(qtbot, torrent_panel, tmp_path, monkeypatch):
    import adp.gui.torrent_panel as tp
    from adp.core.storage import SpaceCheck
    torrent_id = _add_unseeded(torrent_panel, tmp_path, "big.bin")
    monkeypatch.setattr(tp, "same_volume", lambda a, b: False)
    monkeypatch.setattr(tp, "check_space", lambda *a, **k: SpaceCheck(False, 10, 10**12, 512 * 1024**2))
    moved = []
    monkeypatch.setattr(torrent_panel.engine, "move_storage", lambda *a: moved.append(a))
    messages = _status_messages(torrent_panel)
    assert torrent_panel.move_torrents([torrent_id], str(tmp_path / "other_drive")) == 0
    assert moved == [] and any("Files left in place" in m for m in messages)


def test_move_refused_when_destination_unavailable(qtbot, torrent_panel, tmp_path, monkeypatch):
    torrent_id = _add_unseeded(torrent_panel, tmp_path, "nodest.bin")
    blocker = tmp_path / "a_file"
    blocker.write_text("x")
    moved = []
    monkeypatch.setattr(torrent_panel.engine, "move_storage", lambda *a: moved.append(a))
    messages = _status_messages(torrent_panel)
    assert torrent_panel.move_torrents([torrent_id], str(blocker / "sub")) == 0
    assert moved == [] and any("destination unavailable" in m for m in messages)


def test_manual_move_of_stopped_completed_torrent(qtbot, torrent_panel, local_seed, tmp_path):
    torrent_id = _download_to_completion(qtbot, torrent_panel, local_seed, tmp_path, "manual.bin")
    handle = torrent_panel.engine.handles[torrent_id]
    qtbot.waitUntil(lambda: handle.status().paused, timeout=5000)
    dest = tmp_path / "picked"
    assert torrent_panel.move_torrents([torrent_id], str(dest)) == 1
    qtbot.waitUntil(lambda: (dest / "manual.bin").exists()
                    and torrent_panel.records[torrent_id].save_path == str(dest), timeout=15000)
    # Moving to where it already is does nothing.
    assert torrent_panel.move_torrents([torrent_id], str(dest)) == 0


def test_download_start_is_reported_by_alert_not_polling(qtbot, torrent_panel, local_seed, tmp_path):
    """Regression: auto-move keyed off *polled* state missed small torrents
    that started and finished between two polls (so they were never moved).
    The engine now reports the downloading transition from libtorrent's
    state_changed_alert, before torrent_finished."""
    events = []
    torrent_panel.engine.download_started.connect(lambda tid: events.append(("started", tid)))
    torrent_panel.engine.torrent_finished.connect(lambda tid, _n: events.append(("finished", tid)))
    torrent_id = _download_to_completion(qtbot, torrent_panel, local_seed, tmp_path, "fast.bin")
    qtbot.waitUntil(lambda: ("finished", torrent_id) in events, timeout=5000)
    assert events.index(("started", torrent_id)) < events.index(("finished", torrent_id))
