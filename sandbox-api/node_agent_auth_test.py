#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import hmac
import unittest
from unittest.mock import patch

from sandbox_api import node_agent_auth


class TestNodeAgentAuth(unittest.TestCase):
    def test_signed_headers_cover_method_path_and_body(self) -> None:
        body = b'{"id":"sbx-1"}'
        with patch.object(
            node_agent_auth, "AUTH_SECRET", "s" * 48
        ):
            headers = node_agent_auth.auth_headers(
                "POST",
                "/vm/destroy",
                body,
                timestamp=1_700_000_000,
                nonce="nonce-0123456789abcdef",
            )

        content_hash = hashlib.sha256(body).hexdigest()
        expected = hmac.new(
            ("s" * 48).encode(),
            node_agent_auth.canonical_request(
                "POST",
                "/vm/destroy",
                "1700000000",
                "nonce-0123456789abcdef",
                content_hash,
            ),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(
            headers["X-SBX-Content-SHA256"],
            content_hash,
        )
        self.assertEqual(headers["X-SBX-Signature"], expected)

    def test_required_auth_fails_closed_without_secret(self) -> None:
        with (
            patch.object(node_agent_auth, "AUTH_SECRET", ""),
            patch.object(node_agent_auth, "AUTH_REQUIRED", True),
        ):
            with self.assertRaisesRegex(RuntimeError, "SECRET is empty"):
                node_agent_auth.auth_headers("GET", "/health")


if __name__ == "__main__":
    unittest.main(verbosity=2)
