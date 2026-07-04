# Firecracker 内存快照方案：切换到 FirecrackerDriver，实现 sandbox 内存快照 + 跨机恢复

> **本方案（下文简称"Firecracker 快照方案"）** 目标：把控制面从 kata driver 切到
> firecracker driver，使 `/sandboxes/{id}/suspend` 真正打内存快照（传 S3）、
> `/resume` 在另一节点恢复（文档 §五 实测 1.2s）。
> 状态：**设计，待评审**。
>
> 术语说明：本文档早期用内部代号 "B2" 指代该方案，现统一改为"Firecracker 快照方案"。

---

## 0. 为什么 kata 不行、fc 行（一句话回顾）

kata-qemu 的 VMM 是 QEMU，快照接口 QMP 被 kata-runtime 独占（实测挂死）。
FirecrackerDriver 下 sandbox 是 **node-agent 自己用 jailer 启动的裸 Firecracker microVM**，
node-agent 握着它的 API socket，能直接调 Firecracker REST `PUT /snapshot/create`。

---

## 1. 现状勘察结论（基于代码，已确认）

| 组件 | 现状 | 本方案是否要改 |
|---|---|---|
| node-agent DaemonSet | ✅ 已配 hostNetwork/hostPID/privileged/`/dev`/`/var/lib/sbx`/FC_BIN/S3/IRSA | 基本不改 |
| node-agent nodeSelector | `sandbox=true` | 节点要打这个 label |
| 控制面 ConfigMap `SANDBOX_DRIVER` | 硬编码 `"kata"` | **改 `firecracker`** |
| 控制面缺 `FC_NODES` | 无 | **加**（FirecrackerDriver `_pick_node` 靠它找节点）|
| 控制面缺 `FC_KERNEL_PATH` | 无（driver 默认 `/opt/sbx/vmlinux`）| 可用默认 |
| phase3 系统节点 userData | ✅ **已装 firecracker + vmlinux + rootfs.ext4** | 复用 |
| phase3 系统节点 label | ❌ 没打 `sandbox=true` | **打上**，让 node-agent 调度上去 |

**最大发现**：phase3 的 c6g.metal 系统节点 `pre_bootstrap_user_data` 已经装好了
firecracker 二进制 + guest kernel + rootfs.ext4（为 FC 模式预留）。只是没打 `sandbox=true`，
所以 node-agent 没调度上去。

---

## 2. FirecrackerDriver 的一个硬约束：FC_NODES 是静态 IP

`firecracker.py` 的 `_list_metal_nodes()` 读环境变量 `FC_NODES=ip1,ip2`（静态）。
EKS 节点 IP 是动态的——这是代码标注的"POC 阶段硬编码"。

**本方案 POC 处理**：固定用一台 metal 节点跑 sandbox，部署后把它的内网 IP 填进控制面 `FC_NODES`
（控制面通过节点 IP:8002 访问 node-agent）。node-agent 用 hostNetwork，所以节点 IP:8002 直达。

> 生产化才需要做"节点注册表"（DynamoDB 动态维护节点 IP），POC 不做。

---

## 3. 改动清单（最小集）

### 3.1 terraform/phase3：让系统节点承载 sandbox
- 给 `metal_arm64` 节点组 labels 加 `sandbox = "true"`（现在只有 `role=system`）。
  → node-agent DaemonSet 会调度上去；FC 素材已就绪。
- （可选）确认 rootfs.ext4 构建那段 userData 真的成功（之前是 `|| non-fatal`，需验证产物存在）。

### 3.2 terraform/stage2：控制面切 FC
- ConfigMap `SANDBOX_DRIVER` → `firecracker`
- ConfigMap 加 `FC_NODES`（部署后填节点内网 IP，或用一个 var 传入）
- 控制面不再需要 kata 相关（RuntimeClass 等无害，留着）

### 3.3 节点 rootfs 准备（关键风险点）
node-agent `op_create` 从 `FC_ROOTFS`（默认 `/opt/sbx/rootfs.ext4`）CoW 复制。
phase3 userData 试图从 S3 下载 `rootfs.tar.gz` 造 rootfs——**但那个 S3 对象可能不存在**
（之前的 userData 是 `|| non-fatal`）。需要确保节点上有一个可启动的 rootfs.ext4：
- 选项 a：用 `scripts/setup-host.sh` 的逻辑在节点上现造（docker export amazonlinux → ext4）
- 选项 b：预先造好传 S3，userData 下载

