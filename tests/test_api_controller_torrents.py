# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
import base64
import os
import threading

import pytest

pytest.importorskip("libtorrent", reason="libtorrent not installed; run `pip install libtorrent`")

from adp.api.bridge import GuiBridge
from adp.api.controller import AppController, ApiError

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
def torrent_panel(make_torrent_panel, tmp_path):
    return make_torrent_panel(state_dir=str(tmp_path))


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


# -- update_torrent (API twin of the Properties dialog) ------------------------------
def _add_via_api(qtbot, controller, local_seed, tmp_path, name):
    torrent_bytes = local_seed.seed_file(name, os.urandom(10_000))
    b64 = base64.b64encode(torrent_bytes).decode("ascii")
    return call_from_thread(qtbot, controller.add_torrent, torrent_file_base64=b64,
                            save_path=str(tmp_path / f"leech_{name}"))["torrent_id"]


def test_update_torrent_partial_changes_only_given_fields(qtbot, controller, torrent_panel, local_seed, tmp_path):
    torrent_id = _add_via_api(qtbot, controller, local_seed, tmp_path, "upd.bin")
    result = call_from_thread(qtbot, controller.update_torrent, torrent_id,
                              download_limit_bps=250_000, seed_ratio_limit=1.5)
    assert result["download_limit_bps"] == 250_000
    assert result["seed_ratio_limit"] == 1.5
    assert result["upload_limit_bps"] == 0          # untouched
    assert result["category"] == "Torrents"         # untouched
    assert result["keep_seeding"] is False          # untouched
    assert torrent_panel.engine.handles[torrent_id].download_limit() == 250_000

    result = call_from_thread(qtbot, controller.update_torrent, torrent_id,
                              category="Linux ISOs", keep_seeding=True)
    assert (result["category"], result["keep_seeding"]) == ("Linux ISOs", True)
    assert result["download_limit_bps"] == 250_000  # earlier change kept
    assert torrent_panel.find_widget(torrent_id).category_badge.text() == "Linux ISOs"
    saved = {r.torrent_id: r for r in torrent_panel.session_store.load_records()}[torrent_id]
    assert (saved.category, saved.download_limit_bps, saved.keep_seeding) == ("Linux ISOs", 250_000, True)


@pytest.mark.parametrize("kwargs", [
    {},                                   # nothing to update
    {"category": "   "},
    {"download_limit_bps": -1},
    {"upload_limit_bps": 1.5},
    {"download_limit_bps": True},         # bool must not pass as 1 B/s
    {"seed_ratio_limit": -0.5},
    {"seed_ratio_limit": float("nan")},
    {"keep_seeding": "yes"},
])
def test_update_torrent_rejects_bad_input(qtbot, controller, torrent_panel, local_seed, tmp_path, kwargs):
    torrent_id = _add_via_api(qtbot, controller, local_seed, tmp_path, "bad.bin")
    with pytest.raises(ApiError) as exc_info:
        call_from_thread(qtbot, controller.update_torrent, torrent_id, **kwargs)
    assert exc_info.value.status_code == 400
    record = torrent_panel.records[torrent_id]
    assert (record.download_limit_bps, record.seed_ratio_limit, record.category) == (0, 0.0, "Torrents")


def test_update_unknown_torrent_is_404(qtbot, controller):
    with pytest.raises(ApiError) as exc_info:
        call_from_thread(qtbot, controller.update_torrent, "f" * 40, category="x")
    assert exc_info.value.status_code == 404


def test_update_torrent_via_rest_patch(qtbot, controller, torrent_panel, local_seed, tmp_path):
    from fastapi.testclient import TestClient
    from adp.api.auth import ApiKeyStore
    from adp.api.rest_server import build_app
    keys = ApiKeyStore(str(tmp_path / "keys"))
    client = TestClient(build_app(controller, keys))
    headers = {"X-API-Key": keys.key}
    torrent_id = _add_via_api(qtbot, controller, local_seed, tmp_path, "rest.bin")

    resp = call_from_thread(qtbot, lambda: client.patch(
        f"/torrents/{torrent_id}", json={"upload_limit_bps": 64_000, "keep_seeding": True}, headers=headers))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["upload_limit_bps"] == 64_000 and body["keep_seeding"] is True
    assert body["download_limit_bps"] == 0  # not sent -> unchanged

    bad = call_from_thread(qtbot, lambda: client.patch(
        f"/torrents/{torrent_id}", json={"download_limit_bps": -5}, headers=headers))
    assert bad.status_code == 422  # schema-level rejection
    empty = call_from_thread(qtbot, lambda: client.patch(
        f"/torrents/{torrent_id}", json={}, headers=headers))
    assert empty.status_code == 400
    missing = call_from_thread(qtbot, lambda: client.patch(
        f"/torrents/{'0' * 40}", json={"category": "x"}, headers=headers))
    assert missing.status_code == 404
    noauth = call_from_thread(qtbot, lambda: client.patch(
        f"/torrents/{torrent_id}", json={"category": "x"}))
    assert noauth.status_code == 401


def test_update_torrent_via_mcp_tool(qtbot, controller, torrent_panel, local_seed, tmp_path):
    import asyncio
    import json
    from adp.api.mcp_tools import build_mcp_server
    server = build_mcp_server(controller)
    torrent_id = _add_via_api(qtbot, controller, local_seed, tmp_path, "mcp.bin")
    result = call_from_thread(qtbot, lambda: asyncio.run(server.call_tool(
        "update_torrent", {"torrent_id": torrent_id, "category": "Agents", "seed_ratio_limit": 3})))
    content = result[0] if isinstance(result, tuple) else result
    payload = json.loads(next(b.text for b in content if getattr(b, "text", None)))
    assert payload["category"] == "Agents" and payload["seed_ratio_limit"] == 3.0
    assert torrent_panel.records[torrent_id].category == "Agents"


def test_start_now_only_for_queued_torrents(qtbot, controller, torrent_panel, local_seed, tmp_path):
    torrent_id = _add_via_api(qtbot, controller, local_seed, tmp_path, "notqueued.bin")
    call_from_thread(qtbot, controller.pause_torrent, torrent_id)
    with pytest.raises(ApiError) as exc_info:
        call_from_thread(qtbot, controller.start_torrent_now, torrent_id)
    assert exc_info.value.status_code == 409
    assert call_from_thread(qtbot, controller.get_torrent, torrent_id)["queued"] is False
