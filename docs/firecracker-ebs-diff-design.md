# 方案 C + Diff 增量快照：完整落地设计

> 状态：**设计，待评审**（2026-07-06）。基于本仓库现有 FirecrackerDriver + node-agent 代码。
> 所有关键数据均已实测（数据与演进见下方"方案演进史与实测证据"）。

---

## 方案演进史与实测证据（为什么最终走方案 C+Diff）

> 本节自包含地记录我们**试过哪些路、各自因何被否/胜出**，附关键实测数据。
> 目的：让本文档独立说清决策依据，不依赖其他文档。

### 目标场景（不变的约束）
- 用 **spot 实例**跑 sandbox 省成本（比 on-demand 省 ~70%），但 spot 回收前只有 **~120s（ITN 保证）** 窗口。
- 一台 metal（64核/128G）可跑 **~50 个 sandbox**（内存约束）。
- 疏散要在 120s 内把 50 个的**内存状态**全部保住，并能在另一台机器**跨机恢复、内存精确续跑**。

### 走过的路线与结论

| 路线 | 核心做法 | 结论 | 关键证据 |
|---|---|---|---|
| **JuiceFS + overlay 根** | rootfs 走 overlay，upperdir 放 JuiceFS(→S3) 持久化系统写入 | ❌ **内核硬约束否决** | 实测 `overlayfs: upper fs does not support tmpfile`——FUSE 不能当 overlay upperdir |
| **方案 A：Full 快照 + S3** | 全量内存快照压缩后传 S3，跨机下载恢复 | ❌ **高密度时间不够** | 单块 gp3 顶 1000MB/s，50个×2GB=100GB 传输 **~105s**，余量仅 14s，任何抖动即超窗丢数据 |
| **方案 B：Diff 增量快照** | 只 dump 脏页，增量小 | ✅ **采纳(内存侧优化)** | Diff 改脏300MB时仅 **322MB/1.72s**(vs Full 2048MB/16s)；50个 Diff 写盘 ~16GB |
| **方案 C：持久 EBS + detach/attach** | 状态落持久 EBS，spot 死后卷幸存，新机 attach 恢复 | ✅ **采纳(传输侧优化)** | spot 强制终止后卷幸存+数据完好(md5一致)；attach ~4.6s；从 EBS resume 0.011s |

### 关键实测数据（本项目 c6gd.metal / c6g.metal，arm64，FC v1.16.1）

**1. 压缩（真实运行负载 vm.mem，2GB）**
- 内存真实压缩率 ~3.8x（非空闲态的乐观值）；zstd-3 -T0 多核压缩仅 0.77s（64核，几乎免费）。
- 流式压缩传 S3 省墙钟 ~1.85x。→ 但即便压缩，方案 A 高密度仍受 S3 带宽限制。

**2. Firecracker Diff 增量快照（ground-truth + 真实负载）**
- **前提**：`machine-config` 必须带 `track_dirty_pages: true`（否则 Diff 请求失败降级 Full）。
- Diff 只 dump 脏页、稀疏存储：空转 288K/0.012s；真实改脏 300MB 时 **322MB/1.72s**。
- **Diff 不能独立 resume**：diff.mem 只含脏页，须先有 base.mem，把脏页合并到 base 才能 load（已用 python 按块合并验证 load 成功）。
- Diff 仍是 FC 官方 **DevPreview**。

**3. 单块 1000MB/s gp3 并发写（模拟 N 个快照落盘）**
- 吞吐稳定顶 ~1000MB/s，并发多少都突破不了单卷上限。
- 50 个×2GB=100GB → **105.6s**（Full）；50 个 Diff ~16GB → **~15.6s**。
- 结论：Full+单卷不安全（余量14s）；**Diff 把写盘量砍一个量级是高密度安全的关键**。

**4. EBS detach/attach 全链路（gp3）**
- 快照写 EBS(gp3默认档,2GB)：7.99s；attach ~4.6s；mount 0.1s；从 EBS resume **0.011s**。
- detach 主动等 ~19.9s，但 **spot 强制终止时 AWS 自动 detach，不占关键路径**。

**5. spot 强制终止后卷幸存（决定性验证）**
- 真 spot 实例 + `DeleteOnTermination=false` + 强制 terminate → 卷状态 `available`、数据 md5 与终止前**完全一致**。
- → 用户思路"EBS 数据在，新机 attach 回来恢复"**技术成立**。必须设 `DeleteOnTermination=false`（用独立数据卷，不用根卷）。

**6. 实例类型**
- c6g.metal 与 c6gd.metal：**EBS 带宽同 2375MB/s、IOPS 同 80000、CPU/内存相同**，仅差本地 NVMe。
- 方案 C 状态落持久 EBS，本地 NVMe 用不上 → **换回 c6g.metal 省 ~13% 成本、零性能损失**。

### 最终选择：方案 C + Diff（B+C 组合）
- **Diff（B）** 砍内存写盘量：120s 内 50 个只写 ~16GB 而非 100GB。
- **持久 EBS（C）** 删掉最慢最不可控的 S3 传输：卷幸存 + 换机 attach。
- **方案 A（S3）仅作跨 AZ 兜底**（EBS 不能跨 AZ，跨 AZ 时退化到 S3，慢但可用）。

---

## 选型决策（2026-07-03，已定）

