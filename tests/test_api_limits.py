# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
"""API input bounds (external review findings 4 and 9).

* Request bodies over MAX_REQUEST_BODY_BYTES are refused with 413 *before*
  they are parsed -- previously the 10 MiB .torrent limit was only checked
  after FastAPI had already materialised an arbitrarily large JSON body.
* String/list inputs have sane maximum lengths, enforced in the REST schema
  and again in the controller (which is what MCP tools call).
"""
import threading

import pytest
from fastapi.testclient import TestClient

from adp.api.auth import ApiKeyStore
from adp.api.bridge import GuiBridge
from adp.api.controller import (
    ApiError, AppController, MAX_REQUEST_BODY_BYTES, MAX_SEARCH_TEXT_CHARS, MAX_URL_CHARS,
    MAX_CATEGORY_CHARS, MAX_FILE_SELECTION, MAX_TORRENT_BASE64_CHARS,
)
from adp.api.rest_server import build_app
from adp.gui.main_window import DownloadPanel


def in_thread(qtbot, fn, timeout=30000):
    box = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as e:  # noqa: BLE001
            box["error"] = e

    t = threading.Thread(target=run)
    t.start()
    qtbot.waitUntil(lambda: "value" in box or "error" in box, timeout=timeout)
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
    state = tmp_path / "state"
    state.mkdir()
    p = DownloadPanel(state_dir=str(state), thread_pool=thread_pool)
    qtbot.addWidget(p)
    return p


@pytest.fixture
def controller(bridge, download_panel):
    return AppController(bridge, download_panel, torrent_panel=None, stats_panel=None)


@pytest.fixture
def api(controller, tmp_path):
    keys = ApiKeyStore(str(tmp_path / "keys"))
    return TestClient(build_app(controller, keys)), {"X-API-Key": keys.key}


# -- body size ------------------------------------------------------------------------
def test_body_over_limit_rejected_with_413(qtbot, api):
    client, headers = api
    big = b'{"url": "' + b"a" * (MAX_REQUEST_BODY_BYTES + 10) + b'"}'
    resp = in_thread(qtbot, lambda: client.post(
        "/downloads", content=big, headers={**headers, "Content-Type": "application/json"}))
    assert resp.status_code == 413
    assert "too large" in resp.json()["detail"]


def test_chunked_body_over_limit_rejected_with_413(qtbot, api):
    """No Content-Length (chunked upload): bytes are counted as they arrive."""
    client, headers = api
    chunk = b"a" * (1024 * 1024)
    n_chunks = MAX_REQUEST_BODY_BYTES // len(chunk) + 2

    def body():
        yield b'{"url": "'
        for _ in range(n_chunks):
            yield chunk
        yield b'"}'

    resp = in_thread(qtbot, lambda: client.post(
        "/downloads", content=body(), headers={**headers, "Content-Type": "application/json"}))
    assert resp.status_code == 413


def test_oversized_body_rejected_even_without_api_key(qtbot, api):
    """The size check runs before auth: nobody gets to make the app buffer
    a huge body, authenticated or not."""
    client, _ = api
    resp = in_thread(qtbot, lambda: client.post(
        "/downloads", content=b"x" * (MAX_REQUEST_BODY_BYTES + 1)))
    assert resp.status_code == 413


def test_oversized_body_to_mcp_endpoint_rejected(qtbot, api):
    client, headers = api
    resp = in_thread(qtbot, lambda: client.post(
        "/mcp/", content=b"x" * (MAX_REQUEST_BODY_BYTES + 1), headers=headers))
    assert resp.status_code == 413


def test_max_size_torrent_upload_still_fits_under_body_limit():
    # The body cap must leave room for the largest legal base64 .torrent.
    assert MAX_REQUEST_BODY_BYTES > MAX_TORRENT_BASE64_CHARS + 1024


def test_normal_requests_unaffected(qtbot, api):
    client, headers = api
    resp = in_thread(qtbot, lambda: client.get("/downloads", headers=headers))
    assert resp.status_code == 200 and resp.json() == []


# -- input lengths (REST schema) --------------------------------------------------------
@pytest.mark.parametrize("path,body", [
    ("/downloads", {"url": "https://example.com/" + "a" * MAX_URL_CHARS}),
    ("/downloads", {"url": "https://example.com/f", "category": "c" * (MAX_CATEGORY_CHARS + 1)}),
    ("/search", {"text": "q" * (MAX_SEARCH_TEXT_CHARS + 1)}),
    ("/search", {"text": "ok", "providers": ["p"] * 33}),
    ("/search", {"text": "ok", "providers": ["p" * 129]}),
])
def test_rest_rejects_overlong_inputs(qtbot, api, path, body):
    client, headers = api
    resp = in_thread(qtbot, lambda: client.post(path, json=body, headers=headers))
    assert resp.status_code == 422, resp.text


# -- input lengths (controller: what MCP tools call) ------------------------------------
def test_controller_rejects_overlong_url_before_touching_gui(controller):
    with pytest.raises(ApiError) as exc:
        controller.add_download(url="https://example.com/" + "a" * MAX_URL_CHARS)
    assert exc.value.status_code == 422


def test_controller_rejects_overlong_category(controller):
    with pytest.raises(ApiError) as exc:
        controller.add_download(url="https://example.com/f", category="c" * (MAX_CATEGORY_CHARS + 1))
    assert exc.value.status_code == 422


def test_file_selection_count_is_capped():
    from adp.api.controller import AppController as C
    c = C.__new__(C)
    c._require_torrent_support = lambda: None
    with pytest.raises(ApiError):
        c.select_torrent_files("x" * 40, list(range(MAX_FILE_SELECTION + 1)))
    with pytest.raises(ApiError):
        c.select_torrent_files("x" * 40, [0, -1])
    with pytest.raises(ApiError):
        c.select_torrent_files("x" * 40, [True])
