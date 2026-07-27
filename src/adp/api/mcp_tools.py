# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
"""MCP (Model Context Protocol) server for Accelerated Downloader Pro.

Exposes the exact same operations as the REST API, via mcp.server.fastmcp,
so an MCP-compatible AI assistant can add/manage downloads and torrents as
native tool calls. Built as a mountable ASGI app (streamable-http
transport) rather than its own separate server/port, so it's mounted
directly into the REST API's FastAPI app in rest_server.py -- one port,
one API key, one auth middleware covering both surfaces.

Every tool is a thin wrapper that calls the SAME AppController method the
REST API calls, so there is exactly one implementation of "what does
add_download mean" regardless of which interface a caller used.
"""

from __future__ import annotations

from typing import Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from adp.api.controller import AppController


def build_mcp_server(controller: AppController) -> FastMCP:
    mcp = FastMCP(
        "Accelerated Downloader Pro",
        streamable_http_path="/",
        # DNS-rebinding protection: only accept requests whose Host header
        # matches where this server actually listens. Without this, a
        # malicious web page could point a victim's browser at
        # "evil.com" resolving to 127.0.0.1 and potentially reach this API.
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*"],
            allowed_origins=[],
        ),
        instructions=(
            "Control a running Accelerated Downloader Pro instance: add, list, "
            "pause/resume/stop/retry/remove HTTP downloads, and (when torrent "
            "support is installed) add/list/pause/resume/remove torrents by "
            "magnet link or .torrent file, select which files in a torrent to "
            "download, and read session/lifetime stats."
        ),
    )

    # -- downloads ----------------------------------------------------------
    @mcp.tool()
    def list_downloads() -> dict:
        """Lists every HTTP download currently known to the app (any status:
        downloading, paused, completed, error, stopped)."""
        return {"downloads": controller.list_downloads()}

    @mcp.tool()
    def get_download(download_id: str) -> dict:
        """Gets the current status of one HTTP download by its id."""
        return controller.get_download(download_id)

    @mcp.tool()
    def add_download(url: str, save_path: Optional[str] = None, category: Optional[str] = None,
                      num_threads: int = 4, checksum: Optional[str] = None,
                      speed_limit_bps: int = 0, verify_tls: Optional[bool] = None,
                      overwrite: bool = False) -> dict:
        """Adds a new HTTP(S) download. url must be a real link (starting
        with http:// or https://), not a page's visible link text.
        save_path defaults to the app's data directory if omitted.
        speed_limit_bps of 0 means unlimited. verify_tls of None uses the
        app-wide setting (default True: TLS certificates are verified);
        pass False only for a server whose certificate you've decided to
        trust despite it failing verification. overwrite defaults to False:
        if save_path already holds a file that isn't a resumable ADP download,
        the request is refused rather than destroying it -- pass overwrite=True
        only when you intend to replace the existing file."""
        return controller.add_download(
            url=url, save_path=save_path, category=category, num_threads=num_threads,
            checksum=checksum, speed_limit_bps=speed_limit_bps, verify_tls=verify_tls,
            overwrite=overwrite,
        )

    @mcp.tool()
    def pause_download(download_id: str) -> dict:
        """Pauses an in-progress download."""
        return controller.pause_download(download_id)

    @mcp.tool()
    def resume_download(download_id: str) -> dict:
        """Resumes a paused download."""
        return controller.resume_download(download_id)

    @mcp.tool()
    def stop_download(download_id: str) -> dict:
        """Stops a download and discards its partial progress file (the
        partially-downloaded file itself is left in place)."""
        return controller.stop_download(download_id)

    @mcp.tool()
    def retry_download(download_id: str) -> dict:
        """Retries a download that previously failed or was stopped."""
        return controller.retry_download(download_id)

    @mcp.tool()
    def remove_download(download_id: str) -> dict:
        """Removes a download from the app's list. Does not delete the
        downloaded file itself, only the entry."""
        return controller.remove_download(download_id)

    # -- torrents ------------------------------------------------------
    @mcp.tool()
    def list_torrents() -> dict:
        """Lists every torrent currently known to the app. Fails with a
        clear error if torrent support isn't available in this install."""
        return {"torrents": controller.list_torrents()}

    @mcp.tool()
    def get_torrent(torrent_id: str) -> dict:
        """Gets the current status of one torrent by its id, including its
        file list once metadata has resolved."""
        return controller.get_torrent(torrent_id)

    @mcp.tool()
    def add_torrent(magnet_uri: Optional[str] = None, torrent_file_base64: Optional[str] = None,
                     torrent_file_name: str = "upload.torrent", save_path: Optional[str] = None,
                     category: str = "Torrents", seed_ratio_limit: float = 0.0) -> dict:
        """Adds a new torrent, via EITHER a magnet_uri OR the base64-encoded
        bytes of a .torrent file (torrent_file_base64) -- provide exactly
        one. seed_ratio_limit of 0 means seed indefinitely; otherwise the
        torrent automatically stops seeding once its upload/download ratio
        reaches that value."""
        return controller.add_torrent(
            magnet_uri=magnet_uri, torrent_file_base64=torrent_file_base64,
            torrent_file_name=torrent_file_name, save_path=save_path,
            category=category, seed_ratio_limit=seed_ratio_limit,
        )

    @mcp.tool()
    def pause_torrent(torrent_id: str) -> dict:
        """Pauses a torrent (stops both downloading and seeding)."""
        return controller.pause_torrent(torrent_id)

    @mcp.tool()
    def resume_torrent(torrent_id: str) -> dict:
        """Resumes a paused torrent."""
        return controller.resume_torrent(torrent_id)

    @mcp.tool()
    def remove_torrent(torrent_id: str, delete_files: bool = False) -> dict:
        """Removes a torrent from the app. If delete_files is true, also
        deletes the downloaded data from disk -- use with care."""
        return controller.remove_torrent(torrent_id, delete_files=delete_files)

    @mcp.tool()
    def force_recheck_torrent(torrent_id: str) -> dict:
        """Forces libtorrent to re-verify already-downloaded pieces on disk
        against the torrent's hashes (useful if the files may have been
        modified or corrupted outside the app)."""
        return controller.force_recheck_torrent(torrent_id)

    @mcp.tool()
    def select_torrent_files(torrent_id: str, selected_indices: list) -> dict:
        """Chooses which files in a multi-file torrent to actually download
        -- selected_indices is the list of file indices (from get_torrent's
        `files` field) to keep; every other file in the torrent is skipped."""
        return controller.select_torrent_files(torrent_id, selected_indices)

    # -- stats -----------------------------------------------------------
    @mcp.tool()
    def get_stats() -> dict:
        """Gets session (this run) and lifetime (persisted) transfer
        totals, plus swarm health (active torrents, connected peers/seeds)
        when torrent support is available."""
        return controller.get_stats()

    # -- search ----------------------------------------------------------
    @mcp.tool()
    def search_torrents(text: str, category: Optional[str] = None,
                         providers: Optional[list] = None, limit: int = 25) -> dict:
        """Searches the app's enabled torrent indexers and returns
        deduplicated, ranked results (best first). Each result includes a
        `magnet` link that can be passed directly to add_torrent, plus
        seeders/leechers, size_bytes, and which providers reported it.
        `category` is an optional hint: software, audio, video, books,
        games, or other. A failing indexer appears in `errors` and never
        fails the search. Only download content the user has the right to
        download."""
        return controller.search_torrents(
            text=text, category=category, providers=providers, limit=limit,
        )

    @mcp.tool()
    def list_search_providers() -> dict:
        """Lists every torrent search provider the app knows, with its
        enabled state -- useful before search_torrents to see what's
        available or to explain why a search returned nothing."""
        return {"providers": controller.list_search_providers()}

    return mcp
