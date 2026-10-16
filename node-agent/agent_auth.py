"""
node-agent 调用鉴权(控制面 → node-agent 的 HMAC 请求签名)。

node-agent 以 root 运行在宿主上,能起停/快照任意 microVM、进任意 guest 执行命令、
写宿主文件 —— 只有控制面可以调用它。每个非健康检查请求都必须带以下头:

  X-Sbx-Agent-Timestamp       unix 秒
  X-Sbx-Agent-Nonce           每请求随机串(16~128 位 [A-Za-z0-9_-])
  X-Sbx-Agent-Content-SHA256  请求体 sha256 hex(无 body 时为空串的 sha256)
  X-Sbx-Agent-Signature       v1=<hex(HMAC-SHA256(key, canonical))>

canonical = "\n".join(["v1", timestamp, nonce, METHOD, request_target, content_sha256])
request_target 为 HTTP 请求行里的原样 path+query(即 BaseHTTPRequestHandler.path)。

密钥只下发给控制面与 node-agent(K8s Secret,见 terraform/stage2-control-plane),
缺失或过短时 node-agent 拒绝启动、所有受保护路由一律 401(fail closed)。
签名先于读取请求体校验(头部本身已绑定 body 哈希),读完 body 后再比对哈希,
未鉴权请求不会让 node-agent 缓冲任意大小的 body。

控制面侧的签名实现在 sandbox-api/node_agent_auth.py,两边 canonical 必须保持一致。
"""
from __future__ import annotations

import hashlib
import hmac
import re
import threading
import time

HEADER_TIMESTAMP = "X-Sbx-Agent-Timestamp"
HEADER_NONCE = "X-Sbx-Agent-Nonce"
HEADER_CONTENT_SHA256 = "X-Sbx-Agent-Content-SHA256"
HEADER_SIGNATURE = "X-Sbx-Agent-Signature"
AUTH_HEADERS = frozenset(h.lower() for h in (
    HEADER_TIMESTAMP, HEADER_NONCE, HEADER_CONTENT_SHA256, HEADER_SIGNATURE,
))

MIN_KEY_BYTES = 32
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

_TS_RE = re.compile(r"[0-9]{1,12}")
_NONCE_RE = re.compile(r"[A-Za-z0-9_-]{16,128}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SIG_RE = re.compile(r"v1=([0-9a-f]{64})")


class AuthError(Exception):
    """鉴权失败。reason 是固定枚举值,用于日志/指标标签(不含请求内容)。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def canonical_string(timestamp: str, nonce: str, method: str,
                     target: str, content_sha256: str) -> bytes:
    return "\n".join(
        ["v1", timestamp, nonce, method.upper(), target, content_sha256]
    ).encode()


def sign(key: bytes, timestamp: str, nonce: str, method: str,
         target: str, content_sha256: str) -> str:
    mac = hmac.new(key, canonical_string(timestamp, nonce, method, target,
                                         content_sha256), hashlib.sha256)
    return f"v1={mac.hexdigest()}"


def load_key(value: str = "", path: str = "") -> bytes:
    """优先读文件(K8s Secret volume),其次环境变量值。不足 MIN_KEY_BYTES 视为未配置。"""
    raw = ""
    if path:
        try:
            with open(path, encoding="utf-8") as stream:
                raw = stream.read()
        except OSError:
            raw = ""
    if not raw:
        raw = value
    key = raw.strip().encode()
    return key if len(key) >= MIN_KEY_BYTES else b""


class Verifier:
    """校验签名头并做 nonce 防重放(进程内,窗口 = max_skew_s 的两倍)。"""

    def __init__(self, key: bytes, max_skew_s: int = 300, max_nonces: int = 100_000):
        self._key = key
        self._max_skew_s = max_skew_s
        self._max_nonces = max_nonces
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return len(self._key) >= MIN_KEY_BYTES

    def verify_headers(self, headers, method: str, target: str) -> str:
        """校验成功返回声明的 body sha256(调用方读完 body 后需比对);失败抛 AuthError。"""
        if not self.configured:
            raise AuthError("not_configured")
        ts = headers.get(HEADER_TIMESTAMP, "")
        nonce = headers.get(HEADER_NONCE, "")
        content_sha = headers.get(HEADER_CONTENT_SHA256, "")
        signature = headers.get(HEADER_SIGNATURE, "")
        if not (ts or nonce or signature):
            raise AuthError("missing")
        # fullmatch:拒绝尾随换行、Unicode 数字等(str.isdigit 会放过 "²")
        if not _TS_RE.fullmatch(ts) or not _NONCE_RE.fullmatch(nonce) \
                or not _SHA256_RE.fullmatch(content_sha):
            raise AuthError("malformed")
        match = _SIG_RE.fullmatch(signature)
        if not match:
            raise AuthError("malformed")
        now = time.time()
        if abs(now - int(ts)) > self._max_skew_s:
            raise AuthError("expired")
        expected = sign(self._key, ts, nonce, method, target, content_sha)
        if not hmac.compare_digest(expected, f"v1={match.group(1)}"):
            raise AuthError("bad_signature")
        self._remember_nonce(nonce, now)
        return content_sha

    def _remember_nonce(self, nonce: str, now: float) -> None:
        with self._lock:
            if nonce in self._seen:
                raise AuthError("replay")
            if len(self._seen) >= self._max_nonces:
                horizon = now - 2 * self._max_skew_s
                self._seen = {n: t for n, t in self._seen.items() if t >= horizon}
                if len(self._seen) >= self._max_nonces:
                    raise AuthError("replay_cache_full")
            self._seen[nonce] = now


def body_matches(body: bytes, content_sha256: str) -> bool:
    return hmac.compare_digest(hashlib.sha256(body).hexdigest(), content_sha256)
