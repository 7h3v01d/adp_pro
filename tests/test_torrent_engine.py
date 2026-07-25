# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
import os
import time

import pytest

lt = pytest.importorskip("libtorrent", reason="libtorrent not installed; run `pip install libtorrent`")

from PyQt6.QtWidgets import QApplication

from adp.torrent.engine import TorrentEngine
from adp.torrent.models import FilePriority, TorrentState

pytestmark = pytest.mark.torrent


def pump(app, condition, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def test_add_torrent_file_and_download_completes(qapp, local_seed, leech_engine, leech_dir):
    content = os.urandom(300_000)
    torrent_bytes = local_seed.seed_file("movie.mp4", content)
    torrent_path = os.path.join(leech_dir, "movie.mp4.torrent")
    local_seed.write_torrent_file(torrent_bytes, torrent_path)

    leech_engine.start()
    torrent_id = leech_engine.add_torrent_file(torrent_path, leech_dir)
    leech_engine.connect_peer(torrent_id, "127.0.0.1", local_seed.port)

    finished = {}
    leech_engine.torrent_finished.connect(lambda tid, name: finished.update(tid=tid, name=name))
    assert pump(qapp, lambda: bool(finished), timeout=30)
    assert finished["tid"] == torrent_id

    with open(os.path.join(leech_dir, "movie.mp4"), "rb") as f:
        assert f.read() == content


def test_progress_updated_reports_growth(qapp, local_seed, leech_engine, leech_dir):
    content = os.urandom(500_000)
    torrent_bytes = local_seed.seed_file("data.bin", content)
    torrent_path = os.path.join(leech_dir, "data.bin.torrent")
    local_seed.write_torrent_file(torrent_bytes, torrent_path)

    leech_engine.start()
    torrent_id = leech_engine.add_torrent_file(torrent_path, leech_dir)
    leech_engine.connect_peer(torrent_id, "127.0.0.1", local_seed.port)

    seen_progress = []
    leech_engine.progress_updated.connect(
        lambda tid, status: seen_progress.append(status["total_wanted_done"]) if tid == torrent_id else None
    )

    finished = {}
    leech_engine.torrent_finished.connect(lambda tid, name: finished.update(done=True))
    assert pump(qapp, lambda: bool(finished), timeout=30)

    assert len(seen_progress) > 0
    assert seen_progress[-1] == len(content)
    assert all(a <= b for a, b in zip(seen_progress, seen_progress[1:]))


@pytest.mark.timeout(60)
def test_pause_and_resume(qapp, local_seed, leech_engine, leech_dir):
    content = os.urandom(2_000_000)
    torrent_bytes = local_seed.seed_file("bigfile.bin", content)
    torrent_path = os.path.join(leech_dir, "bigfile.bin.torrent")
    local_seed.write_torrent_file(torrent_bytes, torrent_path)

    leech_engine.start()
    torrent_id = leech_engine.add_torrent_file(torrent_path, leech_dir)

    # Connect every listener we care about up front, before pumping the
    # event loop at all. Local-swarm transfers over loopback can finish
    # near-instantly; a listener connected only after an earlier pump() call
    # can miss an alert that already fired and was drained by _poll() in the
    # meantime -- libtorrent alerts are consumed exactly once, so a signal
    # connected late can permanently miss it. Real GUI usage doesn't have
    # this problem because it connects once, upfront, when a torrent is
    # added, and never re-attaches listeners mid-flight.
    progress = {"done": 0}
    leech_engine.progress_updated.connect(
        lambda tid, status: progress.update(done=status["total_wanted_done"]) if tid == torrent_id else None
    )
    finished = {}
    leech_engine.torrent_finished.connect(lambda tid, name: finished.update(done=True) if tid == torrent_id else None)

    leech_engine.connect_peer(torrent_id, "127.0.0.1", local_seed.port)
    assert pump(qapp, lambda: progress["done"] > 0, timeout=20)

    leech_engine.pause(torrent_id)
    assert pump(qapp, lambda: leech_engine.handles[torrent_id].status().paused, timeout=10)
    # Let any blocks already in flight when pause() was issued land, rather
    # than expecting an instant, exact freeze -- over a fast loopback
    # connection libtorrent can have several block requests already
    # dispatched before a pause takes full effect.
    time.sleep(1.0)
    qapp.processEvents()
    baseline = progress["done"]

    # Hold across several auto-manage ticks (the leech engine runs the
    # auto-manager every 1s): a plain handle.pause() on an auto-managed
    # torrent gets silently resumed by the next tick, so pausing must also
    # take the torrent out of auto-management to actually stick.
    time.sleep(2.5)
    qapp.processEvents()
    assert leech_engine.handles[torrent_id].status().paused, \
        "pause was reverted by libtorrent's auto-manager"
    growth_while_paused = progress["done"] - baseline
    # Real growth (not just settling residue) should be a small fraction of
    # the whole transfer -- this confirms pause meaningfully stopped the
    # download rather than asserting a fragile exact-zero freeze.
    assert growth_while_paused < len(content) * 0.05

    leech_engine.resume(torrent_id)
    leech_engine.connect_peer(torrent_id, "127.0.0.1", local_seed.port)
    # Generous timeout: on a loaded machine (full-suite runs) the swarm can
    # take a while to re-establish after the pause window.
    assert pump(qapp, lambda: bool(finished), timeout=40)


def test_file_priorities_skip_unwanted_files(qapp, leech_engine, leech_dir, tmp_path):
    # Build a real multi-file torrent so we can verify per-file skip works.
    seed_dir = str(tmp_path / "multi_seed")
    os.makedirs(seed_dir, exist_ok=True)
    wanted_content = os.urandom(50_000)
    skipped_content = os.urandom(50_000)
    with open(os.path.join(seed_dir, "wanted.bin"), "wb") as f:
        f.write(wanted_content)
    with open(os.path.join(seed_dir, "skip_me.bin"), "wb") as f:
        f.write(skipped_content)

    fs = lt.file_storage()
    lt.add_files(fs, seed_dir)
    ct = lt.create_torrent(fs, piece_size=16384)
    lt.set_piece_hashes(ct, os.path.dirname(seed_dir))
    torrent_bytes = lt.bencode(ct.generate())
    torrent_path = os.path.join(str(tmp_path), "multi.torrent")
    with open(torrent_path, "wb") as f:
        f.write(torrent_bytes)

    entries = TorrentEngine.preview_torrent_file(torrent_path)
    names = {e.path.split(os.sep)[-1]: e.index for e in entries}
    assert "wanted.bin" in names and "skip_me.bin" in names

    leech_engine.start()
    priorities = {names["skip_me.bin"]: 0}  # 0 == skip
    torrent_id = leech_engine.add_torrent_file(torrent_path, leech_dir, file_priorities=priorities)

    # Applying priorities is asynchronous inside libtorrent (it just enqueues
    # the request), so poll for it to take effect rather than asserting
    # immediately after add_torrent_file returns.
    def _priorities_applied():
        entries = leech_engine.get_file_list(torrent_id)
        skip_entry = next((e for e in entries if e.index == names["skip_me.bin"]), None)
        return skip_entry is not None and skip_entry.priority == FilePriority.SKIP

    assert pump(qapp, _priorities_applied, timeout=10)

    file_list = leech_engine.get_file_list(torrent_id)
    skipped_entry = next(e for e in file_list if e.index == names["skip_me.bin"])
    wanted_entry = next(e for e in file_list if e.index == names["wanted.bin"])
    assert skipped_entry.priority == FilePriority.SKIP
    assert wanted_entry.priority != FilePriority.SKIP


@pytest.mark.timeout(30)
def test_duplicate_add_returns_same_id_without_duplicating_state(qapp, leech_engine, leech_dir, tmp_path):
    """Re-adding a torrent already in the session must hand back the existing
    id and leave engine bookkeeping untouched -- libtorrent itself silently
    returns the same handle for a duplicate add, which used to let the GUI
    build a second row/record pointing at one torrent."""
    seed_dir = str(tmp_path / "dup_seed")
    os.makedirs(seed_dir, exist_ok=True)
    with open(os.path.join(seed_dir, "file.bin"), "wb") as f:
        f.write(os.urandom(50_000))

    fs = lt.file_storage()
    lt.add_files(fs, seed_dir)
    ct = lt.create_torrent(fs, piece_size=16384)
    lt.set_piece_hashes(ct, os.path.dirname(seed_dir))
    torrent_path = os.path.join(str(tmp_path), "dup.torrent")
    with open(torrent_path, "wb") as f:
        f.write(lt.bencode(ct.generate()))

    leech_engine.start()
    first_id = leech_engine.add_torrent_file(torrent_path, leech_dir)
    second_id = leech_engine.add_torrent_file(torrent_path, leech_dir)

    assert second_id == first_id
    assert len(leech_engine.handles) == 1


def test_magnet_add_resolves_metadata_and_completes(qapp, local_seed, leech_engine, leech_dir):
    content = os.urandom(300_000)
    local_seed.seed_file("via_magnet.bin", content)
    magnet_uri = lt.make_magnet_uri(local_seed.torrent_info)

    leech_engine.start()
    torrent_id = leech_engine.add_magnet(magnet_uri, leech_dir)

    # See the comment in test_pause_and_resume: connect every listener
    # before triggering any activity (connect_peer here), since a fast
    # local-swarm transfer can complete before a late-attached listener
    # would ever see the alert.
    metadata = {}
    leech_engine.metadata_received.connect(
        lambda tid, name, size, files: metadata.update(name=name, size=size) if tid == torrent_id else None
    )
    finished = {}
    leech_engine.torrent_finished.connect(lambda tid, name: finished.update(done=True) if tid == torrent_id else None)

    leech_engine.connect_peer(torrent_id, "127.0.0.1", local_seed.port)
    assert pump(qapp, lambda: bool(metadata), timeout=20)
    assert metadata["size"] == len(content)
    assert pump(qapp, lambda: bool(finished), timeout=20)
    with open(os.path.join(leech_dir, "via_magnet.bin"), "rb") as f:
        assert f.read() == content


def test_status_dict_reports_seeding_after_completion(qapp, local_seed, leech_engine, leech_dir):
    content = os.urandom(100_000)
    torrent_bytes = local_seed.seed_file("seedtest.bin", content)
    torrent_path = os.path.join(leech_dir, "seedtest.bin.torrent")
    local_seed.write_torrent_file(torrent_bytes, torrent_path)

    leech_engine.start()
    torrent_id = leech_engine.add_torrent_file(torrent_path, leech_dir)
    leech_engine.connect_peer(torrent_id, "127.0.0.1", local_seed.port)

    finished = {}
    leech_engine.torrent_finished.connect(lambda tid, name: finished.update(done=True))
    assert pump(qapp, lambda: bool(finished), timeout=30)

    # After finishing, the handle should report is_finished (seeding or not
    # depending on auto-manage timing) rather than still "downloading".
    assert pump(qapp, lambda: leech_engine.handles[torrent_id].status().is_finished, timeout=10)


def test_remove_with_delete_files(qapp, local_seed, leech_engine, leech_dir):
    content = os.urandom(50_000)
    torrent_bytes = local_seed.seed_file("removeme.bin", content)
    torrent_path = os.path.join(leech_dir, "removeme.bin.torrent")
    local_seed.write_torrent_file(torrent_bytes, torrent_path)

    leech_engine.start()
    torrent_id = leech_engine.add_torrent_file(torrent_path, leech_dir)
    leech_engine.connect_peer(torrent_id, "127.0.0.1", local_seed.port)

    finished = {}
    leech_engine.torrent_finished.connect(lambda tid, name: finished.update(done=True))
    assert pump(qapp, lambda: bool(finished), timeout=30)

    target_path = os.path.join(leech_dir, "removeme.bin")
    assert os.path.exists(target_path)

    leech_engine.remove(torrent_id, delete_files=True)
    assert pump(qapp, lambda: not os.path.exists(target_path), timeout=10)
    assert torrent_id not in leech_engine.handles
