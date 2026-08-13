# Terraform —— Claude Code 沙盒 POC 基础设施

所有 AWS 基础设施用 Terraform 管理。按阶段逐步 apply；当前主线是裸
Firecracker，并已把 EKS system 控制面节点与 sandbox 数据面节点分离。

## 目录结构

```
terraform/
├── phase1/                单机 Firecracker 实验环境
├── stage1-dynamodb/       sandbox / events / tap-idx / nodes / locks 状态表
├── phase3/                EKS + On-Demand system 节点组 + sandbox OD/Spot 双池 + 持久 EBS + 预热池
└── stage2-control-plane/  控制面、node-agent、LiteLLM、Ingress、可观测性与对应 IAM/K8s 资源
```

> Firecracker 安装、guest 内核、rootfs 构建、microVM 启动是**主机内**操作,不归 Terraform 管 —— 见
> [`docs/POC-技术文档.md`](../docs/POC-技术文档.md) 第 3 节。Terraform 只负责 AWS 侧资源。

## Phase 1 —— 立即可用

```bash
cd phase1

# 1) 准备 SSH key(若还没有)
ssh-keygen -t ed25519 -f ~/.ssh/claude-sbx-poc -N ""

# 2) init + apply(自动填入你的公网 IP 限制 SSH)
terraform init
terraform apply -var="my_ip_cidr=$(curl -s https://checkip.amazonaws.com)/32"

# 3) 登录主机(或用 SSM Session,免开 22 口)
$(terraform output -raw ssh_command)

# 4) 主机内一键准备:装 docker + 编 FUSE 内核 + 构建 rootfs(含 Claude Code+JuiceFS) + 配网
#    (scripts/setup-host.sh 已含全部步骤,幂等可重跑)
sudo bash setup-host.sh
#    设 SKIP_FUSE_KERNEL=1 可跳过编内核(仅本地 ext4 workspace、不挂 JuiceFS 时)

# 5) 起 microVM
sudo firecracker --no-api --config-file /opt/sbx/vmconfig.json

# 6) 用完即毁(沙盒主机按小时计费,较贵)
terraform destroy -var="my_ip_cidr=$(curl -s https://checkip.amazonaws.com)/32"
```

> 默认起的是 `amd64` + `r8i.8xlarge`（Nitro 嵌套虚拟化自动开启）。要走 Graviton 裸金属，
> 在上面的 apply/destroy 上都加 `-var="node_arch=arm64"`。

> ⚠️ **关键(实测坐实 R3):** Firecracker CI 默认内核【无 FUSE】,JuiceFS/s3fs/mountpoint
> 在 guest 内挂不上。`setup-host.sh` 默认调用 `scripts/build-fuse-kernel.sh` 原生编一个带 FUSE 的
> 内核(架构随主机;c6g.metal 64 核几分钟),vmconfig 指向 `/opt/sbx/vmlinux-fuse`。

Phase 1 创建的资源:
- 1 台 Firecracker 主机（默认 `amd64` → `r8i.8xlarge`；`node_arch=arm64` 时为 `c6g.metal`）+ 200 GiB gp3
- 主机 IAM 角色:`bedrock:InvokeModel*`(限 `anthropic.*` 模型与 `us.anthropic.*` inference profile)→ 沙盒走 IAM 凭据链调 Bedrock,无需长期 key
- 安全组:仅放行你的 IP 的 22 口(也挂了 SSM,可免 22 口用 Session Manager 登录)
- ECR 仓库 `claude-sbx`

## EKS 控制面 / 数据面分离

`phase3` 创建三个职责明确的托管节点组（数据面两个池，控制面一个）：

| 节点组 | 容量类型 | 默认台数 | 作用 |
|---|---|---|---|
| `system_arm64` | On-Demand（固定） | `system_node_count`，默认 2 × `m7g.large` | 控制面、LiteLLM、Ingress、系统 Pod |
| `sandbox_<arch>` | On-Demand | `sandbox_od_node_count`，默认 1 | 不可中断业务 + **接收 Spot 疏散** |
| `sandbox_<arch>_spot` | Spot | `sandbox_spot_node_count`，默认 0（不创建） | 无配置通道/无主动任务下发的被动用户实例 |