**选定方案 C + Diff 为主路线**，方案 A（S3）仅作跨 AZ 兜底。
- 理由：50 个内存快照传 S3 的累积时间不可控（单卷/带宽瓶颈），**很难稳定挤进 120s 窗口** →
  方案 A 在高密度下时间不够。
- 方案 C+Diff 把最慢的 S3 传输整段删掉（数据留在幸存 EBS），关键路径实测可压到 ~15-20s。
- 代价（已接受）：需常驻内存 base（2GB×N 存储开销，见 §6.1）+ 补 node-agent 代码 + AZ 绑定约束。

**实例类型决策**：换回 **c6g.metal**（不用 c6gd.metal）。方案 C 状态落持久 EBS，c6gd 的本地 NVMe
用不上；实测两者 EBS 带宽/IOPS/CPU/内存全同，仅差本地 NVMe → 换回省 ~13% 成本、零性能损失。

---

## 0. 一句话概括

用 **Diff 增量快照**把疏散写盘量从"全量内存"降到"增量脏页"，用 **持久 EBS + detach/attach**
替代"传 S3"，使一台 c6g.metal 上 **50 个 sandbox 能在 spot 120s 窗口内安全疏散并跨机恢复**。

---

## 1. 要解决的核心矛盾（背景）

- spot 便宜（比 on-demand 省 ~70%），但会被回收，回收前只有 **~120s（ITN 保证）** 窗口。
- 一台 c6gd.metal 可跑 ~50 个 sandbox（内存约束）。疏散要把每个的**内存状态**保住并跨机恢复。
- 现状 S3 路线的两个瓶颈：
  1. **传 S3 慢且不可控**：每个 sandbox 内存快照 512MB~2GB，50 个串行/并发传 S3 会累积到数百秒。
  2. **Full 快照写盘量大**：每个写全量内存（2GB），50 个 = 100GB。

## 2. 两个正交的优化（可独立、更可组合）

| 优化 | 解决的瓶颈 | 实测收益 |
|---|---|---|
| **Diff 增量快照** | Full 写全量内存 → 只写脏页 | 单个 322MB（改脏300MB时）vs Full 2048MB；50 个写盘 16GB vs 100GB |
| **EBS detach/attach** | 传 S3 慢/不可控 → 换机挂载 | 卷幸存已验证；关键路径去掉整段 S3 上传 |

**组合效果（实测数据推算）**：
- 50 个 Diff 写盘量 ~16GB ÷ 单块 1000MB/s gp3 = **~15.6s**（vs Full+单卷 105s，Full+S3 更久）。
- 疏散后卷 detach（AWS 随实例终止自动完成，不占关键路径）→ 新机 attach ~4.6s → resume。
- **关键路径 ~15-20s，spot 120s 窗口余量充足。**

---

## 3. 存储布局：一块"状态 EBS" + 只读基础 rootfs

> **实例类型：换回 c6g.metal（不用 c6gd.metal）。** 方案 C 下状态落持久 EBS，本地 NVMe
> （c6gd 的卖点）用不上（临时盘，spot 死即清空）。实测 c6g.metal 与 c6gd.metal **EBS 带宽同为
> 2375MB/s、IOPS 同为 80000、CPU/内存相同**，唯一差别是本地 NVMe → 换回 c6g.metal **省 ~13% 成本、
> 零性能损失**。切换方式：部署时 `-var="metal_instance_type=c6g.metal"`（已参数化，代码不用改）。

```
c6g.metal 节点
├── 根 EBS (nvme0n1, 200G)         系统盘 / /opt/sbx(基础 rootfs、内核、FC 二进制)
└── 状态 EBS (独立卷, gp3 1000MB/s) ★ 挂 /var/lib/sbx —— 所有 sandbox 的快照 + rootfs 改动
     /var/lib/sbx/
       {sid}/
         rootfs.ext4              该 sandbox 的 rootfs(CoW 自基础镜像;含装的软件)
         snap/
           vm.snapshot.base       首个 Full base 的状态文件
           vm.mem.base            首个 Full base 的内存(2GB)
           vm.snapshot            最新 Diff 的状态文件
           vm.mem                 最新 Diff 的内存(稀疏,仅脏页)
```

**关键点**：
- 状态 EBS 挂 `/var/lib/sbx`（node-agent 的 `SBX_BASE`，代码已是这个路径）。
- 该卷 attach 时设 **`DeleteOnTermination=false`** → spot 终止后卷幸存（已实测验证）。
- 基础 rootfs（只读、所有节点字节一致）留在**根盘/opt**，不进状态卷 → 消除快照-磁盘一致性风险。
- sandbox 对 rootfs 的改动（装软件）写在状态卷的 `{sid}/rootfs.ext4`，**跟着卷 detach/attach 走** →
  顺带解决"装软件到系统目录会丢"的老问题。

---

## 4. 生命周期时序

### 4.1 create（新增：创建后打 base）
```
控制面 create → node-agent op_create(CoW rootfs 到状态EBS, 起 FC, track_dirty_pages=true)
             → [新增] 后台异步打一次 Full base(16s, off 关键路径) → 存 {sid}/snap/*.base
```
> base 是 Diff 的前提。创建后立即在后台打，spot 来临时才打 Diff。

### 4.2 正常运行期（可选：周期性 Diff 刷新 base 新旧差）
- 可周期性打 Diff（把"脏了很久"的页固化），控制单次 Diff 不至于太大。POC 可跳过。

