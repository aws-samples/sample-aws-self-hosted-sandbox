"""
控制面 → node-agent 请求签名(HMAC-SHA256)。

node-agent 对所有非健康检查路由强制校验签名(见 node-agent/agent_auth.py,两边 canonical
格式必须一致)。密钥来自 K8s Secret(terraform stage2 生成),只下发给控制面与 node-agent:
  NODE_AGENT_AUTH_KEY_FILE  密钥文件路径(优先)
  NODE_AGENT_AUTH_KEY       密钥值
未配置时 signed_headers 抛 RuntimeError —— 宁可调用失败,也不发出未签名请求。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time

HEADER_TIMESTAMP = "X-Sbx-Agent-Timestamp"
HEADER_NONCE = "X-Sbx-Agent-Nonce"
HEADER_CONTENT_SHA256 = "X-Sbx-Agent-Content-SHA256"
HEADER_SIGNATURE = "X-Sbx-Agent-Signature"
AUTH_HEADERS = frozenset(h.lower() for h in (
    HEADER_TIMESTAMP, HEADER_NONCE, HEADER_CONTENT_SHA256, HEADER_SIGNATURE,
))
MIN_KEY_BYTES = 32

_KEY_CACHE: bytes | None = None


def _load_key() -> bytes:
    global _KEY_CACHE
    if _KEY_CACHE:
        return _KEY_CACHE
    raw = ""
    path = os.environ.get("NODE_AGENT_AUTH_KEY_FILE", "")
    if path:
        try:
            with open(path, encoding="utf-8") as stream:
                raw = stream.read()
        except OSError:
            raw = ""
    if not raw:
        raw = os.environ.get("NODE_AGENT_AUTH_KEY", "")
    key = raw.strip().encode()
    if len(key) < MIN_KEY_BYTES:
        raise RuntimeError(
            "node-agent auth key not configured: set NODE_AGENT_AUTH_KEY_FILE or "
            f"NODE_AGENT_AUTH_KEY (>= {MIN_KEY_BYTES} bytes)"
        )
    _KEY_CACHE = key
    return key


def canonical_string(timestamp: str, nonce: str, method: str,
                     target: str, content_sha256: str) -> bytes:
    return "\n".join(
        ["v1", timestamp, nonce, method.upper(), target, content_sha256]
    ).encode()


def signed_headers(method: str, target: str, body: bytes | None = None,
                   key: bytes | None = None) -> dict[str, str]:
    """返回需附加到 node-agent 请求上的签名头。target = 请求行里的 path(+query)。"""
    key = key or _load_key()
    timestamp = str(int(time.time()))
    nonce = secrets.token_urlsafe(24)
    content_sha = hashlib.sha256(body or b"").hexdigest()
    mac = hmac.new(key, canonical_string(timestamp, nonce, method, target, content_sha),
                   hashlib.sha256).hexdigest()
    return {
        HEADER_TIMESTAMP: timestamp,
        HEADER_NONCE: nonce,
        HEADER_CONTENT_SHA256: content_sha,
        HEADER_SIGNATURE: f"v1={mac}",
    }


def strip_auth_headers(headers: dict[str, str]) -> dict[str, str]:
    """去掉外部客户端自带的同名头,防止伪造/干扰签名。"""
    return {k: v for k, v in headers.items() if k.lower() not in AUTH_HEADERS}
