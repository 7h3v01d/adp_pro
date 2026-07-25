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
