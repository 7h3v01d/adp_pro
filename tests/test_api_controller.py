# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
import os
import threading

import pytest

from adp.api.bridge import GuiBridge
from adp.api.controller import AppController, ApiError
from adp.core.models import Status
from adp.gui.main_window import DownloadPanel


def call_from_thread(qtbot, func, *args, **kwargs):
    """Runs func(*args, **kwargs) on a real background thread and waits for
    it to finish -- mirroring how the REST/MCP servers will actually call
    into the controller, so these tests exercise the real cross-thread path
    rather than just calling everything from the Qt test thread directly."""
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
def download_panel(qtbot, tmp_path, thread_pool):
    state_dir = tmp_path / "download_state"
    state_dir.mkdir()
    p = DownloadPanel(state_dir=str(state_dir), thread_pool=thread_pool)
    qtbot.addWidget(p)
    yield p
    for manager in list(p.downloads.values()):
        if manager.status.is_active or manager.status == Status.PAUSED:
            manager.stop()


@pytest.fixture
def controller(bridge, download_panel):
    return AppController(bridge, download_panel, torrent_panel=None, stats_panel=None)


def test_list_downloads_starts_empty(qtbot, controller):
    assert call_from_thread(qtbot, controller.list_downloads) == []


def test_add_and_get_download(qtbot, controller, download_panel, mock_server, download_dir):
    mock_server.add_file("api_test.zip", os.urandom(50_000))
    result = call_from_thread(
        qtbot, controller.add_download,
        url=mock_server.url_for("api_test.zip"),
        save_path=os.path.join(download_dir, "api_test.zip"),
    )
    assert result["filename"] == "api_test.zip"
    download_id = result["download_id"]

    # Poll the real manager state directly for the wait condition -- calling
    # through the full thread-spawning API path on every poll tick would be
    # needlessly slow for a tight wait loop.
    qtbot.waitUntil(lambda: download_panel.downloads[download_id].status == Status.COMPLETED, timeout=15000)
    final = call_from_thread(qtbot, controller.get_download, download_id)
    assert final["status"] == "COMPLETED"


def test_add_download_missing_url_raises_api_error(qtbot, controller):
    with pytest.raises(ApiError):
        call_from_thread(qtbot, controller.add_download, url="")


def test_get_nonexistent_download_raises_404(qtbot, controller):
    with pytest.raises(ApiError) as exc_info:
        call_from_thread(qtbot, controller.get_download, "does-not-exist")
    assert exc_info.value.status_code == 404


def test_pause_resume_stop_download(qtbot, controller, download_panel, mock_server, download_dir):
    mock_server.add_file("pausable.bin", os.urandom(2_000_000))
    # Loopback would serve 2 MB in microseconds -- the download used to
    # sometimes COMPLETE before pause_download() even landed. Cap the rate so
    # there is guaranteed to be an in-flight download to pause.
    mock_server.set_throttle("pausable.bin", 400_000)
    result = call_from_thread(
        qtbot, controller.add_download,
        url=mock_server.url_for("pausable.bin"),
        save_path=os.path.join(download_dir, "pausable.bin"),
        num_threads=1,
    )
    download_id = result["download_id"]

    qtbot.waitUntil(lambda: download_panel.downloads[download_id].downloaded_size > 0, timeout=10000)

    paused = call_from_thread(qtbot, controller.pause_download, download_id)
    assert paused["status"] == "PAUSED"

    resumed = call_from_thread(qtbot, controller.resume_download, download_id)
    assert resumed["status"] == "DOWNLOADING"

    stopped = call_from_thread(qtbot, controller.stop_download, download_id)
    assert stopped["status"] == "STOPPED"


def test_remove_download(qtbot, controller, mock_server, download_dir):
    mock_server.add_file("removeme.bin", os.urandom(1000))
    result = call_from_thread(
        qtbot, controller.add_download,
        url=mock_server.url_for("removeme.bin"),
        save_path=os.path.join(download_dir, "removeme.bin"),
    )
    download_id = result["download_id"]

    removal = call_from_thread(qtbot, controller.remove_download, download_id)
    assert removal["removed"] is True

    with pytest.raises(ApiError):
        call_from_thread(qtbot, controller.get_download, download_id)


def test_add_download_rejects_duplicate_path(qtbot, controller, mock_server, download_dir):
    mock_server.add_file("dup.bin", os.urandom(2_000_000))
    mock_server.set_throttle("dup.bin", 600_000)  # first download must still be active for the conflict check
    save_path = os.path.join(download_dir, "dup.bin")
    call_from_thread(qtbot, controller.add_download, url=mock_server.url_for("dup.bin"), save_path=save_path)

    with pytest.raises(ApiError):
        call_from_thread(qtbot, controller.add_download, url=mock_server.url_for("dup.bin"), save_path=save_path)