两个数据面池由 `local.sandbox_ng_base` 派生，共用同一 AZ、同 AMI、同 cloudinit、
同持久状态 EBS、同 `dedicated=sandbox:NoSchedule` taint 和 `sandbox=true` label，
只有 `capacity_type` / 实例列表 / 台数不同，另各带一个 `capacity-type=od|spot` label。
node-agent DaemonSet 按 `sandbox=true` 选节点，所以 Spot 池起来即自动被覆盖，无需额外配置。

`system_arm64` 组的定义完全不受数据面变量影响 —— 控制面永远是 On-Demand。

```bash
# 数据面:2 台 OD + 4 台 Spot(Spot 给多个候选机型提高容量命中率)
terraform apply \
  -var="sandbox_od_node_count=2" \
  -var="sandbox_spot_node_count=4" \
  -var='sandbox_spot_instance_types=["r8i.8xlarge","r8i.12xlarge","r8i.16xlarge"]' ...
```

⚠️ **只做机型分散，不做 AZ 分散**：持久状态 EBS 不能跨 AZ attach（方案C 的硬约束），
所以 Spot 池和 OD 池都钉在 `sandbox_az_index` 这一个 AZ 里。这会降低 Spot 容量命中率，
是为了让被回收节点上的幸存状态卷能挂到同 AZ 的新节点而付的代价。

⚠️ **开 Spot 池前必须先打开疏散**：stage2 的 `reclaim_auto_evacuate` 默认 `false`
（DRY-RUN，只把"会疏散哪些"记进 `/reclaim/status`），此时 Spot 被回收沙盒直接丢。
即使置 `true`，当前实现也只做到「打 Diff 快照到本机持久 EBS」——**跨机拉起仍要运维
把幸存卷 detach/attach 到新节点，没有自动化**；且 `_evacuate_local` 是串行的，
客户复盘建议的并发 6-8 还没落地。

🔴 **控制面目前不会主动把沙盒投向 Spot 池**：`_pick_node()`
（`sandbox-api/drivers/firecracker.py`）只按注册表里的 `free_mem_mib` 降序挑节点，
完全不看 `capacity-type` label。2026-08-13 真机实测连续建 5 个沙盒**全部落在 OD 节点**，
Spot 节点一个没拿到。也就是说双池能建出来，但「被动用户实例进 Spot 池」这条分流策略
还只是节点组层面的准备，控制面侧未实现 —— 现在开 Spot 池只会白付一台机器的钱。

### 单 AZ 亲和的节点预热池

> 注意与**沙盒暖池**区分：stage2 的 `warm_pool_size` 是控制面预先创建的**空白 microVM**
> 池（缩短 create 延迟）；这里的 `sandbox_warm_pool_size` 是 EC2 侧预先备好的**节点**
> 池（缩短节点替换/扩容延迟）。两者独立，可同时开。

`sandbox_warm_pool_size`（默认 0=关闭）在数据面 ASG 上挂一个 ASG warm pool：
预热实例启动、过健康检查后停在 `Warmed:Stopped`，ASG 需要补节点时直接 start。
**2026-08-13 真机实测**（`r8i.8xlarge`，见
`docs/OD-Spot双池-节点预热池-预打包运行环境-真机测试报告-2026-08-13.md`）：
terminate → 新节点 `Ready` = **44 秒**，同集群冷启动基线 **5 分 03 秒**，快约 6.9 倍
（其中 warm pool 出实例只花 7 s）。这是满足客户整机恢复 SLO ≤5 min 的关键一环。

```bash
terraform apply -var="sandbox_warm_pool_size=1" ...                              # 默认挂 OD 池
terraform apply -var="sandbox_warm_pool_size=1" -var="sandbox_warm_pool_target=both" ...
```

- **单 AZ 亲和是天然的**：warm pool 不能单独指定子网，它继承 ASG 的子网，而数据面 ASG
  已被钉死在 `sandbox_az_index` 一个 AZ → 预热出来的机器必定与幸存状态卷同 AZ。
- `sandbox_warm_pool_target` 默认 `od`：被回收的是 Spot 节点，需要提前备好的是**接收
  疏散的 OD 容量**。Spot ASG 能否走 Stopped 预热池取决于 AWS 侧支持，本仓库未验证，
  选 `spot`/`both` 时若 `put-warm-pool` 被拒，apply 会失败。
- EKS 托管节点组不暴露 warm pool，和 grace period 一样靠 `null_resource` 在节点组建好后
  调 `aws autoscaling put-warm-pool`（destroy 时 `delete-warm-pool --force-delete`）。

