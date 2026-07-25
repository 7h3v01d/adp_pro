# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
import asyncio
import json
import os
import threading

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from adp.api.auth import ApiKeyStore
from adp.api.bridge import GuiBridge
from adp.api.controller import AppController
from adp.api.mcp_tools import build_mcp_server
from adp.core.models import Status
from adp.gui.main_window import DownloadPanel


def call_from_thread(qtbot, func, *args, timeout=15000, **kwargs):
    box = {}

    def runner():
        try:
            box["value"] = func(*args, **kwargs)
        except BaseException as e:
            box["error"] = e

    t = threading.Thread(target=runner)
    t.start()
    qtbot.waitUntil(lambda: "value" in box or "error" in box, timeout=timeout)
    t.join(timeout=2)
    if "error" in box:
        raise box["error"]
    return box["value"]


def call_tool_from_thread(qtbot, mcp_server, name, args, timeout=15000):
    """asyncio.run() must happen on a background thread here too, for the
    same reason as call_from_thread: the tool ultimately calls into
    AppController -> GuiBridge.call(), which blocks waiting for the Qt main
    thread to drain its queue -- that can't be the same thread running the
    asyncio loop that's waiting on this call.

    Note on return shape: mcp_server.call_tool()'s return value is NOT a
    stable, single shape across return-type annotations in this SDK version
    -- sometimes a (content_list, structured_dict) tuple, sometimes just a
    content_list. We don't rely on either; extract_json() below pulls the
    JSON payload out of whatever TextContent block is present, regardless
    of how it's wrapped."""
    return call_from_thread(qtbot, lambda: asyncio.run(mcp_server.call_tool(name, args)), timeout=timeout)


def extract_json(call_tool_result):
    """Normalizes mcp_server.call_tool()'s result (tuple-or-list-of-content)
    down to the actual JSON payload the tool returned."""
    content = call_tool_result[0] if isinstance(call_tool_result, tuple) else call_tool_result
    for block in content:
        text = getattr(block, "text", None)
        if text:
            return json.loads(text)
    raise AssertionError(f"No text content found in tool result: {call_tool_result!r}")


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
def controller(bridge, download_panel):
    return AppController(bridge, download_panel, torrent_panel=None, stats_panel=None)


@pytest.fixture
def mcp_server(controller):
    return build_mcp_server(controller)


def test_all_expected_tools_are_registered(qtbot, mcp_server):
    tools = call_from_thread(qtbot, lambda: asyncio.run(mcp_server.list_tools()))
    names = {t.name for t in tools}
    expected = {
        "list_downloads", "get_download", "add_download", "pause_download", "resume_download",
        "stop_download", "retry_download", "remove_download",
        "list_torrents", "get_torrent", "add_torrent", "pause_torrent", "resume_torrent",
        "remove_torrent", "force_recheck_torrent", "select_torrent_files",
        "get_stats",
    }
    assert expected.issubset(names)


def test_list_downloads_tool_returns_empty_list(qtbot, mcp_server):
    result = call_tool_from_thread(qtbot, mcp_server, "list_downloads", {})
    assert extract_json(result) == {"downloads": []}


def test_add_download_tool_then_list(qtbot, mcp_server, mock_server, download_dir):
    mock_server.add_file("mcp_test.zip", os.urandom(20_000))
    result = call_tool_from_thread(qtbot, mcp_server, "add_download", {
        "url": mock_server.url_for("mcp_test.zip"),
        "save_path": os.path.join(download_dir, "mcp_test.zip"),
    })
    assert extract_json(result)["filename"] == "mcp_test.zip"

    listed = call_tool_from_thread(qtbot, mcp_server, "list_downloads", {})
    assert len(extract_json(listed)["downloads"]) == 1


def test_add_download_tool_missing_url_raises_tool_error(qtbot, mcp_server):
    """Confirmed empirically: a raised ApiError propagates out of
    mcp_server.call_tool() as a ToolError, it is not silently wrapped into
    an 'isError' content result."""
    with pytest.raises(ToolError, match="url is required"):
        call_tool_from_thread(qtbot, mcp_server, "add_download", {"url": ""})


def test_list_downloads_tool_wraps_multiple_items_in_one_json_block(qtbot, mcp_server, mock_server, download_dir):
    """Regression test: a tool whose return type annotation is a bare
    `list` gets serialized by FastMCP as one separate content block PER
    ITEM rather than a single JSON array -- fine for a human reading
    multiple text blocks, but it means an AI client parsing the result as
    one JSON payload would only see the first item. Wrapping in a named
    dict ({"downloads": [...]}) avoids that and gives one clean block."""
    for name in ("a.bin", "b.bin"):
        mock_server.add_file(name, os.urandom(1000))
        call_tool_from_thread(qtbot, mcp_server, "add_download", {
            "url": mock_server.url_for(name), "save_path": os.path.join(download_dir, name),
        })
    result = call_tool_from_thread(qtbot, mcp_server, "list_downloads", {})
    content = result[0] if isinstance(result, tuple) else result
    assert len(content) == 1  # exactly one content block, not one per download
    payload = extract_json(result)
    assert len(payload["downloads"]) == 2


def test_get_stats_tool(qtbot, mcp_server):
    result = call_tool_from_thread(qtbot, mcp_server, "get_stats", {})
    assert extract_json(result) == {}


def test_torrent_tool_raises_tool_error_without_torrent_support(qtbot, mcp_server):
    with pytest.raises(ToolError, match="[Tt]orrent support"):
        call_tool_from_thread(qtbot, mcp_server, "list_torrents", {})


@pytest.mark.timeout(30)
def test_full_stack_via_real_mcp_client_over_http(qtbot, tmp_path, thread_pool):
    """The one true end-to-end test: a real uvicorn server, a real port, a
    real API key, and the OFFICIAL mcp client SDK doing the actual
    JSON-RPC/session handshake over real HTTP -- not just calling our
    Python functions directly."""
    from adp.api.rest_server import build_app, ApiServerRunner
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    state_dir = tmp_path / "e2e_state"
    state_dir.mkdir()
    bridge = GuiBridge(poll_interval_ms=10)
    download_panel = DownloadPanel(state_dir=str(state_dir), thread_pool=thread_pool)
    qtbot.addWidget(download_panel)
    controller = AppController(bridge, download_panel, torrent_panel=None, stats_panel=None)
    key_store = ApiKeyStore(str(tmp_path / "keys"))
    app = build_app(controller, key_store)

    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    runner = ApiServerRunner(app, host="127.0.0.1", port=port)
    runner.start()
    try:
        result_box = {}

        async def run_client():
            async with streamablehttp_client(
                f"http://127.0.0.1:{port}/mcp", headers={"X-API-Key": key_store.key}
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    result_box["tool_names"] = {t.name for t in tools.tools}
                    call_result = await session.call_tool("list_downloads", {})
                    result_box["call_result"] = call_result

        def thread_target():
            try:
                asyncio.run(run_client())
            except BaseException as e:
                result_box["error"] = e

        t = threading.Thread(target=thread_target)
        t.start()
        # Wait for the FINAL step (call_result) or an error -- waiting on
        # tool_names alone would be satisfied as soon as list_tools()
        # returns, well before call_tool() has even run.
        qtbot.waitUntil(lambda: "call_result" in result_box or "error" in result_box, timeout=20000)
        t.join(timeout=5)

        if "error" in result_box:
            raise result_box["error"]
        assert "add_download" in result_box["tool_names"]
        assert not result_box["call_result"].isError
    finally:
        runner.stop()
        bridge.stop()