### 4.3 spot 疏散（关键路径，≤120s）
```
收到 spot ITN(EC2 metadata / NTH)
  → 控制面对本节点所有 sandbox 批量 POST /suspend {defer_upload:true}
     node-agent: PATCH /vm Paused → PUT /snapshot/create Diff(只写脏页到状态EBS) → kill VMM
     [关键路径只到这:50 个 Diff 写盘 ~16s]
  → 实例被 AWS 终止 → 状态EBS 因 DeleteOnTermination=false 自动 detach 幸存
```
> 注意：方案C 下 `defer_upload` 不再是"延后传S3"，而是"根本不传S3"——数据已在幸存的 EBS 上。

### 4.4 跨机恢复（不受 120s 限制）
```
控制面感知节点消失 → 在同 AZ 起新节点(Karpenter)
  → 把幸存的状态EBS attach 到新节点的 /var/lib/sbx (~4.6s)
  → 对每个 sandbox POST /resume
     node-agent: [新增] 若是 Diff → 合并 base.mem + diff 脏页 → 完整 mem → PUT /snapshot/load
     → 重建 tap → resume(~秒级)
```

---

## 5. 需要的代码改动（精确到函数）

### 5.1 node-agent/main.py

**(a) `_start_fc`：开启脏页跟踪（否则 Diff 永远失败）**
```python
# 现在:
_fc(sock, "PUT", "/machine-config", {"vcpu_count": cpu, "mem_size_mib": mem_mib})
# 改为:
_fc(sock, "PUT", "/machine-config",
    {"vcpu_count": cpu, "mem_size_mib": mem_mib, "track_dirty_pages": True})
```
> 实测：不加这个，任何 Diff 请求都失败 → 现有代码永远走 Full 降级分支（Diff 从未真正生效）。

**(b) 新增 `op_snapshot_base`：创建后打 Full base（off 关键路径）**
- 新接口 `POST /vm/snapshot_base {id}`：PATCH Paused → Full 快照存 `*.base` → Resumed（不 kill VMM）。
- 或复用现有 op_suspend 的"首次 Full"逻辑，但**不 kill、不改状态**（base 是运行中打的）。

**(c) `op_suspend`：Diff 分支已有，但依赖 base 存在**
- 现有逻辑：`has_base = os.path.exists(base_mem)` → 有 base 走 Diff，无 base 走 Full。
- 配合 (b) 后，疏散时 base 已在 → 走 Diff。**逻辑基本已就位，只差 (a) 的 track_dirty_pages。**

**(d) `op_resume`：实现 base + diff 合并（当前只 load 单个 vm.mem，Diff 无法恢复）**
```python
# 现在: 直接 load snap_dir/vm.mem
# 改为: 若存在 vm.mem.base 且 vm.mem 是 diff → 先合并
if os.path.exists(f"{snap_dir}/vm.mem.base"):
    merged = f"{snap_dir}/vm.mem.merged"
    # 用 base 副本 + diff 稀疏页覆盖(ground-truth 已验证 python 按块合并可行)
    # 生产优化: 用 FC 官方 snapshot-editor 或 mmap 稀疏合并
    _merge_diff_into_base(f"{snap_dir}/vm.mem.base", f"{snap_dir}/vm.mem", merged)
    mem_path = merged
else:
    mem_path = f"{snap_dir}/vm.mem"
_fc(sock, "PUT", "/snapshot/load", {"snapshot_path": ..., "mem_backend": {"backend_path": mem_path, ...}})
```

### 5.2 sandbox-api/drivers/firecracker.py
- `create()` 后触发一次 base 快照（调新接口 `/vm/snapshot_base`），异步不阻塞 create 返回。
- `_pick_node()` / `_list_metal_nodes()`：跨机恢复要选**同 AZ**的新节点（当前 FC_NODES 静态，
  生产需节点注册表带 AZ 信息，见 §7）。

### 5.3 terraform
- **状态 EBS 卷**：给 metal 节点组的启动模板加一块独立 gp3 数据卷（1000MB/s），
  `DeleteOnTermination=false`，userData 里挂到 `/var/lib/sbx`（xfs）。
- 或用独立管理的卷（不随节点组），由控制面在 create/疏散时 attach——**取决于粒度选择(§6)**。

---

## 6. 关键设计决策：粒度（整机一块盘 vs 每 sandbox 一块）

| 维度 | 整机共享一块状态盘（推荐 POC）| 每 sandbox 一块盘 |
|---|---|---|
| 疏散/恢复 | 整块盘迁移，**与 sandbox 数无关** | 每盘独立，可分散到不同新节点 |
| 卷数上限 | 1 块，无压力 | 受 Nitro ~28 卷/实例上限 → 最多 ~27 个 |
| 恢复灵活性 | 只能整机迁到一台新节点 | 可按 sandbox 打散 |
| 成本 | 1 块高吞吐 gp3 ~$56/月 | N 块盘容量+吞吐叠加，贵 |
| 复杂度 | 低 | 高（每 sandbox 卷生命周期）|

**推荐**：POC 走**整机共享一块状态盘 + 整机级疏散**（简单、契合"一台 spot 挂→起一台新机接整块盘"）。
高级需求（按 sandbox 打散恢复）再考虑每 sandbox 一块，但受卷数上限。

### 6.1 base 带来的存储开销（Diff 的固有代价，评审必问）

Diff 要能恢复，必须**常驻一份全量内存 base**（`vm.mem.base`，2GB/个）。这是"存储换时间"。

**每 sandbox 在状态 EBS 上的占用**：

