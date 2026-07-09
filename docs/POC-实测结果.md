# POC 实测结果 —— H1 / H3 / H4 + 快照 + 文件系统

> 📌 **历史存档**:本文是 POC 各阶段的实测记录(某时间点快照),含 Kata 编排(H3)等**已从项目移除**的方案。
> Kata driver 因无法快照/恢复已删除,当前项目为裸 Firecracker 单一后端(见 README.md)。此文仅作历史参考,不再回改。

> 实测日期:2026-06-11 ~ 06-12 · 区域:us-east-1 · 验证方式:全程经 AWS SSM
> 机型:H1/H4/快照初轮用 `c7g.metal`,H3/JuiceFS/跨机轮用 `c6g.metal`(两者同为 Graviton 64vCPU/128GiB,结论通用)
> 基础设施由 `terraform/phase1`(单机)与 `terraform/phase3`(EKS)创建
>
> **相关专题文档**:文件系统对比见 `文件系统方案对比.md`;快照存储架构见 `快照存储架构.md`。
> 本文件汇总各轮实测的核心数字与结论。

## 一、H1 —— Claude Code 在 Firecracker microVM 内原生跑通(✅ 通过)

在 Graviton `.metal` 上用真实负载端到端验证,**非推断**:

| 验证点 | 实测值 | 结论 |
|---|---|---|
| microVM 启动 | Firecracker + KVM 引导 guest 内核 5.10.223,挂载 ext4 rootfs,执行 init,干净退出 | ✅ 真 microVM 跑起来了 |
| CPU 视图 | `nproc=2` | ✅ guest 只见自己的 2 vCPU,**非宿主 64 核**(裸机保真度;容器常泄漏宿主值) |
| 内存视图 | `mem_MB=3936` | ✅ guest 只见自己的 ~4 GiB |
| inotify 配额 | `inotify_watches=32635` | ✅ **独立** inotify 配额,不与宿主/其他沙盒共享(密集容器的痛点) |
| guest 内核 | `kernel=5.10.223` | ✅ 自己的内核,非共享宿主内核 |
| Claude Code | `2.1.173 (Claude Code)`,`which=/usr/local/bin/claude` | ✅ 沙盒内就绪 |
| **Bedrock 推理** | `claude -p "..."` → **`SANDBOX_OK`** | ✅ **整链路打通:microVM + Graviton .metal + Bedrock + Claude Code** |

**鉴权方式(实测可行):** 宿主挂 IAM 角色 → 经 IMDSv2 取临时凭据 → 注入 guest 环境变量 → Claude Code 走 `CLAUDE_CODE_USE_BEDROCK=1` + `us.anthropic.claude-opus-4-8` inference profile。
证明"**凭据不进镜像、由宿主侧提供**"的生产形态可行(呼应客户互不可信租户的凭据隔离要求)。

> 结论:客户离开普通容器、选 Fly 的根本顾虑——"Claude Code 在容器里行为和裸机不一样"——在 AWS Firecracker microVM 上**不存在**。CPU/内存/inotify/内核视图全部呈现裸机语义。

## 二、H4 —— 密度 / 启动延迟 / 装箱(✅ 初步数据)

单台 `c7g.metal` 实测(空载常驻沙盒):

| 批次 | 数量 | 每 VM 分配 | 启动延迟 | 每 VM 实际驻留 | 全部成功 |
|---|---|---|---|---|---|
| 1 | 20 | 512 MiB | ~0.31 s | ~41 MB | 20/20 ✅ |
| 2 | 60 | 1024 MiB | ~0.31 s | **~52 MB** | 60/60 ✅ |

关键发现:
- **启动延迟稳定 ~0.31 s**(含等 init 标记开销;纯 Firecracker 启动更快,标称 ~125ms)。
- **空载每 VM 实际驻留仅 ~50 MB**,远低于分配值(512/1024 MiB)→ **内存可大幅超售**,装箱按"实际驻留 + 工作集"而非"分配值"。
- 60 个 VM 时宿主负载仅 0.29 / 128 GB 还剩 120 GB → 远未到上限,瓶颈在内存工作集与 vCPU 争用,不在 VM 数量本身。

