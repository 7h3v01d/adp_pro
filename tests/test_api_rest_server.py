import os
import threading

import pytest
from fastapi.testclient import TestClient

from adp.api.auth import ApiKeyStore
from adp.api.bridge import GuiBridge
from adp.api.controller import AppController
from adp.api.rest_server import build_app
from adp.core.models import Status
from adp.gui.main_window import DownloadPanel


def request_from_thread(qtbot, method_call, timeout=15000):
    """Runs a TestClient call on a background thread and waits for it,
    exactly like call_from_thread in the controller tests -- necessary
    because TestClient executes requests in-process, and calling it
    directly from the same thread that needs to be pumping the Qt event
    loop (to drain the bridge's queue) would deadlock."""
    box = {}

    def runner():
        box["response"] = method_call()

    t = threading.Thread(target=runner)
    t.start()
    qtbot.waitUntil(lambda: "response" in box, timeout=timeout)
    t.join(timeout=2)
    return box["response"]


@pytest.fixture
def bridge(qtbot):
    b = GuiBridge(poll_interval_ms=10)
    yield b
    b.stop()


@pytest.fixture
def download_panel(qtbot, tmp_path, thread_pool):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    p = DownloadPanel(state_dir=str(state_dir), thread_pool=thread_pool)
    qtbot.addWidget(p)
    yield p
    for manager in list(p.downloads.values()):
        if manager.status.is_active or manager.status == Status.PAUSED:
            manager.stop()


@pytest.fixture
def key_store(tmp_path):
    return ApiKeyStore(str(tmp_path / "keydir"))


@pytest.fixture
def client(bridge, download_panel, key_store):
    controller = AppController(bridge, download_panel, torrent_panel=None, stats_panel=None)
    app = build_app(controller, key_store)
    return TestClient(app)


def auth_headers(key_store):
    return {"X-API-Key": key_store.key}


def test_health_does_not_require_auth(qtbot, client):
    response = request_from_thread(qtbot, lambda: client.get("/health"))
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_missing_api_key_is_rejected(qtbot, client):
    response = request_from_thread(qtbot, lambda: client.get("/downloads"))
    assert response.status_code == 401


def test_wrong_api_key_is_rejected(qtbot, client):
    response = request_from_thread(qtbot, lambda: client.get("/downloads", headers={"X-API-Key": "wrong"}))
    assert response.status_code == 401


def test_list_downloads_empty(qtbot, client, key_store):
    response = request_from_thread(qtbot, lambda: client.get("/downloads", headers=auth_headers(key_store)))
    assert response.status_code == 200
    assert response.json() == []


def test_add_download_then_list_and_get(qtbot, client, key_store, mock_server, download_dir):
    mock_server.add_file("rest_test.zip", os.urandom(20_000))
    add_response = request_from_thread(qtbot, lambda: client.post(
        "/downloads",
        json={"url": mock_server.url_for("rest_test.zip"), "save_path": os.path.join(download_dir, "rest_test.zip")},
        headers=auth_headers(key_store),
    ))
    assert add_response.status_code == 200
    download_id = add_response.json()["download_id"]

    list_response = request_from_thread(qtbot, lambda: client.get("/downloads", headers=auth_headers(key_store)))
    assert len(list_response.json()) == 1

    get_response = request_from_thread(
        qtbot, lambda: client.get(f"/downloads/{download_id}", headers=auth_headers(key_store))
    )
    assert get_response.status_code == 200
    assert get_response.json()["filename"] == "rest_test.zip"


def test_add_download_missing_url_returns_400(qtbot, client, key_store):
    response = request_from_thread(
        qtbot, lambda: client.post("/downloads", json={"url": ""}, headers=auth_headers(key_store))
    )
    assert response.status_code == 400


def test_get_nonexistent_download_returns_404(qtbot, client, key_store):
    response = request_from_thread(
        qtbot, lambda: client.get("/downloads/does-not-exist", headers=auth_headers(key_store))
    )
    assert response.status_code == 404


def test_pause_resume_stop_via_rest(qtbot, client, key_store, download_panel, mock_server, download_dir):
    mock_server.add_file("pause_rest.bin", os.urandom(5_000_000))
    # Keep the download in flight long enough for pause/resume/stop to
    # land -- unthrottled loopback completes before they arrive.
    mock_server.set_throttle("pause_rest.bin", 800_000)
    add_response = request_from_thread(qtbot, lambda: client.post(
        "/downloads",
        json={
            "url": mock_server.url_for("pause_rest.bin"),
            "save_path": os.path.join(download_dir, "pause_rest.bin"),
            "num_threads": 1,
        },
        headers=auth_headers(key_store),
    ))
    download_id = add_response.json()["download_id"]

    qtbot.waitUntil(
        lambda: download_panel.downloads[download_id].status == Status.DOWNLOADING
        and download_panel.downloads[download_id].downloaded_size > 0,
        timeout=10000,
    )

    pause_response = request_from_thread(
        qtbot, lambda: client.post(f"/downloads/{download_id}/pause", headers=auth_headers(key_store))
    )
    assert pause_response.json()["status"] == "PAUSED"

    resume_response = request_from_thread(
        qtbot, lambda: client.post(f"/downloads/{download_id}/resume", headers=auth_headers(key_store))
    )
    assert resume_response.json()["status"] == "DOWNLOADING"

    stop_response = request_from_thread(
        qtbot, lambda: client.post(f"/downloads/{download_id}/stop", headers=auth_headers(key_store))
    )
    assert stop_response.json()["status"] == "STOPPED"


def test_remove_download_via_rest(qtbot, client, key_store, mock_server, download_dir):
    mock_server.add_file("remove_rest.bin", os.urandom(1000))
    add_response = request_from_thread(qtbot, lambda: client.post(
        "/downloads",
        json={"url": mock_server.url_for("remove_rest.bin"), "save_path": os.path.join(download_dir, "remove_rest.bin")},
        headers=auth_headers(key_store),
    ))
    download_id = add_response.json()["download_id"]

    delete_response = request_from_thread(
        qtbot, lambda: client.delete(f"/downloads/{download_id}", headers=auth_headers(key_store))
    )
    assert delete_response.status_code == 200
    assert delete_response.json()["removed"] is True


def test_torrent_endpoints_return_503_without_torrent_support(qtbot, client, key_store):
    response = request_from_thread(qtbot, lambda: client.get("/torrents", headers=auth_headers(key_store)))
    assert response.status_code == 503


def test_stats_endpoint_without_stats_panel(qtbot, client, key_store):
    response = request_from_thread(qtbot, lambda: client.get("/stats", headers=auth_headers(key_store)))
    assert response.status_code == 200
    assert response.json() == {}