| 项 | 大小 | 说明 |
|---|---|---|
| `vm.mem.base` | **2GB** | 全量内存 base（Diff 前提，必须常驻）|
| `vm.mem`（diff）| ~几百MB | 稀疏，只含脏页 |
| `rootfs.ext4` | ~227MB~几GB | 稀疏 ext4，随装的软件增长 |
| 元数据 | ~KB | 可忽略 |
| **合计** | **~2.5~3GB/个** | |

50 个 → **~125~150GB**。

**成本影响很小（关键：gp3 容量便宜，贵的是吞吐）**：
- 150GB 容量 = 150 × $0.08 = **~$12/月**。
- 单块 1000MB/s gp3 总成本 ~$56/月里，吞吐溢价$35 + IOPS$5 占大头，容量只占 ~$16。
- 相对节点成本（c6gd.metal ~$1500+/月）**可忽略**。
- 结论：**容量需求确实变多（主要是 base 2GB×N），但成本影响微乎其微。**

**若在意，可压 base 开销的手段**（POC 不做，列备选）：
1. base zstd 压缩存（2GB→~500MB，4x），恢复前解压。
2. 周期性重打 base + 丢弃旧 diff，控制单 sandbox 总量不无限增长。
3. 共享 base（同镜像 sandbox 内存相似）——收益不确定、复杂，不建议。

---

## 7. 硬约束与风险（诚实标注）

1. **AZ 绑定（最大约束）**：EBS 不能跨 AZ attach。整机疏散只能在**同 AZ**起新机接管。
   - spot 常因"某 AZ 某机型容量紧张"回收 → 该 AZ 可能起不了新同型 spot。
   - 缓解：多 AZ 各留 warm 容量 / 允许回退 on-demand / 跨 AZ 仍留 S3 兜底路径。

2. **base 的存储与可得性**：Diff resume 需 base.mem（2GB/个）。
   - 整机共享盘方案：base 就在状态卷上，detach/attach 天然跟着走 → **无额外传输**。✓
   - 若跨 AZ 用 S3 兜底：base 也要在 S3，成本 = 2GB × N。

3. **Diff 是 FC DevPreview**：`[DevPreview] diff snapshots`。生产用需评估稳定性、跟进 FC 版本。

4. **合并开销**：resume 前 base+diff 合并（2GB 级，python 按块 ~秒级）。生产用 snapshot-editor / mmap 优化。

5. **track_dirty_pages 运行时开销**：全程脏页跟踪有轻微内存/性能开销（可接受）。

6. **一致性**：疏散时 Diff 必须完整落盘 + sync 后才算安全；新机 attach 前确认卷已 available。

7. **单卷带宽仍是上限**：单块 gp3 顶 1000MB/s。若 Diff 也很大（sandbox 改脏内存多），
   50 个仍可能逼近窗口 → 多卷并行或降密度兜底。

---

## 8. 分步验证计划（不要一步到位）

1. **修 node-agent 两个 bug + 单个 Diff 端到端**：track_dirty_pages + resume 合并 →
   create → base → 改内存 → suspend(Diff) → resume → 验证内存状态续上。
2. **状态 EBS 挂 /var/lib/sbx + 单个 sandbox 走 EBS 全链路**：快照落 EBS → detach → attach → resume。
3. **spot 真实疏散**：真 spot 实例 + NTH，触发 ITN → 批量 Diff → 卷幸存 → 新机 attach → 批量 resume。
4. **50 个密度压测**：整机 ~50 个 → 疏散(批量 Diff) → 测关键路径总时间 → 卷迁移 → 批量 resume 成功率。
5. **跨机（跨节点同 AZ）**：2 台同 AZ metal，A 疏散 → B attach 状态卷 → resume。

---

## 9. 已验证的实测基线（引用，出处见 firecracker-snapshot-plan.md）

| 数据 | 值 | 出处小节 |
|---|---|---|
| Diff 快照创建（改脏300MB）| 1.72s / 占盘 322MB | Diff 快照真实负载实测 |
| Full 快照（2GB）| 16s | Firecracker Diff ground-truth |
| 单块 1000MB/s gp3 写 100GB | 105.6s | 方案C 带宽实测 |
| 50 个 Diff 写盘（~16GB）@1000MB/s | ~15.6s | Diff 真实负载实测 |
| EBS attach | ~4.6s | 方案C EBS 全链路 |
| 从 EBS resume | 0.011s | 方案C EBS 全链路 |
| spot 强制终止后卷幸存 + 数据完好 | ✅ | 方案C 卷幸存验证 |
| 快照写 EBS（gp3 默认档，2GB）| 7.99s | 方案C EBS 全链路 |

---

## 10. 与其他方案的关系

- **方案 A（Full + 流式 zstd 压缩传 S3）**：稳定、改动小，但受 S3 传输带宽限制，高密度慢。
  → 作为**跨 AZ 兜底路径**（方案 C 跨 AZ 走不通时）。
- **方案 B（Diff）**：本设计的一半（内存增量）。单独用 B + S3 也能省，但仍要传 S3。
- **方案 C（EBS）**：本设计的另一半（去掉 S3）。
- **本设计 = B + C 组合**：Diff 砍写盘量 + EBS 去 S3 传输，是三者里最契合 spot 疏散本质的。
- 三者不互斥：**同 AZ 走 C+B（最快）；跨 AZ 退化到 A（S3 兜底，慢但可用）。**


---