## 二点五、快照 / 恢复(✅ 通过)—— Fly suspend/resume 同款机制,成本核心杠杆

裸 Firecracker API 模式实测(模拟 Fly 的 suspend/resume:运行→暂停→Full 快照→销毁释放 RAM→从快照恢复):

| 指标 | 实测值 | 解读 |
|---|---|---|
| 恢复延迟 | **~7 ms(0.0069s)** | ✅ **亚秒级,用户无感**——这就是为何海量"24×7 可达"沙盒可不常驻内存 |
| 销毁后 RAM 回收 | 宿主 used 回落到 786 MB | ✅ 原 VM 的 4 GiB **完全释放**(空闲回收成功) |
| 状态保留 | HEARTBEAT 从 3→4 **无缝续跑**,内存里的 session 状态(`MAGIC`)原样还在 | ✅ 冻结-解冻语义,不是重启;跑到一半的 agent 会话精确续上 |
| Full 快照创建 | 33 s | ⚠️ 偏慢:Full 快照把 4 GiB 内存全量写 **EBS gp3**;可优化(见下) |
| 内存文件 / 状态文件 | 4.0 GB / 11 KB | 内存页全量 + 极小设备状态 |

**33s 创建慢是可优化项,非缺陷:**
1. **diff 快照**(只存脏页)替代 Full → 小得多、快得多;
2. **写本地 NVMe**(`i` 系列 .metal)而非 EBS gp3 → 数量级提升(本测试用 gp3 写 4GB 自然慢);
3. 真实空载沙盒每 VM 仅 ~50MB 驻留,脏页少,快照远比 4GB Full 快。

**成本含义(关键):** 恢复 ~7ms + RAM 可完全回收 → 可把"24×7 可达"与"24×7 常驻"解耦——大量空闲沙盒快照挂起、释放内存,访问时瞬时恢复。这是把 ~10000 沙盒成本从"按峰值常驻"降到"按实际活跃"的核心杠杆,与文档第 5 节判断一致,现已实测验证。

> 注:恢复后需重建 guest 网络/重同步时钟、丢弃旧 vsock 连接、禁止原始+克隆同跑(Firecracker 已知 caveat),生产编排需处理。

## 三、基于实测的装箱与成本测算

> 注意:上面 ~50MB 是**空载**驻留。真实 Claude Code 沙盒跑 build/test 时工作集会涨到几百 MB~GB 级,**按工作集峰值而非空载值做容量规划**。下面给两档估算。

**单台 `c7g.metal`(128 GiB,~$2.32/hr 按需):**

| 场景 | 每沙盒规划内存 | 单机可承载 | 每沙盒 $/月(按需) |
|---|---|---|---|
| 轻量/大量空闲(快照回收 idle) | 256 MiB 有效 | ~400+ | ~$4.2 |
| 活跃 build/test 工作集 | 1.5 GiB 有效 | ~75 | ~$22 |

测算口径:单机月费 ≈ $2.32×730 ≈ $1694;承载数 ≈ 128GiB×0.85 装箱效率 / 每沙盒有效内存;Graviton 按需价,未含 Savings Plan/Spot 折扣(生产可再降 40–60%)。

**~10000 并发的粗估(活跃工作集 1.5 GiB 档):**
- 需 ~10000/75 ≈ **134 台 c7g.metal**,按需 ~$22.7万/月;
- 叠加 **Savings Plan(~-50%)+ 快照回收空闲沙盒(大量沙盒并非时刻活跃,可再砍一大块)**,实际可显著下降。
- 这正是文档第 5 节"快照回收 idle 沙盒"为何是核心成本杠杆——把"24×7 可达"与"24×7 常驻"解耦。

