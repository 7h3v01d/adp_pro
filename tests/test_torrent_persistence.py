# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
"""2.0.3: torrents survive a restart, however ADP was closed.

Found through Dolly (25 Nov 2026): after restarting ADP, a magnet torrent added a
minute earlier was gone, and a finished magnet torrent (Arch Linux ISO) came back
"paused, 0%". Two causes:
  1. the torrent list was only written when the window closed cleanly, and
  2. libtorrent's resume data was saved without the torrent's info dict, so a
     magnet torrent restored paused could never get its file list back.
"""
import os

import pytest

lt = pytest.importorskip("libtorrent", reason="libtorrent not installed; run `pip install libtorrent`")

pytestmark = pytest.mark.torrent


def pump(qtbot, condition, timeout=20000):
    qtbot.waitUntil(condition, timeout=timeout)


def _magnet(torrent_bytes: bytes) -> str:
    return lt.make_magnet_uri(lt.torrent_info(lt.bdecode(torrent_bytes)))


def _restart(panel, make_torrent_panel, state_dir):
    """Shut the engine down without save_session(): like closing the console window."""
    panel.engine.shutdown()
    return make_torrent_panel(add_widget=False, state_dir=state_dir)


def test_a_torrent_added_is_still_there_after_an_unclean_restart(qtbot, tmp_path, local_seed, make_torrent_panel):
    torrent_bytes = local_seed.seed_file("added.bin", os.urandom(50_000))
    state_dir = str(tmp_path / "state")
    panel = make_torrent_panel(add_widget=False, state_dir=state_dir)
    panel.add_torrent(mode="magnet", magnet_uri=_magnet(torrent_bytes), save_path=str(tmp_path / "out"))
    panel2 = _restart(panel, make_torrent_panel, state_dir)
    assert panel2.torrent_list.count() == 1, "a torrent added before an unclean close was lost"


def _download_magnet(qtbot, panel, local_seed, tmp_path, name):
    torrent_bytes = local_seed.seed_file(name, os.urandom(120_000))
    torrent_id = panel.add_torrent(mode="magnet", magnet_uri=_magnet(torrent_bytes), save_path=str(tmp_path / "out"))
    panel.engine.connect_peer(torrent_id, "127.0.0.1", local_seed.port)
    finished = {}
    panel.torrent_completed.connect(lambda tid, _n: finished.update(done=True) if tid == torrent_id else None)
    pump(qtbot, lambda: bool(finished))
    handle = panel.engine.handles[torrent_id]
    qtbot.waitUntil(lambda: handle.status().paused, timeout=5000)        # stop-on-complete
    return torrent_id


def _has_info_dict(path):
    try:
        with open(path, "rb") as f:
            params = lt.read_resume_data(f.read())
        return bool(params.ti) and params.ti.is_valid()
    except Exception:  # noqa: BLE001 -- not written yet / half-written
        return False


def _restored_status(panel2, torrent_id):
    handle = panel2.engine.handles[torrent_id]
    return handle.status()


def test_a_finished_magnet_torrent_comes_back_finished_after_a_clean_restart(
        qtbot, tmp_path, local_seed, make_torrent_panel):
    state_dir = str(tmp_path / "state")
    panel = make_torrent_panel(add_widget=False, state_dir=state_dir)
    torrent_id = _download_magnet(qtbot, panel, local_seed, tmp_path, "arch.iso")
    panel.save_session(wait_for_resume_data=True)                         # a proper close
    local_seed.stop()                                                     # no peers after the restart
    panel2 = _restart(panel, make_torrent_panel, state_dir)
    qtbot.waitUntil(lambda: _restored_status(panel2, torrent_id).has_metadata, timeout=5000)
    qtbot.waitUntil(lambda: _restored_status(panel2, torrent_id).progress > 0.99, timeout=10000)
    assert panel2.records[torrent_id].name == "arch.iso"


def test_a_finished_magnet_torrent_survives_an_unclean_restart_too(
        qtbot, tmp_path, local_seed, make_torrent_panel):
    state_dir = str(tmp_path / "state")
    panel = make_torrent_panel(add_widget=False, state_dir=state_dir)
    torrent_id = _download_magnet(qtbot, panel, local_seed, tmp_path, "debian.iso")
    resume = os.path.join(state_dir, "torrents_resume", f"{torrent_id}.fastresume")
    pump(qtbot, lambda: _has_info_dict(resume), timeout=10000)         # saved, file list included
    local_seed.stop()
    panel2 = _restart(panel, make_torrent_panel, state_dir)               # no save_session()
    qtbot.waitUntil(lambda: _restored_status(panel2, torrent_id).has_metadata, timeout=5000)
    qtbot.waitUntil(lambda: _restored_status(panel2, torrent_id).progress > 0.99, timeout=10000)
    assert panel2.records[torrent_id].name == "debian.iso"


def test_a_removed_torrent_stays_removed_after_an_unclean_restart(qtbot, tmp_path, local_seed, make_torrent_panel):
    torrent_bytes = local_seed.seed_file("gone.bin", os.urandom(30_000))
    state_dir = str(tmp_path / "state")
    panel = make_torrent_panel(add_widget=False, state_dir=state_dir)
    tid = panel.add_torrent(mode="magnet", magnet_uri=_magnet(torrent_bytes), save_path=str(tmp_path / "out"))
    panel.remove_torrent(tid)
    panel2 = _restart(panel, make_torrent_panel, state_dir)
    assert panel2.torrent_list.count() == 0


def test_a_magnet_torrent_saved_the_old_way_is_repaired(qtbot, tmp_path, local_seed, make_torrent_panel):
    """Leon's Arch ISO: finished, paused, and saved by 2.0.2 without its file list."""
    state_dir = str(tmp_path / "state")
    panel = make_torrent_panel(add_widget=False, state_dir=state_dir)
    torrent_id = _download_magnet(qtbot, panel, local_seed, tmp_path, "old.iso")
    panel.save_session(wait_for_resume_data=True)
    resume = os.path.join(state_dir, "torrents_resume", f"{torrent_id}.fastresume")
    with open(resume, "rb") as f:
        params = lt.read_resume_data(f.read())
    params.ti = None                                                       # how 2.0.2 saved it
    with open(resume, "wb") as f:
        f.write(lt.write_resume_data_buf(params))
    panel2 = _restart(panel, make_torrent_panel, state_dir)
    tid2 = next(iter(panel2.engine.handles))
    panel2.engine.connect_peer(tid2, "127.0.0.1", local_seed.port)        # someone to get the file list from
    qtbot.waitUntil(lambda: _restored_status(panel2, torrent_id).has_metadata, timeout=10000)
    qtbot.waitUntil(lambda: _restored_status(panel2, torrent_id).progress > 0.99, timeout=10000)
    qtbot.waitUntil(lambda: _restored_status(panel2, torrent_id).paused, timeout=5000)   # stopped again
