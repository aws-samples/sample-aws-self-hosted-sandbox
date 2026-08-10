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
AWS_REGION      = os.environ.get("AWS_REGION", "us-east-1")

_S3_CLIENT = None  # 懒初始化,复用


def _s3():
    global _S3_CLIENT
    if _S3_CLIENT is None:
        import boto3
        _S3_CLIENT = boto3.client("s3", region_name=AWS_REGION)
    return _S3_CLIENT

# 自定义镜像 / rootfs 模板:控制面把 image 字段归一化成模板名,node-agent 据此选
# /opt/sbx/rootfs-{name}.ext4。可用模板名由 SANDBOX_IMAGES(逗号分隔)声明,供 Portal 下拉;
# 节点未构建对应模板时 node-agent 会回退默认 min(不报错)。
SANDBOX_IMAGES = [
    s.strip() for s in os.environ.get("SANDBOX_IMAGES", "min,web").split(",") if s.strip()
]


import re as _re
_IMAGE_NAME_RE = _re.compile(r"^[a-zA-Z0-9_-]+$")


def normalize_image(image: str) -> str:
    """把用户传的 image 归一化成 rootfs 模板名。
    - 空/default/min → "min"
    - 命中已知模板名(SANDBOX_IMAGES,或形如 ".../web:tag" 取末段去 tag) → 该名
    - 含非法字符(路径注入等)或空 → "min"
    安全:结果会被 node-agent 拼进文件路径,严格限制 [A-Za-z0-9_-];node-agent 侧另有校验(纵深防御)。"""
    img = (image or "").strip()
    if not img or img in ("min", "default"):
        return "min"
    # 允许传 "web"、"web:latest"、"123.dkr.ecr.../sbx-web:tag" 之类,取末段去 tag
    last = img.rsplit("/", 1)[-1].split(":", 1)[0]
    if not last or not _IMAGE_NAME_RE.match(last):
        return "min"
    return last


def available_images() -> list[str]:
    """可用镜像模板名列表(供 Portal 创建表单下拉)。"""
    return SANDBOX_IMAGES
# 统一路径约定:所有节点把沙盒文件放同一前缀(跨机 resume 必须)
SBX_BASE        = "/var/lib/sbx"


def _s3_prefix(snap_id: str) -> str:
    """方案 A:每沙盒的 S3 快照前缀 s3://<bucket>/sbx/{id}/。未配 bucket 则空(退化为纯本地)。"""
    return f"s3://{S3_BUCKET}/sbx/{snap_id}/" if S3_BUCKET else ""