## 部署踩坑记录（2026-07-06 首次 50 并发实测）

1. **node-agent Pod memory limit 会 OOM 杀 microVM（严重）**
   - 现象:50 个 2GB sandbox 起来后,firecracker 进程被 `Memory cgroup out of memory` 批量杀,只剩个位数存活。
   - 根因:所有 Firecracker microVM 作为 node-agent 的**子进程**,跑在 **node-agent Pod 的 cgroup** 内。
     DaemonSet 原设 `limits.memory=4Gi` → 所有 guest 内存之和被卡在 4Gi → OOM。
   - 修复:node-agent DaemonSet **移除 memory limit**(main.tf 已改),让其可用到节点物理内存上限(128G)。
     生产应设为接近节点容量而非无限,并配合调度预留。

2. **auto-base 在 create 时高并发 → EBS 带宽饱和 → base Full 耗时暴涨 + guest 冻结**
   - 现象:50 个 base Full 同时打,单块 gp3(1000MB/s)写 100GB → 每个 base 从 ~16s 拖到 ~200s;
     期间 VM 处于 Paused,exec(SSH)全失败。
   - 修复:(a) app.py 用信号量 `BASE_SNAPSHOT_CONCURRENCY`(默认2)限制并发 base 数;
     (b) node-agent snapshot create `timeout` 提到 600s,避免超时抛异常跳过 `finally: Resumed` 把 VM 卡在 Paused。
   - 启示:base 是一次性预热成本,生产应在 sandbox 创建后**错峰/限流**打,不能 50 个同时打。

3. **节点必须同 AZ**:EBS 状态卷不能跨 AZ attach。phase3 节点组 `subnet_ids` 已钉死到单 AZ(1a)。


---

## 50 并发实测结果（2026-07-06，c6g.metal × us-east-1a，单块 gp3 1000MB/s）

> 完整链路:node1 起 50 个 2GB sandbox → 打 base → 改脏内存 → 批量 Diff 疏散 → 终止 node1
> (卷幸存) → 卷 attach 到 node2 → 批量跨机 resume。

### 各阶段实测

| 阶段 | 结果 | 耗时 |
|---|---|---|
| **创建 50 个** | 50/50 | 8.5s |
| **base 快照**(限流并发4) | 50/50 | 分批,~4min(off 关键路径) |
| **Diff 疏散**(并发10) | **50/50 全走 Diff** | **45.9s** ✅ (< 120s 窗口) |
| ├ 单个 Diff create | | min 2.9 / max 9.9 / avg 7.8s |
| └ Diff 写盘总量 | 37.5GB(平均 751MB/个) | |
| **卷幸存**(node1 终止后) | ✅ 数据完整(54 diffs+bases) | detach 受 metal 关机拖累(~800s,真实spot强制拔电则秒级) |
| **卷 attach 到 node2** | ✅ | **6s** |
| **mount** | ✅ | 0s |
| **跨机 resume**(并发10) | ⚠️ **11/50**(磁盘满) | 单个 snapshot/load avg 16s |

### 关键发现

1. **Diff 疏散达标**:50 个全走 Diff,45.9s 完成,稳稳进 120s 窗口。**核心目标验证成功。**

2. **卷幸存 + 跨机 attach 成功**:node1 强制终止后状态 EBS 幸存,attach 到 node2 仅 6s,187G 数据完整。

3. **⚠️ resume 的 merged 文件撑爆磁盘(必须修)**:
   - `op_resume` 的 `_merge_diff_into_base` 为每个 sandbox 生成一个完整 `vm.mem.merged`(~2GB)。
   - 状态 EBS 200G 已被 50×(base 2GB + diff) 占到 187G(94%),再写 merged → **11 个后磁盘满**(No space left)。
   - 根因:base + diff + merged = 每 sandbox 需 ~2 份全量内存的空间。
   - **修法(三选一)**:
     a) 状态 EBS 扩容到 400G+(50×(2G base+2G merged)+diff ≈ 250G,留余量)。**最简。**
     b) merged 用 reflink 与 base 共享未改块(已用 `cp --reflink=auto`,但 diff 写入仍占增量;
        且 base 本身是 dense 2GB,reflink 收益有限)。
     c) resume 后立即删 merged(load 完 FC 已读入内存,merged 可删)—— 但 load 期间仍需峰值空间。
   - 组合最优:**扩容 EBS(a) + resume 成功后删 merged(c)**,控制峰值。

4. **单个 snapshot/load 16s 偏高**:含 base+diff 合并(~2GB 顺序读写)+ 从 gp3 加载。50 个并发争 EBS 带宽拉高了单个耗时。

### 下一步
- 扩容状态 EBS(state_ebs_size_gb 200→400)+ resume 后删 merged,重测 50 个完整跨机 resume。
- 修 `op_resume`:load 成功后删除 `vm.mem.merged` 释放空间。


---

## 待验证:多代接力（resume 后再被回收）的 Diff 基准语义（2026-07-06 记录，暂缓）

**问题**:新节点也是 spot,也会被回收。sandbox 在 node2 resume 后,若 node2 又被回收,要再次 Diff 疏散。
这引出一个必须坐实的 Firecracker 语义问题:

- resume = `snapshot/load(merged) + resume_vm`。之后 VM 在新实例上跑,`track_dirty_pages` 重新跟踪。
- **再次 suspend 打 Diff 时,脏页是相对"load 时的内存镜像(merged)",不是相对老的 base.mem。**
- 因此下一轮 Diff 的合并基准应是 **merged**,而非原 base。若代码仍拿老 base 合并第二代 diff → 内存错乱。

