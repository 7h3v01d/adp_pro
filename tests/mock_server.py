# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
"""A minimal, dependency-free HTTP server that supports Range requests,
used so the pytest suite can exercise real socket I/O without touching
the network. Runs in a background thread per test.
"""
from __future__ import annotations

import hashlib
import http.server
import os
import re
import socketserver
import ssl
import subprocess
import tempfile
import threading
import time
from typing import Dict, Optional


class RangeRequestHandler(http.server.BaseHTTPRequestHandler):
    # Populated by MockDownloadServer before serving.
    files: Dict[str, bytes] = {}
    # download_id -> number of GET/HEAD requests seen, for fault injection
    fail_after_bytes: Dict[str, int] = {}
    unreliable_paths: set = set()
    request_counts: Dict[str, int] = {}
    accept_ranges = True
    # path -> approx bytes/sec. Loopback transfers otherwise finish in
    # microseconds, which makes pause/stop timing tests racy: the download
    # completes before the test's pause() even lands.
    throttle_bps: Dict[str, int] = {}
    # path -> extra response headers (e.g. Content-Disposition)
    extra_headers: Dict[str, Dict[str, str]] = {}

    def _send_extra_headers(self, path):
        for name, value in self.extra_headers.get(path, {}).items():
            self.send_header(name, value)

    def log_message(self, fmt, *args):
        pass  # silence default stderr logging during tests

    def _get_body(self):
        path = self.path.lstrip('/')
        return self.files.get(path)

    def _is_forbidden(self):
        return self.path.lstrip('/') in self.forbidden_paths

    def do_HEAD(self):
        if self._is_forbidden():
            self.send_response(403)
            self.end_headers()
            return
        body = self._get_body()
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        if self.accept_ranges:
            self.send_header('Accept-Ranges', 'bytes')
        self.send_header('ETag', f'"{hashlib.md5(body).hexdigest()}"')
        self._send_extra_headers(self.path.lstrip('/'))
        self.end_headers()

    def do_GET(self):
        if self._is_forbidden():
            self.send_response(403)
            self.end_headers()
            return
        body = self._get_body()
        if body is None:
            self.send_response(404)
            self.end_headers()
            return

        path = self.path.lstrip('/')
        self.request_counts[path] = self.request_counts.get(path, 0) + 1

        total = len(body)
        start, end = 0, total - 1
        range_header = self.headers.get('Range')
        status = 200
        if range_header and self.accept_ranges:
            m = re.match(r'bytes=(\d+)-(\d*)', range_header)
            if m:
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else total - 1
                status = 206

        chunk = body[start:end + 1]

        # Fault injection: simulate a connection that dies partway through.
        limit = self.fail_after_bytes.get(path)
        truncate_at = len(chunk)
        if limit is not None and start < limit:
            truncate_at = min(len(chunk), max(0, limit - start))

        self.send_response(status)
        self.send_header('Content-Length', str(len(chunk)))
        if status == 206:
            # A real server returns Content-Range with every 206. The engine
            # now validates this, so the mock must behave correctly too.
            self.send_header('Content-Range', f'bytes {start}-{end}/{total}')
        if self.accept_ranges:
            self.send_header('Accept-Ranges', 'bytes')
        self.send_header('ETag', f'"{hashlib.md5(body).hexdigest()}"')
        self._send_extra_headers(path)
        self.end_headers()
        try:
            bps = self.throttle_bps.get(path)
            if bps:
                slice_size = max(1, bps // 50)  # ~50 write slices per second
                sent = 0
                while sent < truncate_at:
                    self.wfile.write(chunk[sent:sent + slice_size])
                    self.wfile.flush()
                    sent += slice_size
                    time.sleep(0.02)
            else:
                self.wfile.write(chunk[:truncate_at])
            if truncate_at < len(chunk):
                self.close_connection = True
        except (BrokenPipeError, ConnectionResetError):
            pass


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class MockDownloadServer:
    """Context-manager-friendly wrapper that starts a RangeRequestHandler
    server on a background thread on an OS-assigned free port."""

    def __init__(self, tls: bool = False):
        handler_cls = type('BoundHandler', (RangeRequestHandler,), {
            'files': {}, 'fail_after_bytes': {}, 'request_counts': {}, 'accept_ranges': True,
            'throttle_bps': {}, 'extra_headers': {}, 'forbidden_paths': set(),
        })
        self.handler_cls = handler_cls
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), handler_cls)
        self.tls = tls
        if tls:
            # Self-signed cert for 127.0.0.1 -- deliberately NOT trusted by
            # any CA store, so a client verifying certificates must reject
            # it. This is exactly the property the verify_tls tests need.
            cert_dir = tempfile.mkdtemp(prefix="adp-test-tls-")
            cert_path = os.path.join(cert_dir, "cert.pem")
            key_path = os.path.join(cert_dir, "key.pem")
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                 "-keyout", key_path, "-out", cert_path, "-days", "1",
                 "-subj", "/CN=127.0.0.1",
                 "-addext", "subjectAltName=IP:127.0.0.1"],
                check=True, capture_output=True,
            )
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cert_path, key_path)
            self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def url_for(self, path: str) -> str:
        scheme = "https" if self.tls else "http"
        return f"{scheme}://127.0.0.1:{self.port}/{path}"

    def add_file(self, path: str, content: bytes):
        self.handler_cls.files[path] = content

    def set_accept_ranges(self, value: bool):
        self.handler_cls.accept_ranges = value

    def set_forbidden(self, path: str):
        """Makes `path` return HTTP 403 to both HEAD and GET -- simulating a
        server that blocks download managers / non-browser requests, exactly
        like link.testfile.org did in the field."""
        self.handler_cls.forbidden_paths.add(path)

    def set_throttle(self, path: str, bytes_per_second: int):
        """Caps how fast `path` is served, so tests can reliably interact
        with a download mid-flight instead of racing loopback speeds."""
        self.handler_cls.throttle_bps[path] = bytes_per_second

    def set_extra_headers(self, path: str, headers: dict):
        """Attaches extra response headers (e.g. Content-Disposition) to
        every HEAD/GET response for `path`."""
        self.handler_cls.extra_headers[path] = dict(headers)

    def fail_path_after(self, path: str, byte_offset: int):
        """Causes the server to drop the connection once `byte_offset` bytes
        into the file have been sent for the given path."""
        self.handler_cls.fail_after_bytes[path] = byte_offset

    def clear_fault(self, path: str):
        self.handler_cls.fail_after_bytes.pop(path, None)

    def request_count(self, path: str) -> int:
        return self.handler_cls.request_counts.get(path, 0)
