"""HMAC authentication for control-plane -> node-agent requests."""
from __future__ import annotations

import hashlib
import hmac
import os
import time
import uuid


AUTH_VERSION = "v1"
AUTH_SECRET = os.environ.get("NODE_AGENT_AUTH_SECRET", "")
AUTH_REQUIRED = os.environ.get(
    "NODE_AGENT_AUTH_REQUIRED", "0"
).strip().lower() in {"1", "true", "yes"}


def _body_bytes(body: bytes | bytearray | None) -> bytes:
    if body is None:
        return b""
    return bytes(body)


def canonical_request(
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    content_sha256: str,
) -> bytes:
    return "\n".join((
        AUTH_VERSION,
        timestamp,
        nonce,
        method.upper(),
        path,
        content_sha256,
    )).encode()


def auth_headers(
    method: str,
    path: str,
    body: bytes | bytearray | None = None,
    *,
    timestamp: int | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    """Return signed headers, or no headers when auth is explicitly disabled."""
    if not AUTH_SECRET:
        if AUTH_REQUIRED:
            raise RuntimeError(
                "NODE_AGENT_AUTH_REQUIRED=1 but NODE_AGENT_AUTH_SECRET is empty"
            )
        return {}
    timestamp_text = str(int(time.time() if timestamp is None else timestamp))
    nonce_text = nonce or uuid.uuid4().hex
    content_sha256 = hashlib.sha256(_body_bytes(body)).hexdigest()
    signature = hmac.new(
        AUTH_SECRET.encode(),
        canonical_request(
            method,
            path,
            timestamp_text,
            nonce_text,
            content_sha256,
        ),
        hashlib.sha256,
    ).hexdigest()
    return {
        "X-SBX-Auth-Version": AUTH_VERSION,
        "X-SBX-Timestamp": timestamp_text,
        "X-SBX-Nonce": nonce_text,
        "X-SBX-Content-SHA256": content_sha256,
        "X-SBX-Signature": signature,
    }