> ⚠️ 这些是单机外推的**粗估**,真实数字需:① 用真实 Claude Code 负载测工作集峰值;② 验证快照恢复延迟与回收比例(Phase 2/5)。

## 四、产物与复现

- 基础设施:`terraform/phase1/`(`terraform apply` 重建)
- 主机准备:`scripts/setup-host.sh`(装 Firecracker / 取内核 / 构建 rootfs / 配 TAP,已验证)
- 主机内验证/压测脚本:`/opt/sbx-setup.sh`、`/opt/run-vm-test2.sh`、`/opt/stress-big.sh`(在实例上)

## 四点六、H2 文件系统(JuiceFS)—— 已实测(详见 `文件系统方案对比.md`)

在 microVM guest 内挂调优 JuiceFS(writeback+cache+buffer),对比本地 ext4:

| 指标 | 调优 JuiceFS | 本地 ext4 | 差距 |
|---|---|---|---|
| **真实 npm install**(8依赖/7160文件) | ✅ **成功 18s** | ✅ 成功 4s | ~4.5× |
| 纯小文件写 500×4k | 729 files/s | 24358 files/s | 33×(最差画像) |
| inotify 基础触发 | ✅ 2 events | ✅ 2 events | 持平 |

**两个关键发现:**
1. **Firecracker CI 内核无 FUSE(坐实 R3)**:`# CONFIG_FUSE_FS is not set` → JuiceFS 挂不上。
   自编带 FUSE 的 arm64 内核(`scripts/build-fuse-kernel.sh`,几分钟)后 JuiceFS 成功挂载。
2. **真实 npm 只慢 ~4.5×(可接受),远小于纯小文件微基准的 33×**:writeback 把网络下载/解压/大文件
   摊平了。**结论:JuiceFS+S3 跑 Claude Code 的 npm/build 可行**,代价约 4.5× 时间,换来"数据天然在 S3、
   免 S3 同步、跨机/跨 AZ"。代价:必须维护 HA 元数据引擎(ElastiCache)。

## 四点七、快照跨机 resume —— 已实测(详见 `快照存储架构.md`)

模拟 A 机 suspend → 删除 A 机 → B 机从三件套(mem+snapshot+rootfs)独立 resume:
- ✅ **成功**:内存计数(HB)与磁盘标记精确续上,resume ~9.8ms。
- ⚠️ **关键坑**:Firecracker snapshot 硬编码磁盘**绝对路径**,跨机必须把 rootfs 放到**与 A 机一致的路径**
  再 load(否则 `No such file or directory`)。生产要统一沙盒路径约定。

## 五、Phase 6 — 统一控制面 v1 端到端实测(2026-06-13)

> 实测环境:本地控制面进程 → 真实 DynamoDB(us-east-1) → 真实 c6g.metal(Firecracker v1.16.0)
> 先跑 Kata driver(对接已有 EKS 集群),再跑 FC driver(对接 c6g.metal 上的裸 Firecracker)

### Kata Driver E2E(控制面 + K8s + 真实 DynamoDB)— 15/15 PASS

| 测试项 | 结果 | 说明 |
|---|---|---|
| T1 服务健康 GET / | ✅ PASS | driver=kata |
| T2 capabilities | ✅ PASS | suspend_resume=False(Kata v1 正确) |
| T3 创建沙盒 → 201 | ✅ PASS | Kata Pod + ClusterIP Service + Ingress 一并创建 |
| T4 wait → running | ✅ PASS | 沙盒 Pod Running 后 DynamoDB 状态同步 |
| T5 GET 单个沙盒 | ✅ PASS | |
| T6 列出沙盒 | ✅ PASS | tenant GSI 查询正常 |
| T7 幂等键 | ✅ PASS | 重复请求返回同一 id |
| T8 locate VMM | ✅ PASS | |
| **T9 exec** | ✅ **PASS** | stdout='sandbox-ok'(kubectl exec) |
| T11 Kata suspend → 501 | ✅ PASS | capability 模型正确拦截 |
| T12 wait timeout → 408 | ✅ PASS | |
| T13 destroy | ✅ PASS | |
| T14 销毁后 GET → 404 | ✅ PASS | |
| **T15 microVM 保真度** | ✅ **PASS** | guest kernel=**6.18.28** ≠ node kernel=6.1.172;nproc=3(guest 配额,非宿主 64 核) |

