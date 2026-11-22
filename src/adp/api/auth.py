# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Leon Priest (7h3v01d)
"""Generates and persists a local API key used to authenticate requests to
the REST/MCP API. The API always binds to 127.0.0.1 only, but a full-control
API (add/pause/remove downloads and torrents) reachable by *any* local
process or, worse, a malicious web page via a browser fetch to localhost,
is a real risk without this.

Threat boundary, stated precisely: the key protects against *unauthenticated*
local clients and web-origin (browser) attacks. It does NOT protect against
hostile software running as the same OS user -- that can read the key file
(or ADP's memory) like any other file in the user's profile. That limit is
inherent to local bearer-token auth.
"""

from __future__ import annotations

import logging
import os
import secrets

logger = logging.getLogger(__name__)

KEY_FILE_NAME = "api_key.txt"


class ApiKeyStore:
    def __init__(self, state_dir: str):
        os.makedirs(state_dir, exist_ok=True)
        self.key_file = os.path.join(state_dir, KEY_FILE_NAME)
        self._key = None

    @property
    def key(self) -> str:
        if self._key is None:
            self._key = self._load_or_create()
        return self._key

    def _load_or_create(self) -> str:
        if os.path.exists(self.key_file):
            try:
                with open(self.key_file, 'r') as f:
                    existing = f.read().strip()
                if existing:
                    return existing
            except OSError as e:
                logger.error(f"Failed to read API key file, generating a new one: {e}")
        return self._generate_and_save()

    def regenerate(self) -> str:
        self._key = self._generate_and_save()
        return self._key

    def _generate_and_save(self) -> str:
        new_key = secrets.token_urlsafe(32)
        try:
            # Owner-only permissions on POSIX. On Windows the mode argument
            # does not set an ACL; the file is protected only by living in
            # the user's own profile (%APPDATA%), i.e. readable by any
            # process running as this user -- see the module docstring.
            fd = os.open(self.key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as f:
                f.write(new_key)
        except OSError as e:
            logger.error(f"Failed to save API key: {e}")
        return new_key

    def verify(self, provided: str) -> bool:
        if not provided:
            return False
        # Constant-time comparison to avoid leaking key contents via timing.
        return secrets.compare_digest(provided, self.key)