def test_stats_without_stats_panel_returns_empty_dict(qtbot, controller):
    assert call_from_thread(qtbot, controller.get_stats) == {}


def test_torrent_methods_raise_503_without_torrent_support(qtbot, controller):
    with pytest.raises(ApiError) as exc_info:
        call_from_thread(qtbot, controller.list_torrents)
    assert exc_info.value.status_code == 503


def test_api_default_path_uses_configured_download_dir(qtbot, controller, download_panel,
                                                        mock_server, tmp_path):
    """REST/MCP add-download without an explicit save_path must land the file
    in the user's configured download_dir, the same as the GUI -- not the
    app-data state dir. Regression: the controller used state_dir directly."""
    configured = tmp_path / "MyConfiguredDownloads"
    configured.mkdir()
    download_panel.settings["download_dir"] = str(configured)

    mock_server.add_file("viaapi.bin", b"x" * 500)
    result = call_from_thread(
        qtbot, controller.add_download, url=mock_server.url_for("viaapi.bin"))
    # The chosen save_path must be under the configured folder.
    assert result["save_path"].startswith(str(configured))
    assert result["save_path"].endswith("viaapi.bin")


def test_api_default_path_fails_closed_when_configured_dir_unavailable(
        qtbot, controller, download_panel, mock_server, tmp_path):
    """If the configured download folder is unavailable, the API must refuse
    rather than silently redirecting to the system/app-data drive."""
    from adp.api.controller import ApiError
    # A path whose parent is a file can't be created -> unavailable.
    blocker = tmp_path / "afile"
    blocker.write_text("x")
    download_panel.settings["download_dir"] = str(blocker / "sub")

    mock_server.add_file("viaapi2.bin", b"y" * 500)
    with pytest.raises(ApiError):
        call_from_thread(qtbot, controller.add_download,
                         url=mock_server.url_for("viaapi2.bin"))


def test_api_add_refuses_existing_destination(qtbot, controller, download_panel, mock_server, tmp_path):
    """REST/MCP add must return an error (not silently overwrite) when the
    destination already holds a non-ADP file and overwrite isn't set."""
    from adp.api.controller import ApiError
    existing = tmp_path / "existing.bin"
    existing.write_bytes(b"precious data")
    mock_server.add_file("existing.bin", b"new" * 100)
    with pytest.raises(ApiError):
        call_from_thread(qtbot, controller.add_download,
                         url=mock_server.url_for("existing.bin"),
                         save_path=str(existing))
    # File untouched.
    assert existing.read_bytes() == b"precious data"


def test_api_add_overwrites_with_explicit_flag(qtbot, controller, download_panel, mock_server, tmp_path):
    """overwrite=true lets the API replace an existing file."""
    existing = tmp_path / "existing2.bin"
    existing.write_bytes(b"old")
    mock_server.add_file("existing2.bin", b"n" * 500)
    result = call_from_thread(qtbot, controller.add_download,
                              url=mock_server.url_for("existing2.bin"),
                              save_path=str(existing), overwrite=True)
    assert result["save_path"] == str(existing)


def test_api_pause_rejects_illegal_transition(qtbot, controller, download_panel, mock_server, download_dir):
    """Pausing a COMPLETED download must raise, not silently return success."""
    from adp.api.controller import ApiError
    from adp.core.models import Status
    save = os.path.join(download_dir, "pauseillegal.bin")
    m, _ = download_panel.add_download(mock_server.url_for("pauseillegal.bin") if False else
                                       "http://192.0.2.1/x.bin", save,
                                       num_threads=1, start_immediately=False)
    m.set_status(Status.COMPLETED)
    with pytest.raises(ApiError):
        call_from_thread(qtbot, controller.pause_download, m.download_id)


def test_api_stop_rejects_illegal_transition(qtbot, controller, download_panel, download_dir):
    """Stopping an already-COMPLETED download must raise."""
    from adp.api.controller import ApiError
    from adp.core.models import Status
    save = os.path.join(download_dir, "stopillegal.bin")
    m, _ = download_panel.add_download("http://192.0.2.1/y.bin", save,
                                       num_threads=1, start_immediately=False)
    m.set_status(Status.COMPLETED)
    with pytest.raises(ApiError):
        call_from_thread(qtbot, controller.stop_download, m.download_id)
