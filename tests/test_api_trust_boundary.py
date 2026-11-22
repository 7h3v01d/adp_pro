# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
"""The API trust boundary (external review findings 2 and 5).

Automation should hold less authority than a person at the GUI:

* Network: downloads added through REST/MCP may only reach public internet
  addresses -- checked at add time, on every redirect hop, and on the address
  actually connected to -- unless "Allow API downloads from local network"
  (api_allow_private_network) is on.
* Filesystem: REST/MCP may only save inside ADP's configured download /
  torrent folders unless "Allow API to save anywhere" (api_allow_any_save_path)
  is on.
Downloads added in the GUI are never restricted.
"""
import os
import socket
import threading
import time

import pytest
import requests

from adp.api.bridge import GuiBridge
from adp.api.controller import ApiError, AppController, is_inside
from adp.core import net_policy
from adp.core.downloader import DownloadManager
from adp.core.models import DownloadRecord, Status
from adp.core.net_policy import BlockedDestinationError, PublicOnlySession, check_url, is_public_address
from adp.gui.main_window import DownloadPanel
from mock_server import MockDownloadServer


def call_from_thread(qtbot, func, *args, **kwargs):
    box = {}

    def runner():
        try:
            box["value"] = func(*args, **kwargs)
        except BaseException as e:  # noqa: BLE001
            box["error"] = e

    t = threading.Thread(target=runner)
    t.start()
    qtbot.waitUntil(lambda: "value" in box or "error" in box, timeout=15000)
    t.join(timeout=2)
    if "error" in box:
        raise box["error"]
    return box["value"]


def pump(app, condition, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return False


# -- address classification ----------------------------------------------------------
@pytest.mark.parametrize("address,public", [
    ("127.0.0.1", False), ("127.8.9.10", False),        # loopback
    ("10.0.0.1", False), ("172.16.5.4", False),          # RFC 1918
    ("192.168.0.163", False),                            # your LAN rig, for instance
    ("169.254.169.254", False),                          # link-local / cloud metadata
    ("100.64.1.1", False),                               # CGNAT
    ("0.0.0.0", False), ("224.0.0.1", False),            # unspecified, multicast
    ("::1", False), ("fe80::1", False), ("fc00::1", False),
    ("::ffff:127.0.0.1", False), ("::ffff:192.168.1.1", False),  # IPv4-mapped tricks
    ("not-an-ip", False),
    ("8.8.8.8", True), ("1.1.1.1", True), ("2606:4700:4700::1111", True),
])
def test_is_public_address(address, public):
    assert is_public_address(address) is public


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8080/x", "http://localhost/x", "http://[::1]/x",
    "http://192.168.0.163:11434/api", "http://169.254.169.254/latest/meta-data/",
])
def test_check_url_blocks_local_targets(url):
    with pytest.raises(BlockedDestinationError):
        check_url(url)


def test_check_url_blocks_hostname_resolving_to_private(monkeypatch):
    real = socket.getaddrinfo
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: (
        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 80))] if host == "nas.example"
        else real(host, *a, **k)))
    with pytest.raises(BlockedDestinationError) as exc:
        check_url("https://nas.example/share/file.iso")
    assert exc.value.address == "10.1.2.3"
    assert "Settings -> API" in str(exc.value)


def test_check_url_allows_public_and_ignores_dns_failure(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: (
        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))] if host == "example.com"
        else (_ for _ in ()).throw(socket.gaierror("nope"))))
    check_url("https://example.com/file.zip")
    check_url("https://no-such-host.invalid/file.zip")  # left to fail normally


# -- enforcement inside the session ----------------------------------------------------
@pytest.fixture
def lan_server():
    """A server on 127.0.0.2, standing in for a LAN/internal host."""
    server = MockDownloadServer(host="127.0.0.2").start()
    server.add_file("secret.txt", b"router admin page")
    yield server
    server.stop()


def test_redirect_into_private_network_is_blocked(mock_server, lan_server, monkeypatch):
    """Review: 'a public URL can redirect into one of those addresses'.
    127.0.0.1 plays the public site here; it redirects to the 'LAN' host."""
    monkeypatch.setattr(net_policy, "is_public_address", lambda a: a != "127.0.0.2")
    mock_server.add_file("dl", b"x")
    mock_server.set_redirect("dl", lan_server.url_for("secret.txt"))
    session = PublicOnlySession()
    with pytest.raises(BlockedDestinationError) as exc:
        session.get(mock_server.url_for("dl"))
    assert exc.value.host == "127.0.0.2"
    assert lan_server.request_count("secret.txt") == 0  # never reached


