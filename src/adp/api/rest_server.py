# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
"""REST API for Accelerated Downloader Pro.

Security posture (read before changing any of this):
- Binds to 127.0.0.1 ONLY. Never make this configurable to 0.0.0.0 without
  also rethinking auth -- this API can add/remove/pause downloads and
  torrents, which is real capability to hand to "whoever can reach this
  port".
- Every route except /health requires a valid X-API-Key header, checked
  with a constant-time comparison (see ApiKeyStore.verify).
- No CORS headers are added anywhere. Requiring a custom header (X-API-Key)
  forces a CORS preflight for any browser-based cross-origin request; with
  no matching Access-Control-Allow-* response, the browser blocks it. This
  is what stops a malicious web page from silently driving this API via a
  victim's browser (a classic localhost-API attack) even though it's
  unauthenticated-by-network-position.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional, List

import uvicorn
from fastapi import FastAPI, Request
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from adp.api.auth import ApiKeyStore
from adp.api.controller import AppController, ApiError
from adp.api.mcp_tools import build_mcp_server

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------
class AddDownloadRequest(BaseModel):
    url: str
    save_path: Optional[str] = None
    category: Optional[str] = None
    num_threads: int = Field(default=4, ge=1, le=16)
    checksum: Optional[str] = None
    speed_limit_bps: int = 0
    # None -> use the app-wide setting (which defaults to True). Only set
    # False for a server whose certificate the caller has decided to trust.
    verify_tls: Optional[bool] = None
    # Replace a pre-existing non-resumable file at save_path. Defaults False so
    # an unrelated existing file is never silently truncated.
    overwrite: bool = False


class AddTorrentRequest(BaseModel):
    magnet_uri: Optional[str] = None
    torrent_file_base64: Optional[str] = None
    torrent_file_name: str = "upload.torrent"
    save_path: Optional[str] = None
    category: str = "Torrents"
    seed_ratio_limit: float = 0.0


class SelectTorrentFilesRequest(BaseModel):
    selected_indices: List[int]


class SearchRequest(BaseModel):
    text: str
    category: Optional[str] = None      # provider-neutral hint, e.g. "software"
    providers: Optional[List[str]] = None
    limit: int = Field(default=50, ge=1, le=200)


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------
class ApiKeyMiddleware(BaseHTTPMiddleware):
    OPEN_PATHS = {"/health"}

    def __init__(self, app, key_store: ApiKeyStore):
        super().__init__(app)
        self.key_store = key_store

    async def dispatch(self, request: Request, call_next):
        if request.url.path in self.OPEN_PATHS:
            return await call_next(request)
        provided = request.headers.get("X-API-Key", "")
        if not self.key_store.verify(provided):
            return JSONResponse(status_code=401, content={"detail": "Missing or invalid X-API-Key header."})
        return await call_next(request)


def _api_error_handler(request: Request, exc: ApiError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


def build_app(controller: AppController, key_store: ApiKeyStore) -> FastAPI:
    mcp_server = build_mcp_server(controller)
    mcp_asgi_app = mcp_server.streamable_http_app()

    app = FastAPI(
        title="Accelerated Downloader Pro API",
        description="Control downloads and torrents programmatically -- built for AI agents and scripts.",
        version="1.0.0",
        lifespan=mcp_asgi_app.router.lifespan_context,
    )
    app.add_middleware(ApiKeyMiddleware, key_store=key_store)
    app.add_exception_handler(ApiError, _api_error_handler)
    app.mount("/mcp", mcp_asgi_app)

    @app.get("/health")
    def health():
        return {"status": "ok", "torrent_support_available": controller.torrent_support_available}

    # -- downloads -----------------------------------------------------
    @app.get("/downloads")
    def list_downloads():
        return controller.list_downloads()

    @app.post("/downloads")
    def add_download(body: AddDownloadRequest):
        return controller.add_download(
            url=body.url, save_path=body.save_path, category=body.category,
            num_threads=body.num_threads, checksum=body.checksum, speed_limit_bps=body.speed_limit_bps,
            verify_tls=body.verify_tls, overwrite=body.overwrite,
        )

    @app.get("/downloads/{download_id}")
    def get_download(download_id: str):
        return controller.get_download(download_id)

    @app.post("/downloads/{download_id}/pause")
    def pause_download(download_id: str):
        return controller.pause_download(download_id)

    @app.post("/downloads/{download_id}/resume")
    def resume_download(download_id: str):
        return controller.resume_download(download_id)

    @app.post("/downloads/{download_id}/stop")
    def stop_download(download_id: str):
        return controller.stop_download(download_id)

    @app.post("/downloads/{download_id}/retry")
    def retry_download(download_id: str):
        return controller.retry_download(download_id)

    @app.delete("/downloads/{download_id}")
    def remove_download(download_id: str):
        return controller.remove_download(download_id)

    # -- torrents ------------------------------------------------------
    @app.get("/torrents")
    def list_torrents():
        return controller.list_torrents()

    @app.post("/torrents")
    def add_torrent(body: AddTorrentRequest):
        return controller.add_torrent(
            magnet_uri=body.magnet_uri, torrent_file_base64=body.torrent_file_base64,
            torrent_file_name=body.torrent_file_name, save_path=body.save_path,
            category=body.category, seed_ratio_limit=body.seed_ratio_limit,
        )

    @app.get("/torrents/{torrent_id}")
    def get_torrent(torrent_id: str):
        return controller.get_torrent(torrent_id)

    @app.post("/torrents/{torrent_id}/pause")
    def pause_torrent(torrent_id: str):
        return controller.pause_torrent(torrent_id)

    @app.post("/torrents/{torrent_id}/resume")
    def resume_torrent(torrent_id: str):
        return controller.resume_torrent(torrent_id)

    @app.post("/torrents/{torrent_id}/force_recheck")
    def force_recheck_torrent(torrent_id: str):
        return controller.force_recheck_torrent(torrent_id)

    @app.post("/torrents/{torrent_id}/select_files")
    def select_torrent_files(torrent_id: str, body: SelectTorrentFilesRequest):
        return controller.select_torrent_files(torrent_id, body.selected_indices)

    @app.delete("/torrents/{torrent_id}")
    def remove_torrent(torrent_id: str, delete_files: bool = False):
        return controller.remove_torrent(torrent_id, delete_files=delete_files)

    # -- search ----------------------------------------------------------
    @app.post("/search")
    def search_torrents(body: SearchRequest):
        """Searches the enabled torrent indexers, returning deduplicated,
        ranked results. A failing provider is reported in `errors` and never
        fails the search. Feed a result's `magnet` straight to POST
        /torrents to download it."""
        return controller.search_torrents(
            text=body.text, category=body.category,
            providers=body.providers, limit=body.limit,
        )

    @app.get("/search/providers")
    def list_search_providers():
        return controller.list_search_providers()

    # -- stats -----------------------------------------------------------
    @app.get("/stats")
    def get_stats():
        return controller.get_stats()

    return app


class _ThreadedUvicornServer(uvicorn.Server):
    """uvicorn.Server tries to install OS signal handlers by default, which
    only works on the main thread -- we run it on a background thread
    instead, so that's a no-op here."""
    def install_signal_handlers(self):
        pass