⚠️ 真机实测出的限制，用前必读：

1. 🔴 **预热池与 `rootfs_images` 目前不能同时用**：ASG 把「EC2 running + 健康检查通过」
   当作预热完成就停机，**不等 cloud-init 跑完**。实测预热实例只活了约 1 分钟，
   刚造完默认 `min` 模板就被停掉，`rootfs-<name>.ext4` 全部缺失；而
   `node-agent` 的 `_rootfs_template_path()` 对找不到的模板**静默回退 min**
   → 从预热池起来的节点上 `image=claude-code` 会返回 `running` 但 guest 里没有 claude。
   造 3 个命名模板约需额外 74 s，远超预热实例的存活窗口。
   短期二选一：要预热池就只用默认 `min`；要预打包运行环境就先别开预热池。
   根治要么把模板烤进自定义 AMI，要么给 node-agent 加启动时模板自愈 + 缺失即报错。
2. **它不会帮你恢复状态**：预热只缩短「节点 Ready」时间。实测节点被替换后，
   旧节点的 400G 状态卷残留为 `available` 孤儿卷，新节点用自己的空卷，
   **没有任何自动 detach/attach**。
3. **成本**：每台预热实例各持一块 `state_ebs_size_gb`（默认 400G）的状态盘，
   `delete_on_termination=false` → 停机期间 EBS 照收费，实例被替换后卷还会残留。
   实测这些卷上只有 `Name`/`eks:*` tag，**没有统一项目 tag**，共享账号里按 tag
   审核清理暂时做不到（`default_tags` 仍是待办）。
4. **不会产生 NotReady 的 Node 对象**：预热实例的第一次开机来不及跑完 nodeadm bootstrap
   就被停机，实测全程 `kubectl get nodes` 只有正常节点数，没有多出 NotReady Node
   ——不需要为此放宽节点数告警。

## 选择 sandbox 数据节点架构（Graviton / Intel x86）

`node_arch` 只描述 sandbox 数据节点，system 节点始终为 arm64：

| `node_arch` | 默认实例 | 虚拟化方式 | AMI | 状态 |
|---|---|---|---|---|
| `amd64`（默认） | `r8i.8xlarge` | Nitro nested virtualization | AL2023 x86_64 | **推荐主线** |
| `arm64` | `c6g.metal` | Graviton 裸金属 KVM | AL2023 ARM64 | 备选，暂缓 |

x86 支持 `r8i.*` 与 `i7i.*` 两个系列，默认 `r8i.8xlarge`。Terraform 会为 amd64
显式开启 `cpu_options.nested_virtualization=enabled`：

- **`r8i.*`（推荐，默认）**：无本地 NVMe，快照和 rootfs 全部落在
  `delete_on_termination=false` 的持久状态 EBS 上 —— 这正是方案C 依赖的幸存介质。
  同 vCPU/内存配置比 i7i 便宜（`r8i.8xlarge` 按需约 $1,623/月，1 年 SP 约 $1,002/月，
  比 `i7i.8xlarge` 每月省 $582 / $333）。
- **`i7i.*`（保留）**：本地 NVMe 是 **instance store**，spot 回收或停机即销毁，
  存不了需要幸存的状态。保留该系列仅为复现既有 i7i 真机报告。
- **规格不建议低于 8xlarge**：更小规格的 EBS 吞吐受突发积分限制，只能短时冲峰，
  无法持续跑满。`r8i.8xlarge` 的 EBS 吞吐为 1250 MB/s（10 Gbps），而 gp3 单卷吞吐上限是
  1000 MB/s，所以单卷配置下真正的瓶颈在卷而不在实例。吞吐不足会直接拉长 spot 回收窗口内的
  疏散落盘时间。
- **Graviton 暂缓**：Graviton 不支持 Nitro 嵌套虚拟化，只能上裸金属；而裸金属被回收后
  重新启动要 10 分钟以上，不适合按 spot 中断窗口疏散的运行模型。

```bash
# 默认即 Intel x86 + r8i.8xlarge，无需传 node_arch
terraform apply ...

# 覆盖为其他 x86 规格（r8i.* 或 i7i.*）
terraform apply -var="sandbox_instance_type=r8i.16xlarge" ...

# 切回 Graviton 裸金属（备选路线）
terraform apply -var="node_arch=arm64" ...
```