class FirecrackerDriver:

    def capabilities(self) -> Capabilities:
        return Capabilities(suspend_resume=True, warm_pool=True, migrate=True)

    # ------------------------------------------------------------------
    # create
    # ------------------------------------------------------------------

    def create(self, sandbox_id: str, spec: SandboxSpec, epoch: int = 1) -> dict:
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
            # 自定义镜像:image 归一化后的模板名 → node-agent 选 /opt/sbx/rootfs-{name}.ext4
            "rootfs_template": normalize_image(spec.image),
            "epoch":       epoch,   # 栅栏世代
        })
        # agent 回包含 guest_ip
        info = self._agent(node_id, "GET", f"/vm/{sandbox_id}")
        return {
            "node":      node_id,
            "tap_idx":   tap_idx,
            "guest_ip":  info.get("ip", ""),
            "epoch":     epoch,
        }

    def fence_vm(self, sandbox_id: str, node: str) -> bool:
        """围杀:在【指定节点】销毁某沙盒的陈旧 VM(分区愈合后老 VM 复活场景)。成功/无该 VM 返回 True。"""
        try:
            self._agent(node, "POST", "/vm/destroy", {"id": sandbox_id}, timeout=15)
            return True
        except Exception:
            return False

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

    def snapshot_base(self, sandbox_id: str, record: dict) -> dict:
        """
        方案C 预热:sandbox 运行期打一次 Full base 快照(不释放 RAM),供后续 Diff。
        create 成功后由控制面异步调用;off spot 关键路径。
        """
        node = record["node"]
        snap_local = f"{SBX_BASE}/{sandbox_id}/snap"
        # 方案 A:创建期打 base → 传 S3(内存 base)。timeout 放宽含 S3 上传。
        return self._agent(node, "POST", "/vm/snapshot_base", {
            "id": sandbox_id,
            "snapshot_local_path": snap_local,
            "s3_prefix": _s3_prefix(sandbox_id),
            "upload_s3": bool(S3_BUCKET),
        }, timeout=300)

    def suspend(self, sandbox_id: str, record: dict) -> dict:
        # 方案 A:快照(内存 base+diff)+ 磁盘(rootfs)传 S3,S3 为权威副本。
        # 有 base 时走 Diff(只写脏页,秒级);无 base 降级 Full。节点(本地 NVMe)丢失也能跨机恢复。
        node        = record["node"]
        snap_local  = f"{SBX_BASE}/{sandbox_id}/snap"

        # timeout 放宽:快照 create + S3 上传(内存 diff + 2GiB 磁盘全量)。给足 600s。
        resp = self._agent(node, "POST", "/vm/suspend", {
            "id":                   sandbox_id,
            "snapshot_local_path":  snap_local,
            "s3_prefix":            _s3_prefix(sandbox_id),
            "upload_s3":            bool(S3_BUCKET),
        }, timeout=600)
        return {
            "snapshot_type":          resp.get("snapshot_type", ""),
            "snapshot_size_bytes":    resp.get("mem_file_bytes", 0),
            "snapshot_actual_bytes":  resp.get("mem_actual_bytes", 0),
            "snapshot_create_time_s": resp.get("snapshot_create_time_s", 0),
            "s3_upload_time_s":       resp.get("s3_upload_time_s", 0),
            "snapshot_s3":            resp.get("snapshot_s3", ""),
        }

    # ------------------------------------------------------------------
    # resume  (从快照秒级恢复;可在不同节点)
    # ------------------------------------------------------------------

    def resume(self, sandbox_id: str, record: dict,
               snapshot_id: str | None = None,
               force_node: str | None = None,
               epoch: int | None = None) -> dict:
        # snapshot_id:快照来源沙盒的 id(暖池 claim 时 = warm_id;普通 resume 时
        # = sandbox_id 自身)。node-agent 用 sandbox_id 注册 VM,但从 snapshot_id
        # 的快照/rootfs 路径恢复 —— 暖池把 warm VM"改名"成 real_id 上线时,exec
        # 等后续操作按 real_id 路由,若仍用 warm_id 注册会 "not running"。
        snap_id    = snapshot_id or sandbox_id
        snap_local = f"{SBX_BASE}/{snap_id}/snap"
        rootfs     = f"{SBX_BASE}/{snap_id}/rootfs.ext4"
        # 方案 A:S3 前缀按快照来源 id 推导(与 suspend 上传路径一致),不依赖 record 里存的值。
        # 同节点 resume:本地 NVMe 有快照 → node-agent 跳过 S3 下载(亚秒)。
        # 跨节点 resume:本地无 → node-agent 从此前缀拉内存+磁盘。
        snap_s3    = _s3_prefix(snap_id)

        # 优先在快照所在的原节点 resume:该节点本地已有快照文件,resume 亚秒级;
        # 若换节点则需从 S3 下载整份内存镜像(实测跨节点 ~78s,远慢于冷建),
        # 完全背离暖池"秒级 create"的目的。仅当原节点已死/不可达时才跨节点兜底
        # (此时 S3 下载是恢复的必要代价)。
        # force_node:手动 migrate 时强制在指定的【其它】节点恢复(从 S3 拉),不复用原节点。
        node = force_node if force_node else self._resume_node(record.get("node", ""))

        resp = self._agent(node, "POST", "/vm/resume", {
            "id":                  sandbox_id,
            "snapshot_local_path": snap_local,
            "rootfs_path":         rootfs,        # 路径约定,跨机一致
            "tap_idx":             record["tap_idx"],
            # 方案 A:S3 权威副本。本地有快照则 node-agent 跳过下载(同机亚秒);
            # 本地无(跨机/节点被回收)则从 s3_prefix 拉内存+磁盘再 load。
            "s3_prefix":           snap_s3,
            # rootfs 缺失时的兜底模板(与 create 一致);正常路径 rootfs 已随卷在,不用它。
            "rootfs_template":     normalize_image(record.get("image", "")),
            # 栅栏世代:显式传入优先,否则用 record 里的(默认 1)。
            "epoch":               epoch if epoch is not None else int(record.get("epoch", 1)),
        }, timeout=180)
        info = self._agent(node, "GET", f"/vm/{sandbox_id}")
        return {
            "node":            node,
            "guest_ip":        info.get("ip", ""),
            "epoch":           epoch if epoch is not None else int(record.get("epoch", 1)),
            # 透传 node-agent 的恢复指标(P1 网络收敛结果 + 恢复/合并耗时),便于观测/验证。
            "restore_time_s":  resp.get("restore_time_s"),
            "merge_time_s":    resp.get("merge_time_s"),
            "net_fix_ok":      resp.get("net_fix_ok"),
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
            # 短超时:这是【存活探针】,不是数据操作。默认 120s 会让"探已死节点"每个挂 120s
            # (节点终止后 IP 不可达 → SYN 无响应直到超时),reconcile 一趟被拖到分钟级。5s 足够:
            # 活节点秒回,死节点快速失败 → unknown。
            info = self._agent(node, "GET", f"/vm/{sandbox_id}", timeout=5)
            return info.get("state", "unknown")
        except Exception:
            return "unknown"

    # ------------------------------------------------------------------
    # 节点选择:按 node-agent /health 的 free_mem_mib 挑水位最高的
    # ------------------------------------------------------------------

    def _resume_node(self, preferred: str) -> str:
        """
        resume 选点:优先复用快照原节点(本地有快照,亚秒 resume);原节点不存活
        才退到 _pick_node 跨节点兜底(从 S3 下载,慢但能恢复)。
        """
        if preferred:
            try:
                self._agent(preferred, "GET", "/health")
                return preferred
            except Exception:
                pass  # 原节点已死/不可达 → 跨节点兜底
        return self._pick_node()

    def _pick_node(self, exclude: str | None = None) -> str:
        # 优先用注册表里已上报的 free_mem_mib 排序,省去逐个 /health 往返;
        # 拿不到注册表(如本地测试用 FC_NODES)时回退到逐个探 /health。
        # exclude:跳过某个节点(手动 migrate 用 —— 强制换到另一台)。
        registry = self._active_nodes_from_registry()
        if registry:
            for node_id, _ in sorted(registry, key=lambda x: -x[1]):
                if exclude and node_id == exclude:
                    continue
                try:
                    self._agent(node_id, "GET", "/health")  # 存活兜底确认
                    return node_id
                except Exception:
                    continue
            raise RuntimeError("all registered nodes unreachable")

        nodes = self._list_metal_nodes()
        if not nodes:
            raise RuntimeError("no available .metal nodes")

        best_node, best_mem = None, -1
        for node_id in nodes:
            if exclude and node_id == exclude:
                continue
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

    def pick_other_node(self, current: str) -> str | None:
        """为手动 migrate 选一台【不是 current】的活节点;没有别的活节点则返回 None。"""
        try:
            return self._pick_node(exclude=current)
        except RuntimeError:
            return None

    def snapshot_complete_in_s3(self, sandbox_id: str) -> bool:
        """S3 是否有【完整可恢复】快照(diff + rootfs 都在)。
        用于对账判定:死节点上的沙盒能否跨机重调度恢复(只有 base 不足以恢复运行态)。
        未配 bucket 一律返回 False(退化为纯本地,不可跨机)。"""
        if not S3_BUCKET:
            return False
        prefix = f"sbx/{sandbox_id}/"
        for obj in ("diff.tar.gz", "rootfs.tar.gz"):
            try:
                _s3().head_object(Bucket=S3_BUCKET, Key=prefix + obj)
            except Exception:
                return False
        return True

    def _active_nodes_from_registry(self) -> list[tuple[str, int]]:
        """
        从 DynamoDB 心跳注册表拉活节点,返回 [(node_ident, free_mem_mib), ...]。
        node_ident 用 ip(node-agent 心跳里写的内网 IP),与 _agent 的 host 解析一致。
        表为空(未部署心跳/本地测试)返回 []，调用方回退 FC_NODES。
        """
        try:
            nodes = db.list_active_nodes()
        except Exception:
            return []
        out: list[tuple[str, int]] = []
        for n in nodes:
            ident = n.get("ip") or n.get("node_id")
            if ident:
                out.append((ident, int(n.get("free_mem_mib", 0))))
        return out

    def _list_metal_nodes(self) -> list[str]:
        """
        节点发现 fallback:环境变量 FC_NODES=ip1,ip2(本地测试/心跳表未就绪时用)。
        生产走 _active_nodes_from_registry() 的 DynamoDB 心跳注册表(P0-3)。
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
