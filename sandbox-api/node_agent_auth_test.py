#!/usr/bin/env python3
"""控制面签名与 node-agent 校验必须互通(两边 canonical 格式一致)。"""
from __future__ import annotations

import hashlib
import importlib.util
import pathlib
import unittest
from unittest.mock import patch

from sandbox_api import node_agent_auth

_AGENT_AUTH_PATH = pathlib.Path(__file__).resolve().parent.parent / "node-agent" / "agent_auth.py"
_spec = importlib.util.spec_from_file_location("agent_auth", _AGENT_AUTH_PATH)
agent_auth = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(agent_auth)


class TestNodeAgentAuth(unittest.TestCase):
    def test_signed_headers_verify_on_node_agent(self) -> None:
        key = b"s" * 48
        body = b'{"id":"sbx-1"}'
        headers = node_agent_auth.signed_headers("POST", "/vm/destroy", body, key=key)
        self.assertEqual(
            headers[node_agent_auth.HEADER_CONTENT_SHA256],
            hashlib.sha256(body).hexdigest(),
        )
        verifier = agent_auth.Verifier(key)
        declared = verifier.verify_headers(headers, "POST", "/vm/destroy")
        self.assertTrue(agent_auth.body_matches(body, declared))
        # 换路径或换方法都不能通过
        other = node_agent_auth.signed_headers("POST", "/vm/destroy", body, key=key)
        with self.assertRaises(agent_auth.AuthError):
            verifier.verify_headers(other, "POST", "/vm/exec")

    def test_fails_closed_without_key(self) -> None:
        with patch.dict("os.environ", {"NODE_AGENT_AUTH_KEY": "", "NODE_AGENT_AUTH_KEY_FILE": ""}), \
                patch.object(node_agent_auth, "_KEY_CACHE", None):
            with self.assertRaises(RuntimeError):
                node_agent_auth.signed_headers("GET", "/health")

    def test_strip_auth_headers(self) -> None:
        forged = {node_agent_auth.HEADER_SIGNATURE: "v1=" + "0" * 64, "X-Other": "1"}
        self.assertEqual(node_agent_auth.strip_auth_headers(forged), {"X-Other": "1"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
