# POC 实测结果 —— H1 / H3 / H4 + 快照 + 文件系统

> 实测日期:2026-06-11 ~ 06-12 · 区域:us-east-1 · 验证方式:全程经 AWS SSM
> 机型:H1/H4/快照初轮用 `c7g.metal`,H3/JuiceFS/跨机轮用 `c6g.metal`(两者同为 Graviton 64vCPU/128GiB,结论通用)
> 基础设施由 `terraform/phase1`(单机)与 `terraform/phase3`(EKS)创建
>
> **相关专题文档**:文件系统对比见 `文件系统方案对比.md`;快照存储架构见 `快照存储架构.md`;
> Kata 快照定位见 `Kata快照定位机制.md`。本文件汇总各轮实测的核心数字与结论。

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

## 四点五、H3 —— EKS + Kata 编排 + 任意端口(✅ 通过)

用 Terraform(官方 `terraform-aws-modules/eks` + `vpc`)创建 EKS 1.31 + Graviton `c7g.metal` 托管节点组,装 Kata 3.31.0,部署 Claude Code 沙盒 Pod,端到端验证三要素。

**最终验证结果:**

| H3 要素 | 实测 | 结论 |
|---|---|---|
| (a) 自定义镜像 | Pod `runtimeClassName: kata-qemu` + 自建 ECR 镜像 `claude-sbx:poc` 正常拉起 | ✅ |
| microVM 保真度 | guest `kernel=6.18.28`(节点是 `6.1.172`,**完全不同**)、`nproc=1`(自己的配额)、独立 inotify=16052 | ✅ 真 microVM,非共享宿主内核 |
| Claude Code | `2.1.173` 在 Kata Pod 内就绪 | ✅ |
| (b) 任意端口 | 沙盒内起 8080 dev server,经**共享 ingress-nginx(单 NLB)按 Host 头 `8080-sbx1.sbx.example.com` 路由**,集群内访问返回沙盒内容 | ✅ 3.4 节方案验证可行 |
| (c) 24×7 | Pod `Running` 无 TTL,长驻 | ✅ |

> 端口暴露:集群内经 ingress Host 路由已验证打通(`hello from kata microVM sandbox`)。外部 NLB 那一跳因测试机出口 IP 被 NLB 安全组挡未直连,生产配好 Route53 通配符 DNS + 安全组即通——路由机制本身已证明。

**过程中暴露的真实坑(对客户决策有价值):**

1. **EIP 配额坑(共享账号典型)**:默认私有子网 + NAT 方案,NAT 需 EIP。本账号 EIP 配额 5 却已被无关资源占满 16 个 → `AddressLimitExceeded` → NAT 建不成 → 节点无法出网 → **join 失败(`NodeCreationFailure`)**。
   - **修复**:节点组改公有子网 + 公网 IP、禁用 NAT。生产应提前申请 EIP 配额,或用 VPC Endpoint 让私有子网免 NAT 访问 ECR/S3/EKS。
2. **`.metal` 节点启动慢**:Graviton `.metal` cloud-init 实测 **~606 秒(10 分钟)**,EKS 节点组创建总耗时远超普通实例。→ 印证"裸金属需 warm buffer",Karpenter 扩容要预留缓冲。
3. **Kata `kata-clh`(Cloud Hypervisor)shim 默认未注册(R2 实锤)**:kata-deploy 3.31.0 helm chart 默认只把 **qemu 系列** runtime 写进 containerd drop-in,**没写 `kata-clh`**——RuntimeClass `kata-clh` 存在但 containerd 无对应 handler,Pod 报 `no runtime for "kata-clh" is configured`。
   - **回退(已验证)**:改用 **`kata-qemu`**,保真度与 clh 完全相同(都是真 guest 内核),仅启动稍慢。这正是文档 R2 早写好的预案。
   - **生产**:若要 clh(virtio-fs/热插拔),需在 kata-deploy values 显式启用 clh shim 并确认其 containerd 配置写入。**arm64 + clh 的开箱可用性确实不如 qemu**,选型时纳入。
4. **节点网络重建会让 Pod 短暂 NotReady**:Terraform 改子网/重建节点组时,运行中的节点会经历 unreachable→恢复;生产变更网络需滚动、避开业务高峰。
5. **Kata Pod 需设 resources**:`BestEffort`(无 requests/limits)的 Kata Pod 实测不稳定(Exit 9 / CrashLoopBackOff);设明确 `requests/limits`(2vCPU/4Gi)后稳定 0 重启。沙盒模板必须带资源声明。

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

## 五、尚未验证(后续)

- **dev server HMR 持续监听**:本轮只验了基础 inotify 触发;Vite/Next.js 大量文件持续监听需专门压。
- **H4 完整密度**:真实 Claude Code 负载下的工作集峰值、diff 快照、空闲回收比例(本轮是空载密度)。
- **多租户凭据隔离落地**:LiteLLM 网关 / 每租户 STS 短期凭据(见 `Workshop方案借鉴与优化.md` §1)。
- **JuiceFS 元数据引擎 HA**:生产用 ElastiCache(多AZ),非 POC 的单机 Redis。