### FC Driver E2E(控制面 + 裸 Firecracker + c6g.metal + 真实 DynamoDB)

| 测试项 | 结果 | 数据 |
|---|---|---|
| T1 服务健康 | ✅ PASS | driver=firecracker |
| T2 capabilities | ✅ PASS | suspend_resume=**True** |
| T3 创建沙盒 | ✅ PASS | 调 node-agent POST /vm/create,CoW rootfs 复制 + tap 配网 + FC 启动 |
| T4 wait → running | ✅ PASS | |
| T5–T8 | ✅ PASS | locate 返回节点 ip-10-0-103-109/VMM pid |
| **T10 suspend/resume** | ✅ **PASS** | snapshot→S3;**resume restore_time=1.208s** |
| T12 wait timeout | ✅ PASS | |
| T13–T14 destroy+404 | ✅ PASS | |

**关键数字:**
- FC 沙盒冷启动(rootfs CoW + VMM + guest boot): ~8s
- **suspend(Full 快照写本地) → resume:1.2s** ← 成本核心杠杆实测验证
- node-agent 健康检查:free_mem_mib=125,924(~123 GiB 可用,64vCPU c6g.metal)

**实测中发现并修复的 2 个 bug:**
1. `op_create` 把 `rootfs_path`(目的地路径)错当 src 来 cp → 改为始终从 `ROOTFS` 常量复制
2. `ROOTFS` 常量未定义 → 补加环境变量 `FC_ROOTFS=/opt/sbx/rootfs.ext4`

## 六、已完成补全项(2026-06-13 续)

| 项目 | 状态 | 结果 |
|---|---|---|
| T9 exec | ✅ 完成 | Kata driver 下 kubectl exec 打通,stdout 正常 |
| T15 microVM 保真度 | ✅ 完成 | guest kernel 6.18.28 ≠ node 6.1.172;独立 inotify;root 可绑 80 端口;可 dnf 装包 |
| LiteLLM Terraform + 部署 | ✅ **完成并实测** | `litellm.tf` apply 成功;LiteLLM 2/2 Running;claude-haiku-4-5 调用返回"OK" |
| **LiteLLM → Bedrock 端到端** | ✅ **PASS** | `curl /v1/messages` → Bedrock → Claude Haiku 返回正常响应;凭据隔离落地 |
| T16 控制面认证 | ✅ 完成 | `API_KEYS` env 控制;未配置=开发模式;配置后无 key→401,正确 key→200 |
| T17 LiteLLM 健康 | ✅ **PASS** | models=claude-opus-4-8,claude-sonnet-4-6,claude-haiku-4-5 |
| 节点 Bedrock 权限撤销 | ✅ 完成 | `terraform/phase3/main.tf` 已移除 `aws_iam_role_policy.node_bedrock` |
| Karpenter Terraform | ✅ 完成 | 双节点池(standard-arm64 + kata-metal),`install_karpenter=true` 时激活 |
| diff 快照逻辑 | ✅ 完成 | node-agent:首次 Full 保留 base,后续 Diff 只写脏页;Diff 失败自动降级 Full |
| FC exec vsock | ✅ 完成 | TAP SSH 优先 + vsock UDS 兜底;VM 配置加 vsock 设备 |
| 冒烟测试 | ✅ **21/21 PASS** | 新增 TestAPIAuth(2 case) |