这是本方案能否成功的**主要不确定点**——sandbox 要能真正 boot 起来，rootfs 必须正确。

---

## 4. 部署顺序（在干净环境上）

1. stage1（DynamoDB）— 不变
2. phase3（EKS）— **改：节点组加 sandbox=true label**
3. RuntimeClass / ingress-nginx — 可保留（FC 模式用不到 kata，但无害）
4. 镜像构建推送 — 不变（node-agent 镜像同一个）
5. stage2 — **改：SANDBOX_DRIVER=firecracker + FC_NODES**
6. **新增：确保节点有可启动 rootfs.ext4**
7. 验证：create → wait → exec → **suspend（真快照→S3）→ resume（恢复）→ exec 验证状态续上**

---

## 5. 验证目标（本方案成功的标志）
```
POST /sandboxes               → 201, node-agent 起 FC microVM
POST /sandboxes/{id}/exec     → 在 microVM 内跑命令(写一个内存标记)
POST /sandboxes/{id}/suspend  → 200, snapshot_s3=s3://.../sbx/{id}/, 内存释放
   (S3 里能看到 vm.mem + vm.snapshot)
POST /sandboxes/{id}/resume   → 200, restore_time ~1s, 在(可能不同的)节点恢复
POST /sandboxes/{id}/exec     → 内存标记还在 → 证明内存级续跑 ✅
```

---

## 6. 风险与诚实标注
1. **rootfs 可启动性**：最大风险。FC microVM 要能 boot，rootfs + kernel + init 必须正确匹配。
   文档 §五 实测过（c6g.metal + FC v1.16），但那是手工准备的环境。
2. **FC_NODES 静态 IP**：POC 固定一台节点；节点重建后 IP 变要手动更新。
3. **跨机 resume 的路径一致性**：Firecracker 快照硬编码 rootfs 绝对路径，
   node-agent 已用 `/var/lib/sbx/<id>/` 统一约定处理（§四点七 实测坑）。
4. **单节点演示**：POC 用一台 metal，"跨机"恢复实际是同机不同进程（除非加第二台 metal）。
   真正跨机要 2 台 metal + FC_NODES 填两个 IP。

---

## 7. 已敲定的执行决策（2026-06-30）

- **2 台 metal**：演示真正的跨节点迁移（A 节点 suspend → B 节点 resume）。
- **rootfs**：自己造最小 arm64 rootfs，传到用户自己的 S3 桶，userData 从该桶拉。
- **分两个里程碑**：
  - **里程碑 A（先做）**：验证内存快照 + 跨机恢复本身。最小 rootfs（init 配网 + 内存标记），
    不依赖 SSH exec。跑通 create → suspend(→S3) → 另一节点 resume → 确认内存状态续上。
  - **里程碑 B（后做）**：加 dropbear sshd + boot_args 注入 IP，验证 /exec 跑 python。

## 8. 里程碑 A 涉及的代码改动（精确清单）

| # | 改动 | 文件 |
|---|---|---|
| 1 | 节点组 desired_size 1→2；labels 加 sandbox=true | terraform/phase3/main.tf |
| 2 | rootfs S3 路径改成本账号桶 | terraform/phase3/main.tf |
| 3 | 控制面 SANDBOX_DRIVER=kata→firecracker；加 FC_NODES var | terraform/stage2-control-plane/main.tf |
| 4 | 造最小 rootfs（busybox+init+内存标记），传 S3 | scripts/ 新建 |

> 里程碑 B 才需要改 node-agent/main.py 的 boot_args 注入 IP（#5），里程碑 A 不碰 node-agent 代码。

## 9. 里程碑 A 内存状态验证方法
guest init 启动后往一个固定地址（如 tmpfs 文件 + 进程持续递增的计数器）写状态；
suspend 前记录值，resume 后读出——若续上（而非从头），证明内存级恢复成功。
node-agent 无 exec 时，通过 guest 串口日志（vm.log）或一个会把计数写到可观测位置的方式确认。

