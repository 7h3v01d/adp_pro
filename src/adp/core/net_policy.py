# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
"""Network destination policy for downloads started through the REST/MCP API.

Why: an API built for AI agents that fetches *any* URL is a server-side
request forgery (SSRF) capability. A mistaken, prompt-injected or
compromised agent could make ADP fetch http://192.168.1.1/ (your router),
http://127.0.0.1:<port>/ (local services) or internal hostnames -- and
because ADP *saves the response to disk*, the agent could then read it.
Some internal services even act on plain GET requests.

Policy: downloads added through the API may only reach public internet
addresses, unless the user enables "Allow API downloads from local network"
in Settings. Downloads added by hand in the GUI are never restricted.

Enforcement is layered so it can't be sidestepped:
  1. Before every request -- including every redirect hop -- the hostname is
     resolved and *all* its addresses must be public (a public URL that
     redirects to 127.0.0.1 is caught at the hop).
  2. After every TCP connect, the address actually connected to is checked
     (defeats DNS rebinding: a name that resolved public at check time but
     private at connect time).
Restricted sessions also ignore system proxy settings (trust_env=False): a
proxy would make step 2 see the proxy's address instead of the real one.
"""
from __future__ import annotations

import ipaddress
import socket
from typing import Optional
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

SETTING_KEY = "api_allow_private_network"


class BlockedDestinationError(requests.RequestException):
    """A restricted (API-added) download tried to reach a private, loopback,
    link-local or otherwise non-public address."""

    def __init__(self, host: str, address: Optional[str] = None):
        where = f"{host} ({address})" if address and address != host else host
        super().__init__(
            f"Blocked by the API network policy: {where} is a local/private network address. "
            "Downloads added through the API may only reach public internet addresses. "
            "To allow this, enable 'Allow API downloads from local network' in Settings -> API.")
        self.host = host
        self.address = address


def is_public_address(address: str) -> bool:
    """True only for globally routable unicast addresses. Covers RFC 1918,
    loopback (127/8, ::1), link-local (169.254/16 incl. cloud metadata,
    fe80::/10), CGNAT (100.64/10), unique-local (fc00::/7), unspecified,
    reserved, multicast, and IPv4-mapped IPv6 forms of all of these."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])  # drop IPv6 zone id
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def check_url(url: str) -> None:
    """Raises BlockedDestinationError if the URL's host is, or resolves to,
    any non-public address. Unresolvable hosts are left to fail normally."""
    parts = urlsplit(url)
    host = parts.hostname
    if not host:
        return
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        port = 443 if parts.scheme == "https" else 80
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return  # DNS failure: the request itself will fail with a clear error
    for info in infos:
        address = info[4][0]
        if not is_public_address(address):
            raise BlockedDestinationError(host, address)


# -- connect-time enforcement ---------------------------------------------------------
def _check_peer(conn, sock):
    try:
        address = sock.getpeername()[0]
    except OSError:
        return
    if not is_public_address(address):
        sock.close()
        raise BlockedDestinationError(conn.host, address)


class _GuardedHTTPConnection(HTTPConnection):
    def _new_conn(self):
        sock = super()._new_conn()
        _check_peer(self, sock)
        return sock


class _GuardedHTTPSConnection(HTTPSConnection):
    def _new_conn(self):
        sock = super()._new_conn()
        _check_peer(self, sock)
        return sock


class _GuardedHTTPPool(HTTPConnectionPool):
    ConnectionCls = _GuardedHTTPConnection


class _GuardedHTTPSPool(HTTPSConnectionPool):
    ConnectionCls = _GuardedHTTPSConnection


class _GuardedAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {"http": _GuardedHTTPPool, "https": _GuardedHTTPSPool}


def _find_blocked(exc, depth=0) -> Optional[BlockedDestinationError]:
    """urllib3 wraps errors raised during connect (in ProtocolError /
    MaxRetryError / requests.ConnectionError); dig ours back out."""
    if exc is None or depth > 8:
        return None
    if isinstance(exc, BlockedDestinationError):
        return exc
    for inner in (getattr(exc, "reason", None), exc.__cause__, exc.__context__,
                  *[a for a in getattr(exc, "args", ()) if isinstance(a, BaseException)]):
        found = _find_blocked(inner, depth + 1)
        if found:
            return found
    return None


class PublicOnlySession(requests.Session):
    """A requests.Session that can only reach public addresses (see module
    docstring). Drop-in for the sessions the downloader builds."""

    def __init__(self, max_retries=None):
        super().__init__()
        self.trust_env = False
        adapter = _GuardedAdapter(max_retries=max_retries) if max_retries is not None else _GuardedAdapter()
        self.mount("http://", adapter)
        self.mount("https://", adapter)

    def send(self, request, **kwargs):
        # Called once per hop -- requests' redirect handling re-enters send().
        check_url(request.url)
        try:
            return super().send(request, **kwargs)
        except requests.RequestException as e:
            blocked = _find_blocked(e)
            if blocked is not None and blocked is not e:
                raise blocked from None
            raise