> ⚠️ **已有 state 的集群注意**：默认值从 `arm64` 翻到 `amd64` 后，托管节点组的 key 会从
> `sandbox_arm64` 变成 `sandbox_amd64`，`terraform plan` 会显示销毁旧节点组 + 新建。
> 想维持现有 arm64 集群不变，必须显式传 `-var="node_arch=arm64"`（或写进 tfvars）。

Phase 3 默认把沙盒节点钉在 VPC 的第一个可用区，以保证持久状态 EBS 可在节点间挂载。
若 ASG 报 `InsufficientInstanceCapacity`，可用
`-var="sandbox_az_index=1"`（region-b）或 `2`（region-c）切换整个节点组所在 AZ。

切到 x86 时会变化的是 sandbox AMI、默认实例、Firecracker 二进制与 guest
内核/rootfs 架构。控制面和 LiteLLM 仍调度到 arm64 system 节点。镜像应分开构建：

```bash
bash scripts/build_and_push.sh \
  --control-plane-platform linux/arm64 \
  --node-agent-platform linux/amd64
```

> x86 部署前需确认目标区域提供所选规格（`r8i.*` / `i7i.*`）且 `SupportedFeatures`
> 包含 `nested-virtualization`。完整命令见 `docs/deploy.md` Step 0.5。

历史真机验证在 `sandbox_az_index=2`（`us-east-1c`）完成：`2 × m7g.large`
system + `1 × i7i.8xlarge` sandbox 的调度隔离、Firecracker 生命周期、
恢复后 exec、LiteLLM Bedrock 调用、控制面 leader 故障转移和完整清理均通过。
详见[控制面与数据面分离 i7i 真机测试报告](../docs/控制面数据面分离-i7i真机测试报告-2026-08-11.md)。
当前默认已切到 `r8i.8xlarge`，Terraform 路径与 i7i 完全一致，但 EBS 落盘吞吐、
回收窗口内疏散、跨机恢复三项**尚未在 r8i 上跑真机 E2E**。

## 预打包运行环境（命名 rootfs 模板）

沙盒的 `image` 字段选的是节点上的 rootfs 模板 `/opt/sbx/rootfs-{name}.ext4`，
create 时 CoW 复制一份。模板由 `scripts/build-rootfs-image.sh <name> <bucket>` 构建上传，
节点在 pre-bootstrap 里按 `rootfs_images` 逐个拉取并造 ext4。内置预设：

| `name` | 内容 |
|---|---|
| `min` | 基底：python + sshd + vsock exec agent（默认，不用列进 `rootfs_images`） |
| `web` | 叠加 demo 首页，开机自起 `:80` |
| `claude-code` | 叠加 Node.js LTS + `@anthropic-ai/claude-code`（`claude` 在 PATH） |
| `openclaw` | 叠加 Node.js LTS + `openclaw`（`openclaw` 在 PATH） |

```bash
# 1) 构建上传（PLATFORM 必须与 node_arch 一致）
PLATFORM=linux/amd64 bash scripts/build-rootfs-image.sh claude-code "$BUCKET"
PLATFORM=linux/amd64 bash scripts/build-rootfs-image.sh openclaw    "$BUCKET"

# 2) 让节点造模板（phase3）
terraform apply -var="rootfs_images=web,claude-code,openclaw" ...

# 3) 让控制面/Portal 允许选它（stage2-control-plane）
terraform apply -var="sandbox_images=min,web,claude-code,openclaw" ...
```

Node.js/CLI 都在 Dockerfile 层里装（`docker build --platform` 跨架构走 qemu），
版本可用 `NODE_VERSION` / `CLAUDE_CODE_VERSION` / `OPENCLAW_VERSION` 覆盖。
模板 ext4 大小由 `rootfs_template_size_mib` 控制（默认 2048 MiB）。

⚠️ 默认 `rootfs_images` 只含 `web`：每多一个模板，节点 pre-bootstrap 就多一次
「下载 + dd + mkfs + 解包」，该阶段过长会打断 kubelet 心跳 → ASG 替换循环（见 `main.tf` 头部）。
只装用得上的；要进一步缩短节点引导，下一步应把模板烤进自定义 AMI，而不是继续往 userdata 里堆。