**正确改法(待验证后实施)**:op_resume load 成功后,**把 merged 转正为新 base**:
```
load(merged) 成功
  → 删旧 diff (vm.mem)
  → mv merged → vm.mem.base   # merged 成为下一轮 Diff 的新基准
```
好处:(1) 多代接力语义正确;(2) 磁盘只留一份 2GB(新base),**顺带解决存储翻倍,不必扩到 400G**。

**待验证(用单 sandbox 做,快)**:裸 FC 多代接力实验 ——
`load(base) → 改内存 → diff1 → 合并成 merged1 → load(merged1) → 再改内存 → diff2 → 合并 → load` 
确认第二代 diff 以 merged1 为基准能正确恢复。**确认后再把 op_resume 的"删 diff"升级为"merged 转正 base"。**

> 当前阶段(50 并发疏散+跨机恢复)先用"扩容 EBS 400G + load 后删 diff"跑通;
> 多代接力优化(merged 转正)留待单 sandbox 实验确认语义后再做。

### P0 实施(2026-07-06):Firecracker 官方语义确认 + merged 转正 base

**权威依据(FC 官方 snapshot-support.md)**:
1. **"load 时重置脏页位图,标记所有页为 clean"** → resume(merged) 后打的 Diff **只含自本次 load 以来的脏页**,
   基准 = load 的完整内存镜像(merged)。→ **"merged 转正为 base" 语义正确,支持无限代接力。**
2. **"track_dirty_pages 不随快照保存,load 时必须显式再设 True"** → 否则 resume 后的实例**无法再打 Diff**
   (多代接力断链)。这是我们代码漏掉的第二个点。
3. Diff 必须**按创建顺序**依次合并到 base;官方推荐用 `snapshot-editor edit-memory rebase`(比手写合并可靠)。
4. **网络**:官方明确"resume 后网络连通性不保证,可能丢包,连接状态不保证存活"——印证前面 30s 收敛现象。

**已改 op_resume(node-agent/main.py)**:
- load 请求加 `track_dirty_pages: True`(修复多代接力断链)。
- load 成功后:删旧 diff → `os.replace(merged → base_mem)` 把 merged 原子转正为新 base。
  (os.replace 保留 inode,不影响 FC 已建立的 mmap;旧 base 数据块被释放。)
- **收益**:(a) 多代接力语义正确,新节点再被回收可继续 Diff;(b) 每 sandbox 只留一份 2GB(新base),
  **存储从 400G 需求回落到 ~200G**;(c) 无需 merged+base 两份并存。

**上个失败实验的教训**:1GB 小内存 microVM boot 后几乎所有页都脏 → Diff 近全量(1GB 非稀疏),
且 load 未设 track_dirty_pages → 两个对照组都得 ABC,无法区分基准。→ 改信官方文档语义,不再纠结该实验。
**P0 验证结果(2026-07-06,控制面真实代码路径,单 sandbox)**:✅ **多代接力通过**
- create→base→写A→suspend(**diff1** 29.6MB)→resume(merged转正base)→verify `marks=A` ✅
- →写B→suspend(**diff2** 11.6MB,基准=上次转正的 base)→resume→最终 `marks=AB` ✅ **PASS**
- 关键证据:
  1. 两代都走 Diff → 证明 `track_dirty_pages:True` 在 load 时生效,resume 后仍能打 Diff(接力没断)。
  2. diff2(11.6MB) < diff1(29.6MB) → 基准是上次 resume 的完整镜像,只记 B 之后的脏页(基准正确)。
  3. 最终 A+B 都在 → merged 转正 base 后内存完整传递,无丢失/错乱。
- **结论:P0(merged 转正 base)正确落地。新节点被回收可无限代接力;每 sandbox 只留 1 份 2GB base。**
- 小遗留:两次 resume 的 `merge_time_s=None`(未走稀疏合并分支,可能 suspend 后 diff 被判不够稀疏走了
  直接 load)。结果正确,但合并分支的触发条件待复核(不影响多代接力结论)。


---

## 50 并发完整链路实测：跨机 resume 成功（2026-07-06 修复后复测）

> 修复:(1) 状态 EBS 200G→400G(在线扩容 modify-volume + xfs_growfs);
> (2) op_resume load 成功后删 diff(vm.mem)回收空间(不删 base/merged)。

### 完整链路结果（50 个 2GB sandbox，单节点 c6g.metal，单块 gp3 1000MB/s）

| 阶段 | 结果 | 耗时 |
|---|---|---|
| Diff 疏散(并发10) | **50/50 全 Diff** | **45.9s** |
| 卷幸存 + attach 到 node2 | ✅ 数据完整 | attach 6s |
| **跨机 resume(并发10)** | **50/50 成功** ✅ | **75.3s** |
| └ 单个 snapshot/load | | min 5.8 / max 19.4 / avg 14.3s |

### 核心验证:内存状态跨机精确续上 ✅
- resume 后 node2 上 **50 个 firecracker 进程全部运行**。
- 抽查恢复的 guest:`vm-resume.log` 心跳计数从 **count=1085+ 继续**(不是从 1 重启)→
  **证明内存状态精确续跑,跨机内存恢复成功。**