def test_connect_time_check_defeats_dns_rebinding(lan_server, monkeypatch):
    """A name that passed the pre-check but connects to a private address
    (DNS rebinding) is stopped when the socket connects."""
    monkeypatch.setattr(net_policy, "check_url", lambda url: None)  # pre-check fooled
    session = PublicOnlySession()
    with pytest.raises(BlockedDestinationError) as exc:
        session.get(lan_server.url_for("secret.txt"), timeout=5)
    assert exc.value.address == "127.0.0.2"
    assert lan_server.request_count("secret.txt") == 0  # no HTTP request was sent


def test_public_only_session_ignores_proxy_env(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    assert PublicOnlySession().trust_env is False


# -- end to end through the downloader -------------------------------------------------
def test_api_added_download_is_blocked_by_engine_and_retry_obeys_live_setting(
        qapp, thread_pool, mock_server, tmp_path):
    mock_server.add_file("f.bin", b"y" * 5000)
    allow = {"value": False}
    m = DownloadManager("dl-api", mock_server.url_for("f.bin"), str(tmp_path / "f.bin"),
                        thread_pool, added_via_api=True)
    m.network_policy_allows_private = lambda: allow["value"]
    m.start()
    assert pump(qapp, lambda: m.status == Status.ERROR)
    assert mock_server.request_count("f.bin") == 0

    allow["value"] = True  # user ticks the setting, then retries
    m.retry()
    assert pump(qapp, lambda: m.status == Status.COMPLETED)


def test_gui_added_download_is_never_restricted(qapp, thread_pool, mock_server, tmp_path):
    mock_server.add_file("g.bin", b"z" * 5000)
    m = DownloadManager("dl-gui", mock_server.url_for("g.bin"), str(tmp_path / "g.bin"), thread_pool)
    m.start()
    assert pump(qapp, lambda: m.status == Status.COMPLETED)


def test_added_via_api_survives_restart():
    rec = DownloadRecord(download_id="d", url="http://x/y", save_path="/tmp/y", added_via_api=True)
    assert DownloadRecord.from_dict(rec.to_dict()).added_via_api is True
    legacy = rec.to_dict()
    legacy.pop("added_via_api")
    assert DownloadRecord.from_dict(legacy).added_via_api is False


# -- controller: network ----------------------------------------------------------------
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
    p.settings["download_dir"] = str(tmp_path / "Downloads")
    yield p
    for manager in list(p.downloads.values()):
        if manager.status.is_active or manager.status == Status.PAUSED:
            manager.stop()


@pytest.fixture
def controller(bridge, download_panel):
    return AppController(bridge, download_panel, torrent_panel=None, stats_panel=None)


def test_api_refuses_local_url_by_default(qtbot, controller, download_panel, mock_server):
    mock_server.add_file("a.bin", b"a")
    with pytest.raises(ApiError) as exc:
        call_from_thread(qtbot, controller.add_download, url=mock_server.url_for("a.bin"))
    assert exc.value.status_code == 403
    assert download_panel.downloads == {}
    assert mock_server.request_count("a.bin") == 0


def test_api_allows_local_url_when_setting_enabled(qtbot, controller, download_panel, mock_server):
    mock_server.add_file("b.bin", b"b" * 1000)
    download_panel.settings["api_allow_private_network"] = True
    result = call_from_thread(qtbot, controller.add_download, url=mock_server.url_for("b.bin"))
    manager = download_panel.downloads[result["download_id"]]
    assert manager.added_via_api is True
    assert pump(qtbot._request.getfixturevalue("qapp"), lambda: manager.status == Status.COMPLETED)


# -- controller: filesystem -------------------------------------------------------------
def test_is_inside(tmp_path):
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    assert is_inside(str(root / "sub" / "f.bin"), str(root))
    assert is_inside(str(root / "new" / "f.bin"), str(root))           # not created yet
    assert not is_inside(str(tmp_path / "elsewhere.bin"), str(root))
    assert not is_inside(str(root / ".." / "escape.bin"), str(root))
    assert not is_inside(str(root), str(root))
    assert is_inside(str(root), str(root), allow_root=True)


def test_is_inside_follows_links_out_of_the_root(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    try:
        os.symlink(outside, root / "link", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted here")
    assert not is_inside(str(root / "link" / "f.bin"), str(root))


@pytest.fixture
def lan_ok(download_panel):
    # Network policy isn't under test here: the mock server is local.
    download_panel.settings["api_allow_private_network"] = True


def test_api_save_path_outside_download_folder_refused(qtbot, controller, mock_server, tmp_path, lan_ok):
    mock_server.add_file("c.bin", b"c")
    target = tmp_path / "Documents" / "important.docx"
    with pytest.raises(ApiError) as exc:
        call_from_thread(qtbot, controller.add_download, url=mock_server.url_for("c.bin"),
                         save_path=str(target))
    assert exc.value.status_code == 403
    assert "Settings -> API" in exc.value.message


def test_api_overwrite_outside_folder_cannot_touch_file(qtbot, controller, mock_server, tmp_path, lan_ok):
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"keep me")
    mock_server.add_file("d.bin", b"evil")
    with pytest.raises(ApiError):
        call_from_thread(qtbot, controller.add_download, url=mock_server.url_for("d.bin"),
                         save_path=str(victim), overwrite=True)
    assert victim.read_bytes() == b"keep me"


def test_api_save_path_traversal_refused(qtbot, controller, mock_server, tmp_path, lan_ok):
    mock_server.add_file("e.bin", b"e")
    sneaky = os.path.join(str(tmp_path / "Downloads"), "..", "escaped.bin")
    with pytest.raises(ApiError) as exc:
        call_from_thread(qtbot, controller.add_download, url=mock_server.url_for("e.bin"),
                         save_path=sneaky)
    assert exc.value.status_code == 403


def test_api_save_path_inside_folder_accepted(qtbot, controller, mock_server, tmp_path, lan_ok):
    mock_server.add_file("f.bin", b"f")
    inside = tmp_path / "Downloads" / "sub" / "f.bin"
    result = call_from_thread(qtbot, controller.add_download, url=mock_server.url_for("f.bin"),
                              save_path=str(inside))
    assert result["save_path"] == str(inside)


def test_api_save_anywhere_setting(qtbot, controller, download_panel, mock_server, tmp_path, lan_ok):
    download_panel.settings["api_allow_any_save_path"] = True
    mock_server.add_file("g.bin", b"g")
    elsewhere = tmp_path / "Elsewhere" / "g.bin"
    result = call_from_thread(qtbot, controller.add_download, url=mock_server.url_for("g.bin"),
                              save_path=str(elsewhere))
    assert result["save_path"] == str(elsewhere)


# -- controller: torrent folder ----------------------------------------------------------
@pytest.fixture
def torrent_controller(bridge, make_torrent_panel, tmp_path):
    from types import SimpleNamespace
    pytest.importorskip("libtorrent")
    panel = make_torrent_panel(state_dir=str(tmp_path / "tstate"),
                               default_save_path=str(tmp_path / "Torrents"))
    holder = SimpleNamespace(settings={})
    return AppController(bridge, download_panel=holder, torrent_panel=panel, stats_panel=None), holder


MAGNET = "magnet:?xt=urn:btih:" + "c" * 40 + "&dn=test"


def test_api_torrent_save_path_outside_torrent_folder_refused(qtbot, torrent_controller, tmp_path):
    controller, _ = torrent_controller
    with pytest.raises(ApiError) as exc:
        call_from_thread(qtbot, controller.add_torrent, magnet_uri=MAGNET,
                         save_path=str(tmp_path / "Windows" / "System32"))
    assert exc.value.status_code == 403


@pytest.mark.parametrize("sub", ["", "Linux ISOs"])
def test_api_torrent_save_path_in_torrent_folder_accepted(qtbot, torrent_controller, tmp_path, sub):
    controller, _ = torrent_controller
    target = tmp_path / "Torrents" / sub if sub else tmp_path / "Torrents"
    result = call_from_thread(qtbot, controller.add_torrent, magnet_uri=MAGNET, save_path=str(target))
    assert result["torrent_id"]
