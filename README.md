# AWS Self-Hosted AI Agent Sandbox Platform

> Build your own Fly.io-style Firecracker microVM sandbox on AWS — lower cost, full control, data stays in your account.

**中文** · [English](README.en.md)

---

### 项目简介

在 AWS 上复刻 Fly.io Firecracker microVM 架构，以更低成本、更高可控性运行 Claude Code 及各类 AI Agent。

- **真实 microVM 隔离**：每个沙盒运行在独立的 Firecracker guest 内核，与裸机行为完全一致
- **裸 Firecracker 后端**：node-agent 直管 microVM（jailer/tap/snapshot），成本优先；快照落持久状态 EBS，**默认再上传 S3 作权威副本(可关,`SNAPSHOT_TO_S3`)**,跨机恢复可从 S3 下载或靠 EBS 卷幸存（见下方"快照落盘与跨机恢复"说明）
- **控制面 / 数据面分离**：控制面固定在 On-Demand Graviton system 节点；Firecracker 数据面独占带 taint 的 sandbox 节点，可使用裸金属实例，或支持嵌套虚拟化的 Intel x86 实例（当前默认 `i7i.8xlarge`）；[异构节点池真机 E2E 已通过](docs/控制面数据面分离-i7i真机测试报告-2026-08-11.md)
- **快照驱动成本控制**：空闲沙盒快照挂起释放内存，访问时 ~1.2s 恢复
- **Fly Machines 风格 API**：create/wait/suspend/resume/exec/locate，幂等键、乐观锁、capability 模型
- **凭据零进沙盒**：Bedrock 凭据仅在 LiteLLM Pod 的 IRSA 角色，沙盒永远看不到真实 key
- **平台可观测性**：低基数 Prometheus 指标、CloudWatch 集中 JSON 日志、OpenTelemetry/X-Ray 跨组件 tracing、5 类告警、8 面板 Dashboard、快照 SHA-256 校验；支持 AMP remote-write 与 AMG 自动配置

### 适用场景

| 场景 | 说明 |
|---|---|
| **Claude Code** | fork/exec 密集、文件监听重、嵌套进程 — microVM 保障与裸机一致的行为 |
| **OpenClaw / Hermes** | 会话式智能助理，需多租户隔离、按需扩缩 |
| **OpenAI Codex / 代码生成 Agent** | 任意代码执行，VM 级安全边界，防逃逸 |
| **长程 Agentic 任务** | 任务暂停恢复、工作流中断续跑、快照持久化 session 状态 |
| **SaaS 沙盒服务** | 向终端用户暴露隔离执行环境，多租户、按量计费 |
| **CI/CD 沙盒** | 隔离的构建/测试环境，npm install / docker build / 任意端口服务 |

### 控制台 Portal（Demo Dashboard）

一个轻量级的 E2B / Fly.io 风格控制台（[`portal/`](portal/)），用于快速演示与观测沙盒平台：全局总览
所有沙盒状态、节点水位、暖池水位与事件时间线，并可直接在 API Playground 里跑 create / suspend / resume /
exec / destroy，实时看到每次调用的响应与耗时。**纯本地运行**（`npm run dev` + `kubectl port-forward`），
详见 [portal/README.md](portal/README.md)。

| Dashboard 总览 | 沙盒详情 + 性能指标 |
|---|---|
| ![Portal Dashboard](docs/portal/portal-dashboard.png) | ![Sandbox Detail](docs/portal/portal-detail.png) |

> 上图为真机截图（EKS + c6g.metal）：汇总卡片、沙盒表格（含状态徽章）、节点水位、事件时间线；
> 详情页展示完整 record 与快照/恢复性能指标（如 diff 快照实际仅写 5.35 MB、恢复 408 ms）。

### 核心优势

#### 1. 裸机保真度（microVM 不是容器）

```
guest kernel: 6.18.28   ≠   node kernel: 6.1.172   ✅ 真独立内核
nproc: 3 (guest 配额)   ≠   宿主: 64              ✅ CPU 视图隔离
inotify 配额: 独立                                  ✅ 密集容器不会耗尽
root 可绑 80 端口、dnf 装包、嵌套 docker            ✅ 完整 root 无 seccomp 裁剪
```

#### 2. 成本控制：快照 = 成本杠杆

**最小配置月费（us-east-1，2 台 system + 1 台 c6g.metal sandbox）：**

| 资源 | 月单价 | 月费（730h） |
|---|---|---|
| c6g.metal（64vCPU/128GiB）**spot**（本平台目标模式）| ~$486/月（us-east-1a 2026-07 查询，约按需 29%）| **~$486** |
| c6g.metal 按需（对比基线）| ~$1,588/月 | ~$1,588 |
| system 节点（2 × m7g.large，On-Demand）| ~$59.50/月/台 | ~$119 |
| EKS 控制面 | ~$73/月 | ~$73 |
| DynamoDB（PAY_PER_REQUEST）| 按写入量 | <$1 |
| 持久状态 EBS（gp3 400GB / 4000 IOPS / 1000MB/s，每节点一块，存内存快照）| $32 容量 + $5 IOPS + $35 吞吐 | ~$72/节点 |
| **合计（按需）** | | **~$1,853/月** |
| **合计（1 年 EC2 Instance Savings Plan All Upfront，仅计算）**| | **~$1,199/月** |
| **合计（sandbox spot + system On-Demand，目标模式）** | | **~$751/月** |