### 网络"重建"问题澄清(2026-07-06 诊断后:不是 bug,是收敛延迟)
- 现象:50 个跨机 resume **完成后立即** ping/exec,只有 37/50 可达;**30s 后复测 50/50 全部可达**。
- 结论:**不是网络丢失 bug**,而是【resume 后 guest 网络收敛需要时间】——50 个并发 resume 时,
  后恢复的那批 guest 的网络栈(ARP 重学习/邻居表刷新)需数十秒稳定。宿主侧 tap 全部正确重建
  (50 taps + 50 FC 进程),`tap_idx` 一致保证 guest IP 与 tap 网关匹配。
- **内存续跑验证**:收敛后 exec 成功,`/tmp/mark.txt` = 疏散前写入的 `MARK-xxxx` 精确续上 ✅。
- **生产建议**:resume 后不要立即判定 sandbox 可用;应轮询健康(ping/exec 重试)直到网络收敛
  (实测 <30s),或在 guest agent 里 resume 后主动 `ip neigh flush` + 重发 gratuitous ARP 加速收敛。
- 这不阻塞方案C:内存状态本身跨机精确保留,网络是恢复后的短暂收敛期,重试即可。

### 结论:核心目标达成
**"50 个 sandbox 收到 spot 回收 → 快速 Diff 疏散(45.9s,< 120s 窗口)→ 状态 EBS 幸存迁移到新节点
→ 批量恢复(75.3s,内存精确续上)" 全链路跑通。** 方案 C+Diff 在 50 并发下验证成功。

### 关键数字汇总(供决策)
- 疏散关键路径:**45.9s / 50 个**(spot 120s 窗口内,余量充足)
- Diff 写盘量:37.5GB(平均 751MB/个,vs Full 100GB)
- 卷迁移:attach 6s + mount 0s(真实 spot 强制终止时卷自动 detach,不占关键路径)
- 跨机恢复:**75.3s / 50 个**(不在 spot 窗口内,可从容进行)
- 存储:每节点状态 EBS 400G(容纳 50×(base 2G + resume 期 merged 2G) + diff)


---

## P1(网络收敛加速)现状与待办(2026-07-06,暂缓等同事 vsock)

### 问题
50 个跨机 resume 后,~37/50 立即可达,**~30s 后全部可达**。空窗根因:内存快照固化了 guest 旧的
ARP 缓存(记着源节点网关 MAC),跨机后新宿主 tap 是不同 MAC → guest 要等 ARP 缓存自然超时才重学习。
FC 官方文档印证:"Guest network connectivity is not guaranteed to be preserved after resume"。

### 解法选项
- **A. guest 主动刷新**(治本):resume 后在 guest 内 `ip neigh flush all` + `arping -U`(gratuitous ARP)。
  困境:要下发命令但 SSH 此刻不通(鸡生蛋)→ **需要 vsock 这类不依赖 IP 的通道下发**。
- **B. 宿主侧刷新**(半治本):node-agent resume 后 `ip neigh flush dev <tap>` + 触发双向 ARP。只解决宿主→guest 方向。
- **C. 缩短 guest ARP 超时**(预防,改 rootfs):`sysctl net.ipv4.neigh.default.gc_stale_time=5`。一次性覆盖所有 sandbox。

### vsock 与本问题的关系(关键)
- **vsock 不走 IP、不用 ARP** → exec 控制通道 resume 后**立即可用**(不受 30s 影响)。
- 但 guest **业务网络**(访问外网)仍走 eth0/ARP,仍有 30s → **vsock 正好作为"立即可用的通道去下发方案A的刷新命令"**,解开 A 的鸡生蛋。
- 结论:**P1 正解 = vsock(控制通道) + 经 vsock 下发 guest ARP 刷新(业务网络)**。

### 同事 vsock 进展(origin/main,已合但我本地落后 2 commit)
- ✅ `scripts/vsock-exec-agent.py`:guest 端 vsock listener(port 2222,JSON 协议)已写好。
- ✅ node-agent `op_exec`:改为 vsock 优先 + SSH 兜底,含 `_vsock_exec` 握手。
- ⚠️ **gap**:`_start_fc` 未配 `PUT /vsock` 设备;rootfs 未装/启动 agent → **vsock 通道尚未端到端跑通**。
- 同事 e2e 报告结论:FC exec 当前不 work(三重缺失:node-agent 镜像缺 ssh/socat、rootfs 缺 sshd/vsock listener)。
- **同事正在继续修改 vsock。等他完成后合入,再基于 vsock 做 P1。**

### P1 待办(等同事 vsock 就绪后)
1. 合入同事最新 vsock 代码(注意与本地方案C+Diff 的 node-agent 改动合并:op_snapshot_base/track_dirty_pages/merged转正 vs vsock exec)。
2. `_start_fc` 加 `PUT /vsock`(host UDS=v.sock, guest_cid=3);rootfs 装 vsock-exec-agent + sbxinit 启动。
3. op_resume 成功后经 vsock 下发 `ip neigh flush all; arping -U -I eth0 <ip>` 加速 guest 网络收敛。
4. 兜底:rootfs 预设 `gc_stale_time=5`(方案C)。
5. 复测:50 并发跨机 resume 后"可达时间"从 ~30s 降到秒级。

### 本地代码基线提醒
- 本地 HEAD `dec729b` 落后 origin/main 2 commit(同事 vsock)。本地 node-agent 有方案C+Diff 改动(未提交)。
- **合并策略**:等同事 vsock 定稿 → 先 commit 本地方案C+Diff → 再 merge origin/main → 解 node-agent 冲突
  (两边都改 op_exec/op_resume 区域)。