## 七、第三轮补全(2026-06-13 晚)

| 项目 | 状态 | 关键数据 |
|---|---|---|
| 控制面镜像构建(arm64 原生) | ✅ 完成 | .metal 节点上 Docker 原生构建，sandbox-control-plane + node-agent 推送 ECR |
| KataDriver kubectl → Python k8s client | ✅ 完成 | 消除 kubectl 二进制依赖，集群内 in-cluster config 自动生效 |
| 控制面集群部署验证 | ✅ **PASS** | sandbox-system 2/2 Running；节点通过 IRSA 访问 DynamoDB/S3 |
| Karpenter 1.3.3 安装 | ✅ 完成 | EC2NodeClass Ready=True；NodePool Ready=True；SecurityGroup selector 修复 |
| **全链路 e2e（集群内部署）** | ✅ **17 项 ALL PASS** | T1-T15 通过，T16 dev mode，T17 LiteLLM skip |

## 八、方案 B JuiceFS workspace 实测（2026-06-15）

> 验证目标：workspace 数据在 S3（JuiceFS），快照不含 rootfs，跨机 resume 无需复制磁盘

**实测环境：** 本地控制面 + c6g.metal（Firecracker v1.16.0）+ 本地 Redis + JuiceFS S3

| 测试项 | 结果 | 说明 |
|---|---|---|
| T1 创建沙盒 | ✅ PASS | FC VM 正常启动 |
| T2 wait running | ✅ PASS | |
| T4 suspend（方案 B）| ✅ **PASS** | S3 仅有 vm.snapshot + vm.snapshot.base，**无 rootfs** ✅ |
| T5 resume | ✅ **PASS** | restore_time=**1.16s** |
| T6 running after resume | ✅ PASS | |
| T8 destroy | ✅ PASS | |

**关键数字：**
- 快照创建时间：34.7s（Full 快照，2GB 内存，EBS gp3）→ diff 快照可大幅提速
- resume：1.16s
- S3 快照大小：~2GB（仅内存，无 rootfs 磁盘）← 方案 A 会有额外 6GB rootfs

**已验证的关键差异（方案 B vs 方案 A）：**
- 方案 A：`vm.mem + vm.snapshot + rootfs.ext4` 三件套（~8GB）
- 方案 B：`vm.snapshot + vm.snapshot.base` 两件套（~2GB）✅ 实测确认

**已修复的 Bug（测试中发现）：**
1. `_fc()` 默认 timeout=15s 不够 snapshot/create（16s）→ 改为 120s
2. Mac Docker export → tar.gz → Linux ext4 tar 解压路径问题（`sbin/sbxinit` vs `usr/sbin/sbxinit`）→ 已验证 `sbin/sbxinit` 正确存在
3. node-agent systemd 服务端口冲突（重启时旧进程未退出）→ 先 pkill 再 restart

**未完成（需 FUSE kernel）：**
- JuiceFS 实际挂载验证：CI kernel 无 FUSE，`/workspace` 未挂载（`sbxinit` 中 JuiceFS mount 失败，继续执行）
- 需用 `scripts/build-fuse-kernel.sh` 自编 FUSE kernel 才能验证 /workspace 数据持久性

## 九、下一阶段

- **JuiceFS FUSE kernel**：自编带 FUSE 的 guest kernel，验证 /workspace 数据实际在 S3 持久化
- **diff 快照优化**：Full 快照 34.7s → diff 快照预计 <5s（只写脏页）
- **scale-to-zero 唤醒代理**：挂起沙盒被流量自动拉起（ingress-nginx 不支持，需自研 proxy 层）
- **可观测性**：metrics/日志聚合/健康告警（CloudWatch/Prometheus）
- **H4 真实负载密度**：真实 Claude Code 工作集峰值、空闲快照回收比例
- **JuiceFS 元数据引擎 HA**：生产用 ElastiCache(多AZ)，非 POC 单机 Redis
