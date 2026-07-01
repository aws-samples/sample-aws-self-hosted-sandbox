"""
FirecrackerDriver — 通过每节点 node-agent HTTP API 操作裸 Firecracker microVM。

职责:
  - 选节点(按 /health 水位)
  - 分配 tap_idx(DynamoDB 原子 counter)
  - 把控制面意图转成 node-agent 调用
  - 触发 S3 快照上传/拉取(由 node-agent 执行)

不直接碰 Firecracker socket / tap / jailer —— 那些都在 node-agent 里。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from sandbox_api import db
from sandbox_api.driver import Capabilities, SandboxSpec, UnsupportedOperation

# node-agent 监听端口(DaemonSet hostNetwork 模式)
NODE_AGENT_PORT = int(os.environ.get("NODE_AGENT_PORT", "8002"))
KERNEL_PATH     = os.environ.get("FC_KERNEL_PATH", "/opt/sbx/vmlinux")
S3_BUCKET       = os.environ.get("SNAPSHOT_S3_BUCKET", "")
# 统一路径约定:所有节点把沙盒文件放同一前缀(跨机 resume 必须)
SBX_BASE        = "/var/lib/sbx"


class FirecrackerDriver:

    def capabilities(self) -> Capabilities:
        return Capabilities(suspend_resume=True, warm_pool=True, migrate=True)

    # ------------------------------------------------------------------
    # create
    # ------------------------------------------------------------------

    def create(self, sandbox_id: str, spec: SandboxSpec) -> dict:
        tap_idx = db.alloc_tap_idx()
        node_id = self._pick_node()
        rootfs  = f"{SBX_BASE}/{sandbox_id}/rootfs.ext4"

        self._agent(node_id, "POST", "/vm/create", {
            "id":          sandbox_id,
            "rootfs_path": rootfs,
            "tap_idx":     tap_idx,
            "cpu":         spec.cpu,
            "mem_mib":     spec.mem_mib,
            "kernel":      KERNEL_PATH,
            "env":         spec.env,
        })
        # agent 回包含 guest_ip
        info = self._agent(node_id, "GET", f"/vm/{sandbox_id}")
        return {
            "node":      node_id,
            "tap_idx":   tap_idx,
            "guest_ip":  info.get("ip", ""),
        }

    # ------------------------------------------------------------------
    # destroy
    # ------------------------------------------------------------------

    def destroy(self, sandbox_id: str, record: dict) -> None:
        node = record.get("node")
        if node:
            try:
                self._agent(node, "POST", "/vm/destroy", {"id": sandbox_id})
            except Exception:
                pass   # 节点已挂/沙盒已不存在,destroy 幂等
        if record.get("tap_idx"):
            db.release_tap_idx(record["tap_idx"])

    # ------------------------------------------------------------------
    # suspend  (Fly suspend 同款:暂停 → Full/diff 快照 → kill → 释放 RAM)
    # ------------------------------------------------------------------

    def suspend(self, sandbox_id: str, record: dict) -> dict:
        node        = record["node"]
        snap_local  = f"{SBX_BASE}/{sandbox_id}/snap"
        snap_s3     = f"s3://{S3_BUCKET}/sbx/{sandbox_id}/" if S3_BUCKET else ""

        # suspend 在慢速 EBS gp3 上 Full 快照可能 ~100s，给足 300s 避免 API 超时
        resp = self._agent(node, "POST", "/vm/suspend", {
            "id":                   sandbox_id,
            "snapshot_local_path":  snap_local,
            "s3_prefix":            snap_s3,   # node-agent 异步上传
        }, timeout=300)
        return {
            "snapshot_s3":           snap_s3,
            "snapshot_size_bytes":   resp.get("mem_file_bytes", 0),
            "snapshot_create_time_s": resp.get("snapshot_create_time_s", 0),
        }

    # ------------------------------------------------------------------
    # resume  (从快照秒级恢复;可在不同节点)
    # ------------------------------------------------------------------

    def resume(self, sandbox_id: str, record: dict) -> dict:
        snap_local = f"{SBX_BASE}/{sandbox_id}/snap"
        rootfs     = f"{SBX_BASE}/{sandbox_id}/rootfs.ext4"
        snap_s3    = record.get("snapshot_s3", "")

        node = self._pick_node()

        self._agent(node, "POST", "/vm/resume", {
            "id":                  sandbox_id,
            "snapshot_local_path": snap_local,
            "rootfs_path":         rootfs,        # 路径约定,跨机一致
            "tap_idx":             record["tap_idx"],
            "s3_prefix":           snap_s3,       # node-agent 若本地无缓存则从 S3 拉
        })
        info = self._agent(node, "GET", f"/vm/{sandbox_id}")
        return {
            "node":     node,
            "guest_ip": info.get("ip", ""),
        }

    # ------------------------------------------------------------------
    # exec
    # ------------------------------------------------------------------

    def exec(self, sandbox_id: str, record: dict, cmd: str) -> tuple[int, str, str]:
        resp = self._agent(record["node"], "POST", "/vm/exec", {
            "id":  sandbox_id,
            "cmd": cmd,
        })
        return resp.get("rc", -1), resp.get("stdout", ""), resp.get("stderr", "")

    # ------------------------------------------------------------------
    # get_runtime_state
    # ------------------------------------------------------------------

    def get_runtime_state(self, sandbox_id: str, record: dict) -> str:
        node = record.get("node")
        if not node:
            return "unknown"
        try:
            info = self._agent(node, "GET", f"/vm/{sandbox_id}")
            return info.get("state", "unknown")
        except Exception:
            return "unknown"

    # ------------------------------------------------------------------
    # 节点选择:按 node-agent /health 的 free_mem_mib 挑水位最高的
    # ------------------------------------------------------------------

    def _pick_node(self) -> str:
        nodes = self._list_metal_nodes()
        if not nodes:
            raise RuntimeError("no available .metal nodes")

        best_node, best_mem = None, -1
        for node_id in nodes:
            try:
                h = self._agent(node_id, "GET", "/health")
                free = h.get("free_mem_mib", 0)
                if free > best_mem:
                    best_mem, best_node = free, node_id
            except Exception:
                continue

        if best_node is None:
            raise RuntimeError("all nodes unreachable")
        return best_node

    def _list_metal_nodes(self) -> list[str]:
        """
        从 DynamoDB node registry 或 EC2 DescribeInstances 拉可用 .metal 节点 IP 列表。
        POC 阶段:环境变量 FC_NODES=ip1,ip2 直接传入。
        """
        raw = os.environ.get("FC_NODES", "")
        return [n.strip() for n in raw.split(",") if n.strip()]

    # ------------------------------------------------------------------
    # node-agent HTTP 调用
    # ------------------------------------------------------------------

    def _agent(self, node: str, method: str, path: str, body: Any = None,
               timeout: int = 120) -> dict:
        # node 可以是 "ip" 或 "ip:port";后者已含 port,不再追加
        host = node if ":" in node else f"{node}:{NODE_AGENT_PORT}"
        url  = f"http://{host}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        req  = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body_txt = e.read().decode(errors="replace")
            raise RuntimeError(f"node-agent {method} {path} → {e.code}: {body_txt}") from e