> **spot 是本平台的核心成本模型**：c6g.metal spot 约为按需的 ~29%（实测 us-east-1 各 AZ $0.65–$0.74/hr，2026-07 查询），
> spot 被回收时靠快照疏散 + 跨机恢复保住内存状态（见下方 50 满载实测）。**spot 价格实时浮动**，以实际报价为准。
> 若不用 spot，按需可购 1 年期 Savings Plan 降约 42%。实际价格请以 [AWS Pricing Calculator](https://calculator.aws) 为准。
> 生产节点池应分开采购：system 节点始终使用 On-Demand；仅 sandbox 数据节点在中断恢复链路闭环后使用 Spot。

**沙盒节点实例选择与月度价格（us-east-1）：**

以下价格按 Linux/UNIX、730 小时/月计算。Savings Plan 使用 **1 年 EC2 Instance
Savings Plan、All Upfront**；“等效月价”是一次性预付总额除以 12，便于与按需月价横向比较。
价格不含 EBS、EKS 控制面和网络流量，查询于 2026-08-12，实际价格以
[AWS Pricing Calculator](https://calculator.aws) 为准。

**嵌套虚拟化（Intel x86，4xlarge）：**

| 实例 | vCPU | 内存 | 本地 NVMe | 按需月价 | 1 年 SP 等效月价 | All Upfront 总额 |
|---|---:|---:|---:|---:|---:|---:|
| `c8i.4xlarge` | 16 | 32 GiB | 无 | **$547** | **$338** | $4,055 |
| `m8i.4xlarge` | 16 | 64 GiB | 无 | **$618** | **$382** | $4,579 |
| `r8i.4xlarge` | 16 | 128 GiB | 无 | **$811** | **$501** | $6,011 |
| `i7i.4xlarge` | 16 | 128 GiB | 1 × 3.75 TB | **$1,102** | **$668** | $8,012 |

**嵌套虚拟化（Intel x86，8xlarge）：**

| 实例 | vCPU | 内存 | 本地 NVMe | 按需月价 | 1 年 SP 等效月价 | All Upfront 总额 |
|---|---:|---:|---:|---:|---:|---:|
| `c8i.8xlarge` | 32 | 64 GiB | 无 | **$1,095** | **$676** | $8,109 |
| `m8i.8xlarge` | 32 | 128 GiB | 无 | **$1,236** | **$763** | $9,159 |
| `r8i.8xlarge` | 32 | 256 GiB | 无 | **$1,623** | **$1,002** | $12,021 |
| `i7i.8xlarge` | 32 | 256 GiB | 2 × 3.75 TB | **$2,205** | **$1,335** | $16,024 |

上述虚拟化实例均支持 `nested_virtualization=enabled`。当前 EBS-first 状态存储架构
不依赖本地 NVMe，因此 `r8i.8xlarge` 是规格上最接近 `i7i.8xlarge` 的无本地盘选择。

**Bare Metal：**

| 实例 | 架构 | vCPU | 内存 | 本地 NVMe | 按需月价 | 1 年 SP 等效月价 | All Upfront 总额 |
|---|---|---:|---:|---:|---:|---:|---:|
| `c6g.metal` | ARM64 | 64 | 128 GiB | 无 | **$1,588** | **$934** | $11,208 |
| `c6gd.metal` | ARM64 | 64 | 128 GiB | 2 × 1.9 TB | **$1,794** | **$1,055** | $12,659 |
| `c7g.metal` | ARM64 | 64 | 128 GiB | 无 | **$1,694** | **$1,042** | $12,500 |
| `c7gd.metal` | ARM64 | 64 | 128 GiB | 2 × 1.9 TB | **$2,119** | **$1,247** | $14,959 |
| `m7g.metal` | ARM64 | 64 | 256 GiB | 无 | **$1,906** | **$1,177** | $14,123 |
| `r7g.metal` | ARM64 | 64 | 512 GiB | 无 | **$2,502** | **$1,545** | $18,536 |
| `c7i.metal-24xl` | x86_64 | 96 | 192 GiB | 无 | **$3,127** | **$1,931** | $23,170 |
| `m8i.metal-48xl` | x86_64 | 192 | 768 GiB | 无 | **$7,417** | **$4,579** | $54,953 |
| `i4i.metal` | x86_64 | 128 | 1,024 GiB | 8 × 3.75 TB | **$8,017** | **$4,855** | $58,263 |

> 上表列出 AWS 上可运行 Firecracker 的候选机型，不等于当前 Terraform 的实例白名单。
> 当前仓库已开放并真机验证的是 ARM64 `c6g.metal` 和 x86 `i7i.*`；使用
> `c8i` / `m8i` / `r8i` 或其他 Bare Metal 机型前，需要扩展 Terraform 校验并完成对应架构的 E2E。

**承载能力与摊算成本（单台 c6g.metal，128 GiB）：**

| 运行模式 | 每沙盒内存 | 可承载沙盒数 | 摊算成本（按需） |
|---|---|---|---|
| 24×7 活跃工作集 | 1.5 GiB | ~75 个 | **~$23/沙盒·月** |
| **快照空闲回收** | ~50 MB（空载驻留）| **400+ 个（模型推导）** | **~$4/沙盒·月（模型推导）** |
| Savings Plan + 快照回收 | — | 同上 | **~$2–3/沙盒·月** |

- `400+` 和对应摊算成本来自单 VM 空载足迹与并发假设推导，尚未完成单节点 400 台满载实测。
- **resume 延迟 1.2s 实测**，用户无感知，快照挂起对用户透明
- 单台机器即可支撑小规模 SaaS，多台横向扩展线性增长（节点间无共享状态）

> **超卖（vCPU/内存 Overcommit）可进一步摊薄成本：** Firecracker microVM 支持 vCPU 超售——空闲沙盒几乎不消耗 CPU，活跃沙盒又是突发型负载。实测空载每 VM 实际驻留仅 ~50 MB（远低于分配的 1.5 GiB），这意味着可以按"分配值"超配、以"实际驻留"来装箱。结合快照空闲回收，实际可承载的沙盒数远高于内存物理限制所推算的数字，每沙盒摊算成本可以进一步降低。具体超售比例取决于业务负载特征，建议通过压测确定。

> 与 Fly.io 的详细成本对比（1000 沙盒场景，含带宽与非计算费用分析）见 **[docs/cost-comparison.md](docs/cost-comparison.md)**。

#### 3. 暖池（Warm Pool）：create 永远秒级，冷启动对用户透明

冷建一个 microVM 要造 rootfs（CoW 复制）+ 建 tap 网络 + boot guest 内核，即便 Firecracker 已经很快，仍有可感知延迟。**暖池把这段延迟提前预付**：后台预先造好一批空白沙盒、suspend 成内存快照落持久状态 EBS，`create` 请求来时直接 resume 顶上——把冷启动藏在用户视线之外。

```
后台补充 loop ─► 预造 N 个空白 VM ─► suspend 快照落持久 EBS ─► 标 pool_state=warm
                                                              │
create 请求 ──► 原子 claim 一个 warm ──► resume(~0.13s) ──► 改成真实 id ──► 201
                    │ 池空/抢输                （复用快照原节点）
                    └──► 回退冷建（driver.create）
```

- **claim resume ≈ 0.13s**（真机实测，FC Full 快照 load + tap）；`create` 体感恒定秒级，冷启动对用户透明
- **加速收益取决于业务镜像的冷启动成本**：空白 min-rootfs 的冷建本身就快（~0.17s，CoW + FC 微秒级 boot），暖池收益有限；暖池的价值在**冷建昂贵**时才凸显——rootfs 大、boot 要装依赖/起服务、guest 应用初始化慢（预装 node_modules、预热运行时）的真实业务镜像，此时冷建几秒~几十秒 vs resume 恒定 ~0.13s，才是数量级差异。**上线前请用真实镜像压测确定收益。**
- **原子领取无竞态**：`claim` 走 DynamoDB 条件写抢占（`pool_state=warm → claimed`），并发 create 不会领到同一个实例，抢输方自动回退冷建
- **自动补水**：后台 loop（默认每 30s）按水位补足 `WARM_POOL_SIZE`（默认 5）；多副本控制面下**只有 leader 补池**（复用 P0 的 leader 门控，不重复造）
- **优雅降级**：池空时透明回退冷建，功能不受影响
- 可调：`WARM_POOL_SIZE` / `WARM_POOL_REFILL_S` / `WARM_CPU` / `WARM_MEM_MIB`（实现见 `sandbox-api/warm_pool.py`）

> 暖池依赖 suspend/resume 快照能力。`GET /capabilities` 的 `warm_pool` 字段反映是否启用。
> 暖池已于 2026-07-07 真机 e2e 验证通过（含 resume 落原节点、vsock exec、池空降级），过程修复 4 个真机 bug，详见 **[docs/暖池-真机测试报告-2026-07-07.md](docs/暖池-真机测试报告-2026-07-07.md)**。

#### 3.5 自动休眠 / 唤醒（auto-sleep / auto-wake）：没流量自己睡，来请求自动醒，对齐 fly.io

暖池解决"创建快"，自动休眠解决"没人用时别烧钱"。**沙盒空闲一段时间就自动打快照休眠释放 RAM；下次请求打到网关层，透明 resume 唤醒，用户无感**——这正是 fly.io Machines 的 `auto_stop` / `auto_start` 体验。

```
running ──(空闲超 idle 阈值, 后台扫描)──► 自动 sleep ──► slept  ← 释放 RAM,快照落持久 EBS
   ▲                                                       │
   └────(请求打到网关 /s/{id}/{port}/, 透明 resume ~0.13s)──┘   ← 首请求略慢,之后无感
```

- **opt-in，默认关**：复用 Fly 语义的 `services[].autostop` / `autostart` 字段。create 时声明 `{"port":80,"autostop":true,"autostart":true}` 才启用（或用 `meta.auto_sleep` / `meta.auto_wake`）。不声明的沙盒行为完全不变。
- **自动休眠(`slept`)与手动挂起(`suspended`)严格区分**：这是关键设计。手动 `POST /suspend` 标 `suspended`，网关**不会**自动唤醒它；只有空闲自动休眠的 `slept` 会被请求唤醒。状态一眼可辨（Portal 徽章:`slept`=靛蓝、`suspended`=灰）。
- **多信号空闲裁决**：网关 HTTP、`exec`、文件上传/下载刷新 `last_active_at`；WebSocket 建连、断连及安静连接心跳持续刷新，并以进程内活跃连接计数即时否决休眠。缺失或非法活动时间按保守策略处理（不休眠）；热路径内存节流（默认 15s 内不重复写 DynamoDB）避免写放大。
- **网关透明唤醒**：`/s/` 反代遇到 `slept` 沙盒 → 触发 resume 并等其回 running 再转发（首请求阻塞 ~秒级,与 fly 一致）；并发请求靠 lease 条件写互斥，只有一个真正 resume。
- **后台扫描**：leader 门控的周期 loop（复用 reconcile/暖池同一 leader 锁，多副本不重复触发）；拿 lease 后**二次校验仍空闲**，防"扫描判定→加锁"之间刚来请求被误睡。
- **复用现有并发保护**：自动休眠走与手动 suspend 同一套 `lease + prev_state 条件写 + 失败回滚`，快照失败回滚 `running` 绝不静默丢数据。
- 可调 env：`AUTO_SLEEP_ENABLED`（默认 1）/ `AUTO_SLEEP_IDLE_S`（默认 300s）/ `AUTO_SLEEP_SCAN_S`（默认 30s）/ `AUTO_WAKE_TIMEOUT_S`（默认 30s）/ `ACTIVITY_TOUCH_MIN_S`（默认 15s）。裁决实现见 `sandbox-api/idle_detection.py`，扫描调度见 `sandbox-api/autosleep.py`。

> 依赖 suspend/resume 快照能力（同暖池）。已于 2026-07-16 真机 e2e 验证通过（A0~A5，含自动/手动区分、网关透明唤醒 ~2.1s），详见 **[docs/自动休眠-真机测试报告-2026-07-16.md](docs/自动休眠-真机测试报告-2026-07-16.md)**；脚本 `scripts/autosleep_e2e.sh`。
>
> 2026-08-11 增加 WebSocket 多信号检测与 A6 用例；当时本地测试 50/50 和 AWS
> `i7i.8xlarge` Firecracker A0-A6 E2E 均通过。早先两台 `c6g.metal` 的 EC2
> impaired 尝试保留为基础设施故障记录。详见 **[docs/空闲检测-真机测试报告-2026-08-10.md](docs/空闲检测-真机测试报告-2026-08-10.md)**。

#### 4. API 开发者友好性

```bash
# 创建沙盒（幂等）
POST /sandboxes
{"image": "...", "cpu": 2, "mem_mib": 4096, "idempotency_key": "req-123"}

# 等待就绪
GET /sandboxes/{id}/wait?state=running&timeout=30

# 挂起（快照 + 释放内存）
POST /sandboxes/{id}/suspend   # → snapshot_type, restore_time（快照落持久 EBS）

# 恢复（同机 ~1.2s 读本地 EBS；跨机从 S3 拉,默认已上传）
POST /sandboxes/{id}/resume

# 执行命令
POST /sandboxes/{id}/exec
{"cmd": "npm test"}

# 端口暴露（vibe coding / web 预览）—— 任意端口,经反代访问沙盒内服务
ANY  /s/{id}/{port}/{path}   # → 反代进 guest;路径路由,支持多沙盒暴露同一端口;支持 WebSocket

# 文件上传 / 下载（base64 over exec,适合中小文件 ≤10MB）
PUT  /sandboxes/{id}/files?path=/root/app.py   {"content_b64": "..."}
GET  /sandboxes/{id}/files?path=/root/out.txt  # → {"content_b64": "..."}

# 自定义镜像 / rootfs 模板 —— create 时按 image 选不同根文件系统
POST /sandboxes  {"image": "web", ...}   # web 预设自带 demo 站点,开机自起 :80
GET  /admin/images                        # 可用镜像列表(供 Portal 下拉)
```

> **端口暴露**：`/s/{id}/{port}/` 用**路径**（非子域名）定位沙盒 → 天然支持多个沙盒暴露同一内部端口
> （两个沙盒都开 80 互不冲突）。链路 `NLB → ingress-nginx → 控制面反代 → node-agent → guest`，
> 先用 NLB 自带域名、零自定义 DNS。
> - **任意端口**（`ALLOW_ALL_PORTS`,默认开）：用户在 guest 内起在任何端口都能访问,无需预声明。
> - **WebSocket 透传**：Vite HMR / SSE / 交互式终端均可。
> - **交互式 Web Terminal**：Portal 详情页"打开终端"一键在 guest 内起 PTY-over-WebSocket 终端(xterm.js),无需重建 rootfs。
> - **文件上传/下载**：`PUT/GET /sandboxes/{id}/files?path=`(base64 over exec),Portal 详情页有拖拽上传/下载卡片。
> - **可选鉴权**（`EXPOSE_TOKEN`）：设置后访问需带 `?token=`。
>
> 启用见 [docs/deploy.md](docs/deploy.md) Step 6.5，设计见 [docs/端口暴露设计-firecracker.md](docs/端口暴露设计-firecracker.md)。

> **自定义镜像 / rootfs 模板**：`image` 字段选不同根文件系统模板(命名 rootfs 方案,非实时拉 OCI)。
> `build-rootfs-image.sh <name>` 预构建 `rootfs-{name}.tar.gz` → 节点造 `/opt/sbx/rootfs-{name}.ext4` →
> create `image={name}` CoW 之。内置 **`web`** 预设(自带站点 + 开机自起 :80,端口暴露打开即见页面)。
> 非默认 image 自动跳过暖池走冷建;未构建的模板回退默认 min(不报错)。构建见
> [docs/deploy.md](docs/deploy.md) Step 1.6,设计见 [docs/自定义rootfs设计.md](docs/自定义rootfs设计.md)。

#### 5. 安全性
- VM 级隔离：每沙盒独立 guest 内核，无共享宿主内核泄漏
- 凭据零进沙盒：Bedrock 凭据只在 LiteLLM IRSA
- Bearer token 认证，多 key 支持多租户
- 强制 IMDSv2（`http_tokens=required`）：阻断 SSRF 经 IMDSv1 窃取宿主机实例凭据

#### 6. 高可用编排（控制面自愈，非"手搓 POC"）

控制面无状态。路线 A 使用 `FirecrackerSandbox` CRD 保存生命周期期望态，
`firecracker-operator` 复用现有 FirecrackerDriver/node-agent 执行实际动作；
DynamoDB 保留为 REST/Portal 兼容投影、幂等索引、事件、活跃信号、节点心跳和租约。
详细设计与迁移边界见 [docs/CRD路线A-架构与迁移.md](docs/CRD路线A-架构与迁移.md)。

- **CRD reconcile（状态自愈）**：watch + 周期 resync 对账 CRD 期望态、DynamoDB 投影和 node-agent 实况；Operator 重启或事件丢失后仍会继续收敛。
- **并发防护（多副本不打架）**：每沙盒 DynamoDB 长租约持续续租；create 预写 node/tap 操作日志，node-agent 对重复 create 幂等；leader 只运行暖池与 autosleep 全局维护。
- **节点心跳注册表（弹性发现）**：node-agent 每 30s 上报 `free_mem/vm_count/last_seen`，控制面按 `last_seen` 超时自动剔除死节点、`_pick_node` 从注册表选节点——不再靠硬编码 `FC_NODES`。
- **快照落盘强一致**：suspend 先同步确认快照已落持久状态 EBS、再释放 VMM 内存；落盘失败则恢复 VM 运行而非静默丢数据。保证不变式 **状态标 `suspended` ⟺ 持久 EBS 确有快照**。

> 上述能力均已 **真机验证通过**（EKS + c6g.metal，含 leader 故障转移、reconcile 漂移检测、快照落盘强一致），详见实测数据表。

---

### 与主流方案对比

| 维度 | 本方案（AWS 自建） | E2B | Fly.io Machines | AWS AgentCore |
|---|---|---|---|---|
| **隔离层** | Firecracker microVM | Firecracker microVM | Firecracker microVM | 容器（共享内核）|
| **裸机保真度** | ✅ 最高 | ✅ 高 | ✅ 高 | ❌ 容器行为偏差 |
| **自定义镜像** | ✅ 命名 rootfs 模板(预构建) | ✅ | ✅ | ❌ 受限 |
| **任意端口暴露** | ✅ 路径路由 `/s/{id}/{port}` + 共享 NLB（支持 WebSocket）| ✅ | ✅ | ❌ |
| **交互式 Web 终端 / 文件传输** | ✅ Portal 内置(PTY-over-WS + base64 over exec) | ✅ | 部分 | ❌ |
| **24×7 长驻** | ✅ | ✅ | ✅ | ❌ 有 TTL |
| **快照 suspend/resume** | ✅ 实测 1.2s | ✅ | ✅ | ❌ |
| **凭据隔离** | ✅ LiteLLM IRSA（已落地）| ✅ | ✅ | N/A |
| **控制面自愈** | ✅ reconcile + leader + 心跳发现 | ✅ ~20s sync loop | ✅ 去中心化 flyd | ✅ 托管 |
| **可观测性** | ✅ Prometheus/Alertmanager/Grafana；可选 AMP + AMG | 托管 | 托管 | CloudWatch |
| **数据主权** | ✅ 数据留 AWS 账号内 | ❌ 第三方 | ❌ 第三方 | ✅ |
| **K8s 生态集成** | 每沙盒有 CRD/condition/RBAC；microVM 不建 Pod | ❌ | ❌ | ❌ |

---

### 为什么每个 sandbox 不是一个 Pod

本项目使用 Kubernetes 管理**平台服务和生命周期对象**，但不把每个用户 sandbox
运行成 Pod。Ingress、控制面、Operator、LiteLLM 和 node-agent 都是 Pod；每个沙盒有
一个轻量 `FirecrackerSandbox` CRD，真正执行用户代码的
Firecracker microVM 则是 sandbox 节点上的宿主机进程。可以把这个边界概括为：
**Kubernetes 保存期望状态，平台 Operator 调度和控制 microVM。**

创建 sandbox 时，请求先到 `sandbox-control-plane`，写入 CRD 和 DynamoDB 兼容投影。
Operator 根据 node-agent 心跳和剩余容量选择数据节点，再调用该节点上的 node-agent。node-agent
随后在宿主机上准备 rootfs、TAP 网络和 vsock，分配 vCPU/内存并启动 Firecracker。
kube-scheduler 只看见每个数据节点上的一个 node-agent DaemonSet Pod，不会为每个
sandbox 创建 Pod、PVC 或 Service。

| 维度 | 普通 Pod | 本项目的 Firecracker sandbox |
|---|---|---|
| Kubernetes 对象 | 每个实例都是 Pod，可由 Deployment/Job 等控制器创建 | 每个 sandbox 是轻量 CRD；没有对应 Pod |
| 放置决策 | kube-scheduler 根据 requests、affinity、taint 等选择节点 | 控制面根据 node-agent 心跳、可用内存和 VM 数量选择 sandbox 节点 |
| 隔离边界 | 容器依赖 namespace/cgroup，通常与节点共享宿主内核 | KVM 提供虚拟硬件边界，每个 microVM 启动独立 guest 内核 |
| 启停语义 | kubelet 拉取镜像并启动容器；异常后按 Pod 策略重建 | node-agent 调 Firecracker API 执行 create、snapshot、suspend、resume、destroy |
| 网络 | CNI 分配 Pod IP，Service/Ingress 路由到 Pod | 每个 guest 连接独立 TAP `/30` 网段，经 `Ingress → 控制面 → node-agent → guest` 代理访问 |
| 状态存储 | 常见做法是容器层加 PV/PVC，生命周期由 Kubernetes 协调 | 每个 sandbox 使用独立 rootfs 和内存快照，文件落在宿主机挂载的持久状态 EBS |
| 可观测性 | `kubectl`、Pod condition、probe 和容器日志直接可见 | `kubectl get fcsbx` 查看期望态/phase/condition；VM 指标由平台采集 |
| 规模单位 | 一份用户环境通常至少增加一个 Pod 及相关网络对象 | 每沙盒仅增加一个 CR；单个 node-agent 管理多台 microVM |

这样设计的主要目的不是绕开 Kubernetes，而是让长期存活、可挂起的用户环境拥有独立于
Pod 重建语义的生命周期。空闲 sandbox 可以写入 Firecracker 快照并释放 VMM 内存，恢复时
继续使用原有 guest 状态；节点内也能按真实工作集对 CPU 和内存做更细的装箱。与此同时，
Kubernetes 仍负责平台组件的副本、滚动发布、服务发现和故障接管。

这条路径也意味着平台要承担 kubelet 不会替 microVM 完成的能力：节点选择、Firecracker
状态机、网络代理、快照与恢复动作由 Operator/node-agent 实现。当前实现还有两个明确边界：

- Firecracker 是 node-agent 启动的子进程，并运行在 node-agent Pod 的 cgroup 中；因此
  “sandbox 不是 Pod”表示它不是独立的 Kubernetes 调度对象，并不表示它完全不受
  node-agent Pod 或宿主节点生命周期影响。node-agent DaemonSet 使用 `OnDelete`
  更新策略，避免镜像或配置变更自动滚动杀死 microVM；升级或重启某个 node-agent
  前仍需先安全疏散该节点，再手动删除对应 Pod。
- suspend 快照默认上传 S3，同时保留节点状态 EBS。Operator 可消费可恢复的
  `needs_reschedule`；但运行中节点突然消失时，历史快照可能不是最新状态，系统会标
  `orphaned` 而不会静默回滚。Spot 中断前的最新快照/状态 CAS/排除 draining 节点仍需闭环。

### 架构概览

```
┌─ EKS cluster ─────────────────────────────────────────────────────┐
│                                                                      │
│  system 节点组（On-Demand）      sandbox 数据节点组                 │
│  Graviton m7g（默认 2 台）       c6g.metal 或 i7i.*                 │
│  ┌──────────────────────────┐      ┌────────────────────────────┐  │
│  │ sandbox-control-plane    │      │ node-agent Pod (DaemonSet) │  │
│  │ REST / exec / files/proxy│      │  hostNetwork / privileged  │  │
│  ├──────────────────────────┤ HTTP │  心跳 / tap / 快照 / 代理  │  │
│  │ firecracker-operator     │─────►├────────────────────────────┤  │
│  │ CRD watch/reconcile      │◄─────│ 宿主机 Firecracker 进程     │  │
│  │ WarmPool / AutoSleep     │ 心跳 │  ├ microVM A（不是 Pod）    │  │
│  │ 无状态 → CRD + DynamoDB   │      │  ├ microVM B（不是 Pod）    │  │
│  └──────────────────────────┘      │  └ microVM N（不是 Pod）    │  │
│                                    │  └ microVM N（不是 Pod）    │  │
│                                    └────────────────────────────┘  │
│  CoreDNS / LiteLLM / Ingress         taint: dedicated=sandbox       │
│         ↑ ingress-nginx (NLB)        （普通 Pod 不进入数据节点）     │
│         api.sbx.<domain>  ←── 生产外部访问（POC 推荐 port-forward）  │
│                                                                      │
│  DynamoDB: sandboxes / events / tap-idx / nodes(心跳) / locks(leader)│
│  LiteLLM(Bedrock代理)                                                │
│  Prometheus / Alertmanager / Grafana ──SigV4 remote-write──► AMP    │
│                                                    AMG ──PrivateLink─┘│
│  Fluent Bit ──► CloudWatch Logs    OTLP ──► ADOT ──► X-Ray          │
└──────────────────────────────────────────────────────────────────────┘
```

---

### 可观测性

`terraform/stage2-control-plane/observability.tf` 与 `p2_observability.tf` 提供四档部署：

1. `enable_observability_stack=true`：集群内 Prometheus、Alertmanager、Grafana，
   自动抓取 control-plane 与 node-agent。
2. `enable_amp_remote_write=true`：创建 AMP workspace，通过 Prometheus IRSA + SigV4
   remote-write；要求第 1 档同时开启。
3. 传入已有 AMG workspace/VPC/subnet/SG：附加最小 AMP 查询权限，并创建
   `aps-workspaces` Interface Endpoint。Terraform 不创建 AMG workspace。
4. `enable_p2_observability=true`：部署 Fluent Bit/CloudWatch Logs、ADOT/X-Ray，并使用
   15 分钟临时 AMG token 幂等配置 `sandbox-amp` datasource 和 Dashboard，退出即清理凭据。

平台当前包含 5 类告警（唤醒时延、快照完整性、容量、孤儿增长、控制面退化）和
`Sandbox Platform` 8 面板 Dashboard。指标不使用 sandbox ID 标签；快照恢复前校验
SHA-256 manifest，损坏时拒绝恢复并触发指标/告警。

真实 AWS 环境还验证了固定 correlation ID 在 CloudWatch 同时命中 control-plane 与
node-agent、同一 trace 在 X-Ray 中包含跨组件父子 segment、AMG datasource
health=`OK` 且临时账号残留为 0。部署参数和验证命令见
[完整部署手册 Step 6.2](docs/deploy.md#step-62-部署可观测性p1推荐)，证据见
[P2 可观测性真机测试报告](docs/P2可观测性-真机测试报告-2026-08-12.md)。

---

### 快速开始（Agent 部署指南）

> 将以下提示词复制给 Claude Code / Cursor / 任意支持代码执行的 Agent，即可引导完整部署。
> 完整步骤手册见 **[docs/deploy.md](docs/deploy.md)**。

```
你是一名 AWS 基础设施部署工程师，负责在 AWS 上部署一套 AI Agent 沙盒平台。

任务：完整阅读并按顺序执行 docs/deploy.md 中的所有步骤（Step 0 ~ Step 9）。
遇到错误时先排查根因，修复后再继续，不要跳过任何步骤。

⚠️ 关键注意事项（执行前必读）：
1. 认证安全：Step 6 必须传入 api_keys 和 litellm_master_key（用 openssl rand -hex 32 生成），
   不能留空——控制面无 key 时所有受保护接口返回 503。
2. rootfs 必须含 vsock agent：Step 1.5 的 min-rootfs（exec 走 vsock 通道），phase3 apply 需显式传 rootfs_s3_uri。
3. system 节点固定为 On-Demand Graviton；`node_arch` 只描述 sandbox 数据节点：
   Graviton=`arm64` + `c6g.metal`，x86=`amd64` + `i7i.8xlarge`（可覆盖任意 `i7i.*`）。
4. 控制面镜像构建为 `linux/arm64`；node-agent 和 rootfs 必须与数据节点一致。
5. 测试完成后立即执行 docs/deploy.md 中的【清理】步骤。

开始前先确认：
- AWS CLI 已配置（需要 EKS / EC2 / IAM / DynamoDB / ECR / S3 权限）
- 已安装 kubectl, terraform (≥1.5), helm, git
- 对应实例的 EC2 vCPU 配额已申请（c6g.metal=64，i7i.8xlarge=32）

确认就绪后，请读取并执行 docs/deploy.md 中的所有步骤。
```

---

### 后期运维提示词

```
你是这套 AWS 沙盒平台的运维工程师。平台概况：
- EKS 集群 claude-sbx：独立 On-Demand Graviton system 节点组 + c6g.metal/i7i sandbox 数据节点组
- 控制面：sandbox-system namespace，Deployment 2 副本，固定在 system 节点
  外部访问：http://api.sbx.<domain>（ingress-nginx NLB）
- 状态存储：DynamoDB（claude-sbx-sandboxes / events / tap-idx / nodes / locks）
- 高可用编排：控制面 leader-only reconcile loop（对账自愈）+ node-agent 心跳注册表 + DynamoDB leader 锁
- 凭据隔离：LiteLLM（litellm namespace）持有 Bedrock IRSA，沙盒无凭据
- 快照：持久状态 EBS（base + Diff 增量内存快照），spot 疏散跨机恢复
- 可观测性：monitoring namespace 的 Prometheus/Alertmanager/Grafana；可选 AMP + AMG

常见运维操作：
1. 查看所有沙盒：curl http://api.sbx.<domain>/sandboxes?tenant_id=<id>
   或本地：kubectl port-forward -n sandbox-system svc/sandbox-control-plane 18000:80 &
2. 重启控制面：kubectl rollout restart deployment/sandbox-control-plane -n sandbox-system
3. 查看节点：kubectl get nodes -o wide
4. 查看 LiteLLM：kubectl logs -n litellm deployment/litellm --tail=50
5. DynamoDB 直查：aws dynamodb scan --table-name claude-sbx-sandboxes --select COUNT
6. 镜像更新：按 system/data 架构运行 `bash scripts/build_and_push.sh --control-plane-platform linux/arm64 --node-agent-platform <数据节点平台>`，然后滚动重启
7. 节点扩容：调整 phase3 的 `sandbox_node_count` 并 terraform apply
8. 成本优化：批量挂起空闲沙盒
   for id in $(curl -s http://api.sbx.<domain>/sandboxes?tenant_id=all | python3 -c "import sys,json; [print(s['id']) for s in json.load(sys.stdin)['sandboxes'] if s['state']=='running']"); do
     curl -s -X POST http://api.sbx.<domain>/sandboxes/$id/suspend
   done

9. 查看活节点心跳：aws dynamodb scan --table-name claude-sbx-nodes --query 'Items[].{node:node_id.S,free_mem:free_mem_mib.N,last_seen:last_seen.S}'
   （last_seen 超 90s 的节点会被控制面判死、自动剔除出调度池）
10. 查看当前 reconcile leader：aws dynamodb get-item --table-name claude-sbx-locks --key '{"lock_id":{"S":"reconciler"}}' --query 'Item.{owner:owner.S,rvn:rvn.N}'
    （rvn 持续自增 = leader 在正常续租；owner 变更 = 发生了故障转移）
11. 排查孤儿沙盒：aws dynamodb scan --table-name claude-sbx-sandboxes --filter-expression "#s = :o" --expression-attribute-names '{"#s":"state"}' --expression-attribute-values '{":o":{"S":"orphaned"}}'
    （state=orphaned 是 reconcile 检出的漂移记录，reconcile_reason 字段说明原因）
12. 查看监控组件：kubectl get pods -n monitoring
13. 本地访问 Grafana：kubectl -n monitoring port-forward svc/sandbox-monitoring-grafana 3000:80
14. 查看 AMP/AMG 输出：terraform -chdir=terraform/stage2-control-plane output amp_workspace_id && terraform -chdir=terraform/stage2-control-plane output managed_grafana_endpoint

监控关注点：
- Prometheus targets 与 remote-write：`up` 应全为 1，`prometheus_remote_storage_samples_failed_total` 应为 0
- node-agent 容量：`fcnode_free_memory_bytes`、`fcnode_scratch_bytes`
- 生命周期：`fc_operation_duration_seconds`、`fc_resume_stage_duration_seconds`
- 快照安全：`fc_snapshot_verify_total`、`fc_snapshot_errors_total`
- DynamoDB 写入延迟：AWS Console → DynamoDB → Metrics → SuccessfulRequestLatency
- LiteLLM 请求量：kubectl logs -n litellm deployment/litellm | grep "INFO:"
- reconcile 健康：`background_loop_runs_total`、`reconcile_actions_total` 和 locks 表 rvn
```

---

### 本地冒烟测试

```bash
# 无需 AWS，本地直接跑
python3 -m pip install -r requirements-dev.txt
python3 sandbox-api/smoke_test.py
python3 sandbox-api/crd_test.py
python3 node-agent/observability_test.py
# 期望：控制面 55/55 + CRD/operator 8/8 + node-agent 10/10 PASS
```

---

### 参与开发（Git Hooks，团队共享）

克隆仓库后**运行一次**，启用提交前的 AI code review + 文档自动同步：

```bash
./scripts/install-hooks.sh    # 设置 git config core.hooksPath .githooks
```

- hook 源文件在版本库的 `.githooks/`，**随 `git pull` 自动更新，无需重装**。
- git 出于安全不会自动改本地配置，故 `core.hooksPath` 需每位成员各自设一次（之后一直生效）。
- 临时跳过：`SKIP_CODE_REVIEW=1` / `SKIP_DOC_UPDATE=1 git commit`；全跳过：`git commit --no-verify`。
- 细节见 [.githooks/README.md](.githooks/README.md)。

---

### 实测关键数据

> i7i x86 的宿主 KVM、异构节点池调度、恢复后 exec、LiteLLM 与 leader 故障转移证据见
> [控制面与数据面分离 i7i 真机测试报告](docs/控制面数据面分离-i7i真机测试报告-2026-08-11.md)。

| 指标 | 实测值 | 环境 |
|---|---|---|
| microVM 启动延迟 | ~0.31s | c6g.metal，Firecracker v1.16 |
| 快照 resume 延迟 | **~0.13s（同机 Full 快照 load）** | 暖池默认落原节点走同机路径；跨机走持久 EBS 迁移（见下方 50 满载实测）|
| 空载驻留内存 | ~50 MB/VM | 512 MiB 分配 |
| 单机最大并发 | 60 VM（测试截止，未到上限）| c6g.metal 128 GiB |
| npm install 耗时 | 18s（JuiceFS）/ 4s（本地 ext4）| 7160 文件，8 依赖 |
| LiteLLM → Bedrock | ~1-2s | claude-haiku-4-5 |
| 冒烟测试通过率 | **控制面 55/55 + CRD/operator 8/8 + node-agent 10/10（ALL PASS）** | moto mock + tracing/可观测性/完整性测试 |
| 控制面 / 数据面分离 | **PASS** | `2 × m7g.large` system + `1 × i7i.8xlarge` sandbox |
| i7i x86 生命周期 | **ALL TESTS PASSED** | create/exec/suspend/resume/post-resume exec/destroy/auth |
| FC exec（vsock 通道） | rc=0，guest kernel 5.10.223 | c6g.metal，exec 在 microVM 内执行 |
| FC suspend→resume | 快照落持久 EBS（不传 S3）/ resume 亚秒 | 内存态跨快照精确保留（数据保真已验证）|
| 节点心跳注册 | 每 30s 写 nodes 表，`_pick_node` 从表选点 | 替换硬编码 FC_NODES |
| leader 故障转移 | 删 leader pod → 另一副本秒级接管（owner 转移）| DynamoDB 条件写租约 + rvn |
| reconcile 漂移检测 | running 但 runtime 消失 → 自动标 orphaned | 后台 20s 对账 |
| 快照落盘强一致 | suspend=suspended ⟺ 持久 EBS 确有快照 | 落盘确认后才释放内存 |

> P0 编排加固（reconcile / 心跳 / leader / 快照落盘强一致）已于 2026-07-07 真机验证通过，设计与借鉴来源见 [docs/编排层调研与改进路线.md](docs/编排层调研与改进路线.md)。

---

### 50 满载 sandbox 的 spot 疏散 + 跨机恢复实测（持久 EBS + Diff 增量快照）

> 核心场景验证：spot 节点收到回收通知 → 在 120s 窗口内把 50 个满载沙盒的**内存状态**快照到
> **持久 EBS**（Diff 增量，不传 S3）→ spot 死后卷幸存 → 迁移到另一节点批量恢复、内存精确续上。
> 环境：c6g.metal × us-east-1a（单 AZ），单块 gp3 1000MB/s 状态卷，每个沙盒灌到 ~1.2GB 常驻内存（模拟满载）。

| 环节 | 实测 | 说明 |
|---|---|---|
| **疏散（suspend，关键路径）** | 50/50 全 Diff，**墙钟 79.7s** | < 120s ITN 窗口，余量 ~40s；Diff 写盘 65.7GB（avg 1.3GB/个）|
| **状态卷迁移** | detach → attach 到新节点 ~数秒 | `DeleteOnTermination=false`，spot 强制终止后卷幸存（数据 md5 一致，已验证）|
| **跨机恢复（resume）** | 47–50 成功，**内存精确续上** | 恢复后 `FILL-*` 内存标记原样命中；P1 经 vsock 下发 ARP 刷新，`net_fix_ok` 100% |
| **resume 并发限流** | 并发 ~12–15 最优，50 个 ~33s | 单卷 EBS 带宽 ~15 并发饱和；`RESUME_CONCURRENCY`（默认 12）避免 merge I/O 打爆 |

**满载安全密度定论（单机单卷）**：**~50 个**（疏散 84.8s / 余量 35s + 内存 available 56G，双约束都舒服）；
60 个勉强（余量 15s）；70 个超窗（127s）。要更高密度需提高 EBS 带宽（多卷 / io2）或降低单沙盒内存占用。

> 关键正确性修复：resume 无条件把 Diff 合并到 base 再 load（干净页在 Diff 里是空洞，直接 load 会静默损坏内存）。
> 完整设计、实测数据与踩坑见 **[docs/firecracker-ebs-diff-design.md](docs/firecracker-ebs-diff-design.md)**。

#### 快照落盘与跨机恢复（现状说明，务必先读）

为避免与实现产生误解，明确当前边界：

- **快照默认上传 S3 作权威副本(可关)。** suspend 时先把内存快照(base+diff)落节点本地持久状态 EBS
  (`/var/lib/sbx/{id}/snap`,同机 resume 亚秒),随后**默认再上传整份 snap_dir 到 S3**(`s3://<bucket>/sbx/{id}/`),
  并把前缀回填到 `snapshot_s3`。开关 `SNAPSHOT_TO_S3`(默认 `1`;terraform 变量 `snapshot_to_s3`)设 `0` 则只保留在
  EBS(方案C,省一次上传的耗时/带宽)。需配置 `SNAPSHOT_S3_BUCKET` 才实际上传(未配桶自动退化为纯本地)。
- **跨机恢复有两条路径**:①(开启 S3 时,默认)节点死后从 S3 下载 snap_dir → 合并 diff → load;
  ②EBS 状态卷(`DeleteOnTermination=false`)幸存后 detach/attach 到新节点。开启 S3 上传后,即使节点连同状态卷一起丢失
  也能靠 ① 恢复。`op_resume` 的"本地无快照则从 `s3_prefix` 拉"分支现已被实际触发(不再是空转)。
- **spot 自动疏散仍是 opt-in / 半自动**:node-agent 的 spot 回收自动疏散默认 **DRY-RUN**(只记录计划,不打快照),
  需设 `RECLAIM_AUTO_EVACUATE=1` 才真打快照(落 EBS;若开 S3 则同时上传);而"节点死后自动 detach 卷 / 或自动从 S3
  批量拉起"这一编排闭环**尚未实现**。上文 50 满载"跨机恢复"实测中的卷 detach/attach 与批量 resume
  是**测试时手动/半自动编排**触发的能力验证,不是生产环境下的全自动流程。

> 一句话:**同机 suspend/resume 亚秒;快照默认上传 S3(可关),跨机恢复可从 S3 下载(不再只依赖 EBS 卷幸存);
> 但"自动侦测 spot 回收 → 自动疏散 → 自动拉起"的全自动编排闭环还没做完。**

---

*本项目是生产级参考实现，可作为在 AWS 上自建 Agent 沙盒平台的基础。*