---

## 满载疏散实测:密度 vs 120s 窗口(2026-07-06,单块 gp3 1000MB/s)

> 修正之前"空跑 sandbox"的乐观数据。本轮每个 sandbox 配 1.5G,**实际灌到 ~1.2GB 常驻内存并触碰所有页**
> (模拟满载 openclaw:一半可压文本 + 一半 urandom)。这才是接近生产的真实成本。
> 分批创建(每批10)+ base 限流,避免瞬间 OOM。

### 内存使用真相(关键澄清)
- Firecracker 用 `MAP_PRIVATE` mmap base.mem → guest 未用的页不占物理内存,只在页缓存(可回收)。
- **空跑**:70 个 FC 进程 RSS 仅 ~11GB,MemAvailable 117G(严重低估真实需求)。
- **满载**:每个真实占 ~1.2GB → 70 个 used **98.9GB**、available 仅 **27.5GB**;60 个 available 42G;50 个 available 56G。
- **结论:内存不能超卖。按【真实占用】而非配置值定密度。** 空跑测试会误导容量规划。

### 满载疏散实测(各 ~1.2GB,并发12,单块 gp3 1000MB/s)
| 密度 | Diff 写盘量 | 疏散墙钟 | 进120s? | 余量 | 内存 available |
|---|---|---|---|---|---|
| 70 | 91.9GB | **127.1s** | ❌ 超窗 | -7s | 27.5GB |
| 60 | 77.4GB | **105.0s** | ⚠️ 勉强 | 15s | 42GB |
| **50** | **65.0GB** | **84.8s** | ✅ 安全 | **35s** | 56GB |

- 数据线性:墙钟 ≈ 写盘量 ÷ 1000MB/s + ~20s 编排开销 → **瓶颈是单卷 EBS 带宽**。
- **满载下 Diff ≈ Full**(平均 ~1300MB/个):满载 openclaw 把大部分内存用脏,Diff 省时优势基本消失。
  → Diff 只在"脏页 << 总内存"(空闲/轻负载)时省;满载场景要靠**带宽**而非 Diff。

### 生产容量定论(单块 c6g.metal + 单块 1000MB/s gp3)
- **满载 openclaw 安全密度 = ~50 个**(疏散 84.8s 余量35s + 内存 available 56G,双约束都舒服)。
- 60 个"能进但不安全"(余量15s);70 个超窗(127s)。
- **要更高密度**:(a) 提高 EBS 带宽——多卷并行/io2 冲整机 2375MB/s,65GB÷2375≈27s,可上 70+;
  (b) openclaw 实际内存占用低则 Diff 变小,密度自然上去。
- 三档实测(空跑50/满载70/60/50)把满载真实边界摸清,作为容量规划依据。


---

## P1 网络收敛 + resume 正确性修复(2026-07-06/07)

### 跨机恢复实测(50 个满载,node1→node2 EBS 迁移)
- 迁移:node1 sync+umount 状态卷 → detach **35s**(已 umount 故快) → attach 到 node2 **5s** → mount(50 diffs+bases 完整)。
- 批量 resume:**50/50 成功**,墙钟 154s(满载 merged 1.2G/个 从 EBS 加载,并发争带宽);内存标记 `FILL-xxx` 精确续上、guest 网络 NET_OK。

### P1(经 vsock 加速网络收敛)
- 机制:resume 后经 **vsock**(不依赖 guest IP 网络) 下发 `ip neigh flush all` + `arping -U`(gratuitous ARP)。
- 验证:人为灌错误网关 MAC(动态 stale ARP) → 不修复内核 ~6s 自愈;经 vsock 下发 flush → **0.1s 即恢复**。
- 已实施:node-agent `op_resume` 成功后自动经 vsock 刷新;driver 透传 `net_fix_ok`。
  单 sandbox 多代实测 **net_fix_ok=True** 两代都成功。
- 注:常规跨机 resume 因 **tap_idx 保留**(网关 IP 不变),网络本就秒级收敛;P1 主要兜底极端 stale ARP + 加速。

### ⚠️ resume 内存合并正确性修复(重要)
- **原 bug**:op_resume 用"稀疏比例启发式"判断是否合并 diff——满载 diff 占盘接近全量(非稀疏)时
  **跳过合并、直接 load vm.mem(diff)**。但 Diff 的 mem 是稀疏文件,**自 base 以来的干净页是空洞(读为0)**,
  直接 load → 干净页变 0 → **内存静默损坏**(满载时因几乎全脏侥幸没暴露,但原理上错误)。
- **修复**:只要存在 base 就**无条件合并** base+diff→merged 再 load(不再用稀疏比例判断)。
  合并语义:base 副本 + diff 非空洞页覆盖 = 完整内存;对 Full 的 vm.mem 合并也安全(无空洞=全量覆盖)。
- 配合 P0:merged load 成功后原子转正为新 base。多代实测 marks=AB PASS,merge 耗时 0.03s。

### 合并代码状态(feat/ebs-diff-scheme-c)
- 已含:方案C EBS+Diff、P0 merged转正、P1 vsock网络刷新、always-merge正确性修复、
  driver net_fix透传、同事 vsock exec(合并保留)。
- 镜像已构建推送;真机验证:单sandbox多代 PASS、50满载疏散(84.8s)、跨机恢复 50/50。
