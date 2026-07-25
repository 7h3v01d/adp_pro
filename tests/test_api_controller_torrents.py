# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
import base64
import os
import threading

import pytest

pytest.importorskip("libtorrent", reason="libtorrent not installed; run `pip install libtorrent`")

from adp.api.bridge import GuiBridge
from adp.api.controller import AppController, ApiError
from adp.gui.torrent_panel import TorrentPanel

pytestmark = pytest.mark.torrent


def call_from_thread(qtbot, func, *args, **kwargs):
    box = {}

    def runner():
        try:
            box["value"] = func(*args, **kwargs)
        except BaseException as e:
            box["error"] = e

    t = threading.Thread(target=runner)
    t.start()
    qtbot.waitUntil(lambda: "value" in box or "error" in box, timeout=15000)
    t.join(timeout=2)
    if "error" in box:
        raise box["error"]
    return box["value"]


@pytest.fixture
def bridge(qtbot):
    b = GuiBridge(poll_interval_ms=10)
    yield b
    b.stop()


@pytest.fixture
def torrent_panel(qtbot, tmp_path):
    panel = TorrentPanel(state_dir=str(tmp_path), listen_port=0, enable_dht=False)
    qtbot.addWidget(panel)
    yield panel
    for torrent_id in list(panel.engine.handles.keys()):
        panel.engine.remove(torrent_id, delete_files=False)


@pytest.fixture
def controller(bridge, torrent_panel):
    return AppController(bridge, download_panel=None, torrent_panel=torrent_panel, stats_panel=None)


def test_add_torrent_does_not_report_transient_paused_state(qtbot, controller, torrent_panel, local_seed, tmp_path):
    """Regression test: libtorrent reports a freshly-added torrent as
    paused=True for a brief moment until its internal queue processes it.
    The controller should smooth this over so callers don't see a
    misleading PAUSED state immediately after adding."""
    content = os.urandom(50_000)
    torrent_bytes = local_seed.seed_file("settle_test.bin", content)

    b64 = base64.b64encode(torrent_bytes).decode("ascii")
    result = call_from_thread(
        qtbot, controller.add_torrent, torrent_file_base64=b64, save_path=str(tmp_path / "settle_leech")
    )
    assert result["state"] != "PAUSED"
    assert result["paused"] is False


def test_list_torrents_starts_empty(qtbot, controller):
    assert call_from_thread(qtbot, controller.list_torrents) == []


def test_add_torrent_via_file_and_complete(qtbot, controller, torrent_panel, local_seed, tmp_path):
    content = os.urandom(200_000)
    torrent_bytes = local_seed.seed_file("api_torrent.bin", content)
    torrent_path = str(tmp_path / "api_torrent.bin.torrent")
    local_seed.write_torrent_file(torrent_bytes, torrent_path)
    save_path = str(tmp_path / "api_leech")

    b64 = base64.b64encode(torrent_bytes).decode("ascii")

    result = call_from_thread(
        qtbot, controller.add_torrent,
        torrent_file_base64=b64, torrent_file_name="api_torrent.bin.torrent", save_path=save_path,
    )
    torrent_id = result["torrent_id"]
    assert result["state"] in ("DOWNLOADING", "CHECKING", "QUEUED", "DOWNLOADING_METADATA")

    torrent_panel.engine.connect_peer(torrent_id, "127.0.0.1", local_seed.port)

    qtbot.waitUntil(
        lambda: torrent_panel.engine.handles[torrent_id].status().is_finished, timeout=20000
    )
    final = call_from_thread(qtbot, controller.get_torrent, torrent_id)
    assert final["state"] in ("FINISHED", "SEEDING")
    assert final["downloaded_bytes"] == len(content)


def test_add_torrent_via_magnet(qtbot, controller, torrent_panel, local_seed, tmp_path):
    import libtorrent as lt
    content = os.urandom(100_000)
    local_seed.seed_file("magnet_api.bin", content)
    magnet_uri = lt.make_magnet_uri(local_seed.torrent_info)

    result = call_from_thread(qtbot, controller.add_torrent, magnet_uri=magnet_uri, save_path=str(tmp_path / "out"))
    torrent_id = result["torrent_id"]
    torrent_panel.engine.connect_peer(torrent_id, "127.0.0.1", local_seed.port)

    qtbot.waitUntil(lambda: torrent_panel.engine.handles[torrent_id].status().has_metadata, timeout=15000)
    got = call_from_thread(qtbot, controller.get_torrent, torrent_id)
    assert got["name"] == "magnet_api.bin"


def test_add_torrent_requires_magnet_or_file(qtbot, controller):
    with pytest.raises(ApiError):
        call_from_thread(qtbot, controller.add_torrent)


def test_pause_resume_torrent(qtbot, controller, torrent_panel, local_seed, tmp_path):
    content = os.urandom(2_000_000)
    torrent_bytes = local_seed.seed_file("pausable.bin", content)
    torrent_path = str(tmp_path / "pausable.bin.torrent")
    local_seed.write_torrent_file(torrent_bytes, torrent_path)
    save_path = str(tmp_path / "pausable_leech")

    b64 = base64.b64encode(torrent_bytes).decode("ascii")
    result = call_from_thread(qtbot, controller.add_torrent, torrent_file_base64=b64, save_path=save_path)
    torrent_id = result["torrent_id"]
    torrent_panel.engine.connect_peer(torrent_id, "127.0.0.1", local_seed.port)

    qtbot.waitUntil(lambda: torrent_panel.engine.handles[torrent_id].status().progress > 0, timeout=15000)

    paused = call_from_thread(qtbot, controller.pause_torrent, torrent_id)
    assert paused["paused"] is True

    resumed = call_from_thread(qtbot, controller.resume_torrent, torrent_id)
    assert resumed["paused"] is False


def test_remove_torrent(qtbot, controller, torrent_panel, local_seed, tmp_path):
    content = os.urandom(10_000)
    torrent_bytes = local_seed.seed_file("removeapi.bin", content)

    b64 = base64.b64encode(torrent_bytes).decode("ascii")
    result = call_from_thread(
        qtbot, controller.add_torrent, torrent_file_base64=b64, save_path=str(tmp_path / "rmv_leech")
    )
    torrent_id = result["torrent_id"]

    removal = call_from_thread(qtbot, controller.remove_torrent, torrent_id)
    assert removal["removed"] is True

    with pytest.raises(ApiError):
        call_from_thread(qtbot, controller.get_torrent, torrent_id)


def test_get_nonexistent_torrent_raises_404(qtbot, controller):
    with pytest.raises(ApiError) as exc_info:
        call_from_thread(qtbot, controller.get_torrent, "does-not-exist")
    assert exc_info.value.status_code == 404