class ApiServerRunner:
    """Owns the REST API's background thread/event loop lifecycle."""

    def __init__(self, app: FastAPI, host: str = "127.0.0.1", port: int = 8765):
        self.host = host
        self.port = port
        config = uvicorn.Config(app, host=host, port=port, log_level="warning", loop="asyncio")
        self.server = _ThreadedUvicornServer(config)
        self._thread: Optional[threading.Thread] = None
        self.start_exception: Optional[BaseException] = None

    def start(self, timeout: float = 5.0) -> bool:
        """Starts the server and waits for it to either report ready or
        fail to bind. Returns True on success; on failure, start_exception
        holds the underlying error (most commonly OSError: address already
        in use) so callers can decide whether to retry on another port."""
        def _run():
            try:
                self.server.run()
            except BaseException as e:  # noqa: BLE001 -- surfaced via start_exception, not swallowed
                self.start_exception = e

        self._thread = threading.Thread(target=_run, name="adp-rest-api", daemon=True)
        self._thread.start()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.server.started:
                return True
            if self.start_exception is not None or not self._thread.is_alive():
                return False
            time.sleep(0.02)
        return self.server.started

    def stop(self):
        self.server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


def start_api_server_with_fallback(app: FastAPI, host: str = "127.0.0.1", preferred_port: int = 8765,
                                    max_attempts: int = 20) -> ApiServerRunner:
    """Tries preferred_port first (the common case, and what users will
    have configured any AI tool/script to expect), and only searches for a
    free port if that one's taken -- e.g. a previous instance of this app
    still shutting down, or something else already using it."""
    port = preferred_port
    for attempt in range(max_attempts):
        runner = ApiServerRunner(app, host=host, port=port)
        if runner.start():
            if port != preferred_port:
                logger.warning(f"Port {preferred_port} was unavailable; API server started on {port} instead.")
            return runner
        logger.warning(f"Could not bind API server to {host}:{port} ({runner.start_exception}); trying another port.")
        port += 1
    raise RuntimeError(f"Could not start the API server after {max_attempts} attempts starting from port {preferred_port}.")