⚠️ guest 里没有集群 DNS（只有 tap 网段 + 8.8.8.8），解析不了 `litellm.litellm`。
`claude` 要走网关必须由调用方注入 guest 可达的 `ANTHROPIC_BASE_URL`；镜像里放了
`/etc/sbx-env`（`sbxinit` 启动时 `set -a` source 它，exec 出来的命令会继承）作为注入点。
这两个预打包运行环境**尚未跑真机 E2E**。

## Stage 2 可观测性

`stage2-control-plane/observability.tf` 与 `p2_observability.tf` 提供三个显式开关：

| 开关 | 资源 |
|---|---|
| `enable_observability_stack=true` | 集群内 Prometheus、Alertmanager、Grafana、ServiceMonitor/PodMonitor、5 类告警、8 面板 Dashboard |
| `enable_amp_remote_write=true` | AMP workspace、Prometheus remote-write IRSA/SigV4；要求上一个开关同时为 `true` |
| `enable_p2_observability=true` | CloudWatch Logs、Fluent Bit、ADOT/X-Ray、AMG datasource/Dashboard 自动配置；要求前两个开关为 `true` |

可选传入已有 AMG workspace：

```hcl
managed_grafana_workspace_id       = "g-xxxxxxxxxx"
managed_grafana_vpc_id             = "vpc-..."
managed_grafana_subnet_ids         = ["subnet-a", "subnet-b"]
managed_grafana_security_group_id  = "sg-..."
```

Terraform 会给 AMG workspace role 增加最小 AMP 查询权限，并在其 VPC 创建
`aps-workspaces` Interface Endpoint；不会创建或删除 AMG workspace。启用 P2 后，
`configure-managed-grafana.sh` 使用 15 分钟 token 幂等配置 datasource/Dashboard 并立即清理。
完整参数和验证命令见
[`docs/deploy.md` Step 6.2](../docs/deploy.md#step-62-部署可观测性p1推荐)。

Prometheus 与 node-agent 的指标不使用 sandbox ID 标签。Grafana admin password 通过
Helm `set_sensitive` 注入，不放进普通 values。Kubernetes 和 Helm provider 使用
`aws eks get-token` exec credential，长时间 apply 可以刷新 EKS token；运行 Terraform
的环境必须能从 `PATH` 调用 AWS CLI。

真实 AWS 验证覆盖 29/29 targets、AMP remote-write、AMG datasource/query、
快照损坏告警和 Terraform 零漂移，见
[P1 可观测性真机测试报告](../docs/P1可观测性-真机测试报告-2026-08-12.md)。
P2 集中日志、tracing 与 AMG 自动化证据见
[P2 可观测性真机测试报告](../docs/P2可观测性-真机测试报告-2026-08-12.md)。

## 前置：申请 EC2 vCPU 配额

x86 默认 `r8i.8xlarge` 需要 32 vCPU（Standard 实例类别），Graviton `c6g.metal`
需要 64 vCPU。若 apply 报 `VcpuLimitExceeded`，到 Service Quotas 为对应实例类别
申请提额；注意 On-Demand 与 Spot 是两套独立配额。

## 鉴权说明

Phase 1 的 Terraform 给主机挂了 **Bedrock IAM 角色**(对应 POC 文档 1.8 方式 B),沙盒走宿主凭据链即可,无需把 key 写进代码或环境变量 —— 这也更接近"凭据不进沙盒"的生产形态。
若想用方式 A(Bedrock API key),在 guest 内 `export AWS_BEARER_TOKEN_BEDROCK=...` 即可,Terraform 不管 key。

> ⚠️ 上线前到 Bedrock 控制台 "Model access" 开通 Anthropic 模型,并复制准确的 inference profile ID(us-east-1 通常需 `us.` 跨区前缀)。

## 推荐部署顺序

1. `stage1-dynamodb`：创建 5 张状态与协调表。
2. 构建对应数据节点架构的 rootfs 并上传 S3，作为节点初始化输入；要用预打包运行环境
   （`claude-code` / `openclaw`）的话在这一步一起构建上传。
3. `phase3`：创建 EKS、system 组 + 数据面 OD/Spot 两个池、sandbox 持久状态 EBS
   和可选的节点预热池。
4. 分别构建 arm64 控制面镜像和匹配数据节点架构的 node-agent 镜像。
5. `stage2-control-plane`：部署控制面、node-agent、LiteLLM、可选 Ingress 和可选可观测性栈。

完整变量、验证和销毁步骤以 [`docs/deploy.md`](../docs/deploy.md) 为准。
