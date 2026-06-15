# 在 AWS 上自建 Claude Code 沙盒平台 — POC 技术文档

> 目标读者:负责在 AWS 上搭建最小可行原型(POC)、验证可行性的工程师
> 版本日期:2026-06-11
> 背景:客户现用 Fly.io(Firecracker microVM on bare metal)+ JuiceFS + S3 运行 Claude Code agent,遇到成本与扩展性问题,希望迁移到 AWS 自建。已排除 E2B-on-AWS(运维过重)与 AWS AgentCore(无法自定义镜像/任意端口/24×7)。

---

## 0. POC 要回答的核心问题

POC **不是**要把生产平台一次性搭完,而是按风险从高到低,**用最小代价验证三个假设**:

| # | 假设 | 验证方式 | 阶段 |
|---|---|---|---|
| H1 | **Claude Code 在 Firecracker microVM 内的行为与裸机一致**(这是客户离开普通容器、选择 Fly 的根本原因) | 在单台 Graviton .metal 上裸跑 Firecracker,装 Claude Code,跑真实 agentic 任务 + 保真度检查清单 | Phase 1 |
| H2(可选) | **文件系统可沿用 JuiceFS + S3 架构**,且不拖垮 Claude Code 的重 I/O / 文件监听 | 在 microVM 内挂 JuiceFS(S3 后端),把 workspace 放上去对比本地 ext4。**POC 第一轮先用本地 ext4,H1 通过后再验** | Phase 2 |
| H3 | **可用 K8s 原生方式编排**(EKS + Kata),满足自定义镜像 / 任意端口 / 24×7,且运维轻于 E2B-on-Nomad | EKS + .metal 节点组 + Kata RuntimeClass,跑沙盒 Pod + NLB 暴露端口 | Phase 3 |
| H4 | **密度与成本可控**,能装箱上千并发 | 在单台 .metal 上压测并发 microVM 数、启动延迟、快照恢复 | Phase 5 |

**核心 KPI:**Claude Code 在沙盒内执行的任务(git clone / npm install / build / 跑测试 / 起 dev server / 文件监听 / 嵌套进程)**成功率与裸机一致**,且无普通容器下的异常行为。

---

## 1. 技术选型

### 1.1 底层架构选型

**结论:Firecracker microVM,与 Fly.io 同构。**

| 候选 | 隔离机制 | 裸机保真度 | 需 .metal | 结论 |
|---|---|---|---|---|
| **Firecracker microVM** | 每沙盒独立 guest 内核 + KVM | **最高(与 Fly 同构)** | 是 | **✅ 采用** |
| Kata + Cloud Hypervisor | 每 Pod 独立 guest 内核 + KVM,支持 virtio-fs/热插拔 | 最高 | 是 | ✅ 编排层采用(Phase 3) |
| Kata + Firecracker 后端 | 同上但无 virtio-fs、无热插拔、仅块设备 | 最高 | 是 | ⚠️ 仅极致小footprint时用 |
| gVisor (runsc) | 用户态重实现内核 | **低**(合成 /proc、syscall 缺口、io_uring 默认关、fork/exec 开销) | 否 | ❌ 否决:背离裸机保真度 |
| 普通容器 (runc) | 共享宿主内核 | 最低(泄漏宿主 /proc、共享 inotify/PID 配额、seccomp 裁剪) | 否 | ❌ 否决:正是客户要逃离的 |

**为什么 Claude Code 必须用 microVM 而非容器(H1 的理论依据):**
Claude Code 是 fork/exec 密集、进程树深、文件监听重、且执行不可信生成代码的 CLI agent。microVM 给每个沙盒一个**真实独立 Linux 内核**,因此具备:
- 真实私有 `/proc`、`/sys`(容器会泄漏宿主值 —— 这正是 LXCFS 存在的原因);
- 准确的 cgroup / PID 命名空间视图、真实 PID 1、完整进程树回收;
- **独立的 inotify 配额**(`fs.inotify.max_user_watches` 在宿主上是跨容器共享的 per-user 限制,密集容器会耗尽);
- 完整 syscall 面(无宿主 seccomp 静默拦截)、原生 fork/exec 性能、io_uring 可用;
- 完整 root(装包、nested docker、绑低端口、自定义 sysctl)与 VM 级隔离。

**POC 策略:Phase 1 先用"裸 Firecracker"而非 Kata。** 因为 Fly 本身就是裸 Firecracker,裸跑是**对 H1 最干净的验证**,且避开 K8s/Kata 复杂度;确认保真度后,Phase 3 再上 Kata+EKS 做编排层。

#### 1.1.1 EKS / Kata / Firecracker 三者关系与 Kata 后端选型

**三者不是竞争关系,而是上下三层、各管一段:**

```
┌─────────────────────────────────────────────────┐
│  EKS / Kubernetes        ——「调度层」              │
│  决定:沙盒 Pod 放哪台节点、健康与否、扩缩容          │
│  它只会通过 CRI 接口说:"containerd,起一个容器"      │
└───────────────────────┬─────────────────────────┘
                        │ CRI(容器运行时接口)
┌───────────────────────┴─────────────────────────┐
│  containerd + Kata Containers ——「翻译/适配层」     │
│  Kata 实现 containerd-shim-kata-v2,对上假装成普通   │
│  容器运行时,对下把"起容器"翻译成"起一台 microVM"     │
└───────────────────────┬─────────────────────────┘
                        │ 驱动 VMM 启动 microVM
┌───────────────────────┴─────────────────────────┐
│  VMM(Firecracker / Cloud Hypervisor / QEMU)      │
│                          ——「真正干活的引擎」        │
│  真的开一台带独立 Linux 内核的 microVM              │
└─────────────────────────────────────────────────┘
```

**为什么一定要有 Kata 这层?** 因为 **K8s 天生只会跟"容器运行时(CRI)"对话,它不认识 Firecracker**。Firecracker 是 VMM,接口是"启动 microVM/配内核/配 rootfs 块设备",和 CRI 的"起容器"是两套语言。Kata 就是中间的**适配器**:对上以普通容器运行时的身份接入 K8s(所以用 `runtimeClassName: kata-xxx` 就能无感切换),对下驱动 VMM 真正开 VM。
- 同一个 Pod yaml,去掉 `runtimeClassName` 就是普通共享内核容器(runc),加上就是独立内核 microVM,**上层代码完全不用变**。
- 若想跳过 Kata 让 EKS 直接管 Firecracker —— K8s 没这能力,等于要自己重写一个 Kata;那还不如走**裸 Firecracker 路线**(Fly.io 做法),但调度/状态库/网络全得自研。这正是本 POC 两条路线的取舍。

> 术语澄清:QEMU、Cloud Hypervisor、Firecracker 都是**并列的 VMM**,Firecracker(kata-fc)只是其中一种,不是一个大类。所以准确说法是"Kata 可接多种 VMM 后端",而非"多种 Firecracker 引擎"。

**Kata 后端(VMM)选型对比:**

| 后端 | 保真度 | virtio-fs(共享文件系统) | 设备热插拔 | 启动速度 | 成熟度 |
|---|---|---|---|---|---|
| **kata-qemu**(QEMU) | 高(真 guest 内核) | ✅ 支持 | ✅ 支持 | 较慢 | **最成熟、arm64 稳定 ✅ 采用** |
| kata-clh(Cloud Hypervisor) | 高(真 guest 内核) | ✅ 支持 | ✅ 支持 | 快 | ⚠️ arm64 默认未注册(R2 实锤) |
| kata-fc(Firecracker) | 高(真 guest 内核) | ❌ 不支持,仅块设备 | ❌ 不支持 | 最快 | ⚠️ 无 virtio-fs/热插拔 |

**✅ 实测结论:本方案采用 `kata-qemu`,不采用 `kata-clh`。**

实测(R2 坐实,见 POC-实测结果.md §四点五)：kata-deploy 3.31 arm64 默认**只注册 qemu 系列** containerd handler，`kata-clh` RuntimeClass 存在但 containerd 无对应 handler，Pod 报 `no runtime for "kata-clh" is configured`。`kata-qemu` 在 arm64 完全稳定，保真度与 clh 相同，故直接采用。

- **不选 kata-clh**：arm64 上开箱即用性不足，需显式启用，不值得为此引入不稳定性。
- **不选 kata-fc**：无 virtio-fs，共享 workspace 场景受限；且快照接口与 kata-qemu 不统一。
- **kata-qemu 足够**：24×7 长驻场景启动速度差异可忽略，稳定性和成熟度更重要。

> **代码一致性(已统一):** `k8s/sandbox.yaml`、`sandbox-api/drivers/kata.py`、`terraform/stage2-control-plane` ConfigMap 三处均已统一为 `kata-qemu`。

### 1.2 AWS 产品选型

| 用途 | 选型 | 说明 |
|---|---|---|
| **计算(裸金属)** | **Graviton .metal**:POC 实测用 `c6g.metal`(64 vCPU/128 GiB,~$2.18/hr*);更新代可用 `c7g.metal`/`m7g.metal` | KVM 只在 `.metal` 暴露。Graviton(arm64)$/vCPU、$/GiB 最优,Claude Code(Node)原生跑 arm64。生产做密度可上 Graviton4 `m8g.metal-48xl`(192 vCPU/768 GiB) |
| 编排(Phase 3) | **EKS** + `.metal` 托管节点组 + **Kata RuntimeClass** | K8s 接管调度/健康/扩缩,比自管 Nomad 轻 |
| 节点 OS | **Amazon Linux 2023 (arm64)** 或 Bottlerocket | POC 用 AL2023 配合 `kata-deploy` 最直接 |
| 自动扩缩 | Karpenter(生产) | POC 阶段手动管节点即可 |
| 镜像仓库 | **ECR** | 存沙盒镜像/rootfs 构建产物 |
| 网络/任意端口 | VPC + **NLB**(L4,保留任意 TCP/UDP)+ 安全组 | ALB 仅 HTTP;任意端口须用 NLB |
| 对象存储 | **S3** | JuiceFS 数据后端 + Firecracker 快照存储 |
| JuiceFS 元数据 | **ElastiCache for Redis**(POC 可先用单机 Redis 容器) | JuiceFS 需独立元数据引擎 |
| 块存储 | 本地 NVMe(`i` 系列 .metal)或 **EBS gp3** | rootfs / 快照暂存。`c6g.metal` 无本地盘,用 EBS gp3 |

> *价格为 us-east-1 按需近似值,做成本模型前请用 AWS Pricing / vantage.sh 复核当前值与所选区域可用性。

### 1.3 文件系统设计(之前未讨论,本次补充)

**POC 决策:磁盘先用本地文件系统(ext4),不引入 JuiceFS/S3。**
本阶段目标是验证 Claude Code 本身在 microVM 内能否原生跑通(H1),文件系统是干扰变量 —— 先用最简单、行为最稳定的本地 ext4 把 agent 跑顺,排除一切分布式存储的不确定性。JuiceFS + S3 作为**后续可选验证项**(见下方 H2),客户现状(Fly.io + JuiceFS + S3)留待 H1 通过后再对齐。

**POC 第一选择:全本地 ext4(rootfs + workspace 都在本地盘)**

```
┌─ microVM ──────────────────────────────┐
│  /            ← OS rootfs(本地 ext4)               │
│               含 Node + Claude Code + 工具链        │
│  /workspace   ← 本地 ext4 目录(POC 阶段)           │
│               用户项目/数据,直接落在 rootfs 盘上     │
└─────────────────────────────────────────┘
        宿主块存储:c6g.metal 无本地 NVMe → 用 EBS gp3
```

- **rootfs 与 workspace 都放本地 ext4**:Claude Code 二进制、Node、编译器、用户项目全在本地盘 —— 启动快、I/O 行为与裸机一致、无外部依赖。
- 宿主块存储:`c6g.metal` 无本地 NVMe,POC 用 **EBS gp3**(文档 Phase 0 已挂 200 GiB)即可;若需更高 IOPS / 本地盘,可换 `i` 系列 .metal。
- microVM 自带完整内核,本地 ext4 上的 inotify、ulimit、fork/exec 等行为天然与裸机一致,不会出现普通容器的偏差。

**H2(后续可选)—— 对齐客户 JuiceFS + S3 架构:**
确认 H1 通过后,再把 `/workspace` 切到 JuiceFS(S3 后端 + Redis 元数据,FUSE 挂载),与本地 ext4 做对比验证。届时需重点关注:
1. **inotify / 文件监听在 JuiceFS(FUSE)上的行为** —— FUSE / 网络文件系统对 inotify 支持有限,而 Claude Code、dev server、`npm`/`vite`/`webpack` 等大量依赖文件监听。**这是 H2 最大的不确定点,必须实测。**
2. **重 I/O 性能**:`npm install`、`git`、编译产生海量小文件 I/O,JuiceFS over S3 延迟特征与本地盘不同,需对比基准。
3. **元数据引擎选型**:Redis 最简单;生产要评估 Redis 持久化/HA 或换 TiKV。

> 具体 JuiceFS 搭建步骤见 Phase 2(已标注为**可选**,H1 通过后再做)。

---

## 2. POC 阶段总览

| 阶段 | 目标 | 产出 | 预估时长 |
|---|---|---|---|
| Phase 0 | 账号/配额/网络准备 | VPC、配额、密钥、IAM | 0.5 天 |
| **Phase 1** | 单 .metal 裸 Firecracker + Claude Code,**全本地 ext4**(验 H1) | 能跑真实任务的 microVM + 保真度报告 ✅ 已完成 | 1–2 天 |
| Phase 2(可选) | JuiceFS + S3 文件系统(验 H2,H1 通过后再做) | workspace 落 S3,含 inotify/性能结论 ✅ 已完成 | 1 天 |
| Phase 3 | EKS + Kata 编排 + 任意端口(验 H3) | 沙盒 Pod + NLB 暴露端口 ✅ 已完成 | 2–3 天 |
| Phase 4 | 功能与保真度测试 | 测试清单结果 ✅ 已完成 | 与上重叠 |
| Phase 5 | 密度/启动/快照压测(验 H4) | 密度与成本数据 ✅ 已完成 | 1–2 天 |
| **Phase 6** | **统一控制面 v1**(生产化,见第 8 节) | DynamoDB 状态层 + FirecrackerDriver + KataDriver + Warm Pool + Machine API + Fargate 部署 | 进行中 |

---

## 3. 实施细节

> 约定:`us-east-1`、profile 默认;`<...>` 为占位符;Graviton = arm64/aarch64,镜像/二进制务必取 **arm64**。
>
> **基础设施一律用 Terraform 管理**(见 `terraform/` 目录)。AWS 侧资源(.metal 主机、IAM、安全组、ECR、EKS、NLB、ACM)由 Terraform 创建;Firecracker 安装、guest 内核、rootfs 构建、microVM 启动是**主机内**操作,仍用下面的命令。
>
> **推荐路径:** Phase 0/1 的 AWS 资源直接 `cd terraform/phase1 && terraform apply`(已封装好下面 0.1–1.1 的全部内容)。下方保留等价的 `aws` CLI 命令作为说明与 fallback。

### Phase 0 — 准备(Terraform 已封装,见 `terraform/phase1`)

```bash
# 0.1 设定变量
export AWS_REGION=us-east-1
export AZ=us-east-1a
export KEY_NAME=claude-sbx-poc
export METAL_TYPE=c6g.metal          # POC 实测机型;更新代可用 c7g.metal/m7g.metal

# 0.2 检查 .metal 服务配额(On-Demand 标准实例 vCPU 配额,代码 L-1216C47A)
aws service-quotas get-service-quota \
  --service-code ec2 --quota-code L-1216C47A --region $AWS_REGION \
  --query 'Quota.Value'
# c6g.metal=64 vCPU。若配额不足,提 quota increase(可能需要 1–2 天审批)

# 0.3 创建密钥对
aws ec2 create-key-pair --key-name $KEY_NAME \
  --query 'KeyMaterial' --output text > ~/.ssh/$KEY_NAME.pem
chmod 400 ~/.ssh/$KEY_NAME.pem

# 0.4 VPC / 子网 / 安全组(POC 可直接用 default VPC)
export VPC_ID=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true \
  --query 'Vpcs[0].VpcId' --output text)
export SUBNET_ID=$(aws ec2 describe-subnets \
  --filters Name=vpc-id,Values=$VPC_ID Name=availability-zone,Values=$AZ \
  --query 'Subnets[0].SubnetId' --output text)

export SG_ID=$(aws ec2 create-security-group --group-name claude-sbx-sg \
  --description "Claude sandbox POC" --vpc-id $VPC_ID --query 'GroupId' --output text)
# 仅放行你的 IP 的 SSH
MYIP=$(curl -s https://checkip.amazonaws.com)
aws ec2 authorize-security-group-ingress --group-id $SG_ID \
  --protocol tcp --port 22 --cidr ${MYIP}/32
```

### Phase 1 — 单 .metal 裸 Firecracker + Claude Code(验 H1)

#### 1.1 启动 Graviton .metal 主机

```bash
# 取最新 AL2023 arm64 AMI
export AMI_ID=$(aws ssm get-parameter \
  --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64 \
  --query 'Parameter.Value' --output text)

aws ec2 run-instances --image-id $AMI_ID --instance-type $METAL_TYPE \
  --key-name $KEY_NAME --security-group-ids $SG_ID --subnet-id $SUBNET_ID \
  --block-device-mappings '[{"DeviceName":"/dev/xvda","Ebs":{"VolumeSize":200,"VolumeType":"gp3"}}]' \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=claude-sbx-host}]' \
  --query 'Instances[0].InstanceId' --output text
# 注意:.metal 实例启动需数分钟(裸金属冷启动比普通实例慢)
```

拿到公网 IP 后 SSH 进去:`ssh -i ~/.ssh/$KEY_NAME.pem ec2-user@<PUBLIC_IP>`

#### 1.2 验证 KVM 可用(关键前提)

```bash
ls -l /dev/kvm                 # 必须存在
sudo dnf install -y lscpu && lscpu | grep -i virtual
# Graviton .metal 应能看到 KVM 设备;若 /dev/kvm 不存在 → 选错实例(非 .metal)
```

#### 1.3 安装 Firecracker(aarch64)

```bash
ARCH=aarch64
VER=$(curl -s https://api.github.com/repos/firecracker-microvm/firecracker/releases/latest | grep tag_name | cut -d'"' -f4)
curl -L https://github.com/firecracker-microvm/firecracker/releases/download/${VER}/firecracker-${VER}-${ARCH}.tgz -o fc.tgz
tar -xzf fc.tgz
sudo mv release-${VER}-${ARCH}/firecracker-${VER}-${ARCH} /usr/local/bin/firecracker
firecracker --version
```

#### 1.4 准备 guest 内核(aarch64)

> ⚠️ **实测结论(2026-06-12,坐实 R3):Firecracker CI 默认内核【没有】编 FUSE。**
> guest 内 `# CONFIG_FUSE_FS is not set` → `/dev/fuse` 不存在 → JuiceFS / s3fs / mountpoint-s3
> 任何 FUSE 文件系统在 guest 内**全部挂不上**(`fusermount: fuse device not found`)。
> **所以只要 workspace 要用 JuiceFS/S3,必须自编带 FUSE 的 guest 内核**——这不是可选项。

```bash
# 方式 A(仅当 workspace 用本地 ext4、不挂任何 FUSE 时可用):CI 内核
#   curl -fL https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.10/aarch64/vmlinux-5.10.223 -o vmlinux
#   ⚠️ 此内核无 FUSE,挂不了 JuiceFS

# 方式 B(推荐,JuiceFS 场景必需):自编带 FUSE/overlay/inotify 的内核
#   bash scripts/build-fuse-kernel.sh   # 产出 /opt/sbx/vmlinux-fuse
#   实测:c6g.metal 64 核 native 编译仅几分钟;启动后 /dev/fuse 正常、JuiceFS 可挂
```

> `scripts/setup-host.sh` 默认走方式 B(自动编 FUSE 内核);设 `SKIP_FUSE_KERNEL=1` 回退方式 A。
> 复核 config:`grep -E 'CONFIG_FUSE_FS=|CONFIG_OVERLAY_FS=|CONFIG_INOTIFY_USER=' .config`(应均为 `=y`)。

#### 1.5 构建带 Claude Code 的 rootfs(arm64)

```bash
# 用 Docker 导出文件系统(Graviton 上原生拉 arm64 镜像)
sudo dnf install -y docker && sudo systemctl start docker

cat > Dockerfile.sbx <<'EOF'
FROM node:22-bookworm
RUN apt-get update && apt-get install -y \
    git build-essential python3 curl ca-certificates \
    iproute2 iputils-ping fuse3 inotify-tools strace \
 && rm -rf /var/lib/apt/lists/*
# 安装 Claude Code
RUN npm install -g @anthropic-ai/claude-code
# 简单 init:配网 + 起 shell(POC 用;生产换 systemd 或自研 init)
RUN printf '#!/bin/bash\nip link set lo up\nip addr add 172.16.0.2/30 dev eth0\nip link set eth0 up\nip route add default via 172.16.0.1\necho "nameserver 8.8.8.8" > /etc/resolv.conf\nexec /bin/bash\n' > /sbin/sbxinit \
 && chmod +x /sbin/sbxinit
EOF

sudo docker build -f Dockerfile.sbx -t claude-sbx:poc .

# 导出为 ext4
dd if=/dev/zero of=rootfs.ext4 bs=1M count=8192
mkfs.ext4 rootfs.ext4
mkdir -p /tmp/rootfs && sudo mount rootfs.ext4 /tmp/rootfs
CID=$(sudo docker create claude-sbx:poc)
sudo docker export $CID | sudo tar -C /tmp/rootfs -xf -
sudo docker rm $CID
sudo umount /tmp/rootfs
```

#### 1.6 配置 TAP 网络(Claude Code 必须能出网调 api.anthropic.com)

```bash
sudo ip tuntap add tap0 mode tap
sudo ip addr add 172.16.0.1/30 dev tap0
sudo ip link set tap0 up
# 开启转发 + NAT(eth0 换成主机主网卡名,AL2023 上常为 ens5)
HOST_IF=$(ip route | grep default | awk '{print $5}')
sudo sysctl -w net.ipv4.ip_forward=1
sudo iptables -t nat -A POSTROUTING -o $HOST_IF -j MASQUERADE
sudo iptables -A FORWARD -i tap0 -o $HOST_IF -j ACCEPT
sudo iptables -A FORWARD -i $HOST_IF -o tap0 -m state --state RELATED,ESTABLISHED -j ACCEPT
```

#### 1.7 启动 microVM

```bash
cat > vmconfig.json <<EOF
{
  "boot-source": {
    "kernel_image_path": "/home/ec2-user/vmlinux",
    "boot_args": "console=ttyS0 reboot=k panic=1 pci=off init=/sbin/sbxinit"
  },
  "drives": [{
    "drive_id": "rootfs", "path_on_host": "/home/ec2-user/rootfs.ext4",
    "is_root_device": true, "is_read_only": false
  }],
  "network-interfaces": [{
    "iface_id": "eth0", "host_dev_name": "tap0"
  }],
  "machine-config": { "vcpu_count": 2, "mem_size_mib": 4096 }
}
EOF

sudo firecracker --no-api --config-file vmconfig.json
# 进入 guest shell 后(鉴权见 1.8):
#   export CLAUDE_CODE_USE_BEDROCK=1
#   export AWS_REGION=us-east-1
#   export ANTHROPIC_MODEL='us.anthropic.claude-opus-4-8...'   # 见 1.8 表
#   export AWS_BEARER_TOKEN_BEDROCK=...   # Bedrock API key(POC)
#   claude --version  &&  claude         # 或非交互:claude -p "..."
```

#### 1.8 Claude Code 鉴权 —— POC 用 Amazon Bedrock(已确认)

POC 阶段让沙盒内的 Claude Code 走 **Amazon Bedrock**(客户生产侧用自有网关管理 key,POC 先用 Bedrock 简化)。两种鉴权,二选一:

**方式 A —— Bedrock API key(最简单,POC 首选):**
在 Bedrock 控制台生成长期 API key,作为环境变量注入 microVM:
```bash
# guest 内
export CLAUDE_CODE_USE_BEDROCK=1
export AWS_REGION=us-east-1
export AWS_BEARER_TOKEN_BEDROCK=<bedrock-api-key>
export ANTHROPIC_MODEL='us.anthropic.claude-opus-4-8...'   # 见下表
claude -p "hello"
```

**方式 B —— IAM Role(更接近生产,无长期凭据):**
给 .metal 宿主实例挂 IAM 角色(`bedrock:InvokeModel` + `bedrock:InvokeModelWithResponseStream`),通过 TAP 网络让 guest 走宿主的实例元数据 / 凭据代理;或在宿主侧跑一个轻量凭据中转。Claude Code 读取标准 AWS 凭据链(`AWS_ACCESS_KEY_ID` / `AWS_SESSION_TOKEN`),配合 `CLAUDE_CODE_USE_BEDROCK=1`。

**关键环境变量:**
| 变量 | 值 | 说明 |
|---|---|---|
| `CLAUDE_CODE_USE_BEDROCK` | `1` | 让 Claude Code 走 Bedrock 而非直连 Anthropic API |
| `AWS_REGION` | `us-east-1` | Bedrock 区域 |
| `AWS_BEARER_TOKEN_BEDROCK` | `<bedrock-api-key>` | 方式 A 的鉴权(方式 B 不用,改用 IAM 凭据链) |
| `ANTHROPIC_MODEL` | 见下 | 用 **inference profile ID**,不是裸 model ID |

**模型 ID(us-east-1,必须用跨区 inference profile,前缀 `us.`):**
- Opus:`us.anthropic.claude-opus-4-8`(以 Bedrock 控制台实际列出的为准)
- Sonnet:`us.anthropic.claude-sonnet-4-6`
> ⚠️ Bedrock 上 Anthropic 模型带 `anthropic.` 前缀,且 us-east-1 调用通常**必须**走 `us.` 开头的跨区 inference profile(直接用裸 model ID 会报 on-demand 不支持)。上线前到 Bedrock 控制台 "Model access" 开通模型、并复制准确的 inference profile ID。

**IAM 最小权限(宿主角色或 key 对应的策略):**
```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
    "Resource": "arn:aws:bedrock:*::foundation-model/anthropic.*"
  }]
}
```

**⚠️ 不可信多租户下的凭据隔离(对应 R8):**
客户租户**互不可信**,因此**绝不能**把一个共享的高权限 Bedrock key 直接放进每个沙盒的环境变量 —— 沙盒内运行的是不可信生成代码,能直接读到 env 里的 key 并盗用。生产正解:
- **凭据不进沙盒**:在**宿主侧**跑一个出口代理(egress proxy),沙盒内的 Claude Code 把请求发给代理,代理在 microVM 外注入 Bedrock 凭据再转发到 Bedrock —— 沙盒永远看不到真实 key(这正是客户"自有网关管理 key"的模式,也对应 Fly/E2B 把凭据放在 VM 外的做法);
- 或**每租户独立短期凭据**:用 STS 按租户签发权限最小、短期的 Bedrock 凭据,限流限模型。
- POC 第一轮(单租户验证 H1)可以先用方式 A 的 env key 把功能跑通;但**多租户隔离必须在 POC 第二阶段验证**,否则上生产会有 key 泄露风险。

> 至此 H1 的环境就绪。**接第 4 节的测试清单**验证 Claude Code 真实任务。

### Phase 2(可选)— JuiceFS + S3 文件系统(验 H2,**H1 通过后再做**)

> POC 第一轮跳过本阶段,workspace 直接用 Phase 1 的本地 ext4。仅当 H1 验证通过、需要对齐客户 JuiceFS + S3 架构时再执行以下步骤。

#### 2.1 创建 S3 桶 + 元数据 Redis

```bash
export SBX_BUCKET=claude-sbx-jfs-$(date +%s)
aws s3 mb s3://$SBX_BUCKET --region $AWS_REGION

# POC:在 .metal 主机上跑单机 Redis 作元数据(生产换 ElastiCache)
sudo docker run -d --name jfs-redis -p 6379:6379 redis:7
```

#### 2.2 在 rootfs 内装 JuiceFS 客户端并格式化

在 1.5 的 Dockerfile 里追加(然后重建 rootfs):
```dockerfile
RUN curl -sSL https://d.juicefs.com/install | sh -
```

进入 microVM 后(guest 内,通过 TAP 能访问主机 172.16.0.1:6379 与公网 S3):
```bash
# 首次格式化(仅一次)
juicefs format \
  --storage s3 \
  --bucket https://${SBX_BUCKET}.s3.${AWS_REGION}.amazonaws.com \
  --access-key <AK> --secret-key <SK> \
  redis://172.16.0.1:6379/1 \
  claude-sbx-fs

# 挂载到 /workspace
mkdir -p /workspace
juicefs mount redis://172.16.0.1:6379/1 /workspace -d
```

> 生产应改用 IAM Role(实例角色)而非 AK/SK,并把 Redis 换成 ElastiCache、走私有子网。

#### 2.3 验证(对比本地 ext4)
把 Phase 4 的功能测试在 `/workspace`(JuiceFS)各跑一遍,重点记录:
- `npm install` / 编译 的耗时 vs 本地;
- **文件监听是否生效**(见 4.3 的 inotify 测试,这是 H2 的关键裁决点);
- git 操作正确性。

### Phase 3 — EKS + Kata 编排 + 任意端口(验 H3)

#### 3.1 建 EKS 集群 + Graviton .metal 节点组

```bash
# 用 eksctl 最省事(需先安装 eksctl)
cat > eks-cluster.yaml <<EOF
apiVersion: eksctl.io/v1alpha5
kind: ClusterConfig
metadata:
  name: claude-sbx
  region: ${AWS_REGION}
  version: "1.31"
managedNodeGroups:
  - name: metal-arm64
    instanceType: ${METAL_TYPE}
    amiFamily: AmazonLinux2023
    desiredCapacity: 1
    minSize: 1
    maxSize: 2
    volumeSize: 200
    labels: { sandbox: "true" }
EOF
eksctl create cluster -f eks-cluster.yaml
```

#### 3.2 安装 Kata Containers(Cloud Hypervisor 后端,arm64)

```bash
# kata-deploy DaemonSet 会在节点装好 Kata 二进制并注册 containerd runtime
kubectl apply -k "github.com/kata-containers/kata-containers/tools/packaging/kata-deploy/kata-rbac/base?ref=stable-3.x"
kubectl apply -k "github.com/kata-containers/kata-containers/tools/packaging/kata-deploy/kata-deploy/base?ref=stable-3.x"
kubectl -n kube-system rollout status ds/kata-deploy

# 注册 RuntimeClass(Cloud Hypervisor 后端 = kata-clh)
kubectl apply -k "github.com/kata-containers/kata-containers/tools/packaging/kata-deploy/runtimeclasses?ref=stable-3.x"
kubectl get runtimeclass    # 应看到 kata-clh / kata-qemu 等
```

> ⚠️ 需复核:Kata 在 arm64 + Cloud Hypervisor 的当前支持矩阵,以及 AL2023 .metal 节点上 `kata-deploy` 是否需额外内核模块。若 `kata-clh` 在 arm64 有问题,POC 可回退到 `kata-qemu`(同样真 guest 内核,保真度一致,仅启动稍慢)。

#### 3.3 部署 Claude Code 沙盒 Pod + 任意端口

先把 1.5 的镜像推到 ECR:
```bash
export ACCT=$(aws sts get-caller-identity --query Account --output text)
aws ecr create-repository --repository-name claude-sbx
aws ecr get-login-password | sudo docker login --username AWS --password-stdin ${ACCT}.dkr.ecr.${AWS_REGION}.amazonaws.com
sudo docker tag claude-sbx:poc ${ACCT}.dkr.ecr.${AWS_REGION}.amazonaws.com/claude-sbx:poc
sudo docker push ${ACCT}.dkr.ecr.${AWS_REGION}.amazonaws.com/claude-sbx:poc
```

```yaml
# sandbox.yaml — 用 Kata RuntimeClass。沙盒只声明 ClusterIP Service(集群内),
# 不要用 type: LoadBalancer(否则每个沙盒建一个 NLB,上千沙盒会撞配额且极贵)。
# 外部暴露统一走 3.4 的共享 Ingress(按子域名路由)。
apiVersion: v1
kind: Pod
metadata: { name: claude-sbx-1, labels: { app: claude-sbx, sandboxId: "1" } }
spec:
  runtimeClassName: kata-clh        # ← 关键:跑在 microVM 里
  nodeSelector: { sandbox: "true" }
  containers:
  - name: agent
    image: <ACCT>.dkr.ecr.<region>.amazonaws.com/claude-sbx:poc
    command: ["sleep","infinity"]
    ports:
    - { containerPort: 8080 }       # dev server 示例
    env:
    - { name: CLAUDE_CODE_USE_BEDROCK, value: "1" }
    - { name: AWS_REGION, value: "us-east-1" }
    - { name: ANTHROPIC_MODEL, value: "us.anthropic.claude-opus-4-8" }   # inference profile ID
    # POC 单租户:env 注入 Bedrock API key。多租户(互不可信)严禁如此 —— 见 1.8 凭据隔离,
    # 生产改用宿主侧出口代理注入凭据 / IRSA 每租户短期凭据,key 不进沙盒。
    - { name: AWS_BEARER_TOKEN_BEDROCK, valueFrom: { secretKeyRef: { name: bedrock, key: apikey } } }
---
apiVersion: v1
kind: Service
metadata: { name: sbx-1 }           # 仅集群内,供 Ingress 后端引用
spec:
  type: ClusterIP                   # ← 不是 LoadBalancer
  selector: { sandboxId: "1" }
  ports: [{ port: 8080, targetPort: 8080 }]
```
```bash
kubectl create secret generic bedrock --from-literal=apikey=<bedrock-api-key>
kubectl apply -f sandbox.yaml
kubectl exec -it claude-sbx-1 -- bash   # 进沙盒跑 Claude Code
```

**验证 H3 三要素:**
- (a) 自定义镜像:✅ 用的是自建 ECR 镜像;
- (b) 任意端口:在 Pod 内 `nc -l 8080` / 起 dev server,经 3.4 的共享 Ingress 从 `8080-sbx1.<域名>` 外部可达;`runtimeClassName: kata-clh` 下 `kubectl exec` 正常(Kata 真内核,无 gVisor 的 port-forward 限制);
- (c) 24×7:Pod 长驻无 TTL,`kubectl get pod` 持续 Running 即证。

> **可选增强(降低自研成本):**上层编排可直接采用开源 `kubernetes-sigs/agent-sandbox` 的 CRD + Python SDK(`Sandbox`/`SandboxTemplate`/`SandboxWarmPool`),它能装在 EKS 上提供生命周期 API 与暖池,隔离层用上面的 Kata RuntimeClass —— 等于"GKE Agent Sandbox 的开源 API 层 + 我们的 microVM 底座"。

#### 3.4 端口暴露设计(这是唯一必须自研的网络组件)

**客户实际诉求(已确认):** 暴露的端口绝大多数是 **HTTP(80/443/8080)**,少数其他端口,且**并非每个沙盒都暴露端口**。这让方案大幅收敛——主路径只需一条 HTTP 共享代理。

**方案决策表:**

| 方案 | 可行性 | 说明 |
|---|---|---|
| 每沙盒一个 EIP | ❌ 否决 | EIP 区域默认配额仅 5 个、稀缺收费、绑不到 microVM 内部,上千并发完全不可行 |
| 每沙盒一个 NLB/LoadBalancer Service | ❌ 否决 | 上千沙盒 = 上千 NLB,撞配额且成本爆炸 |
| 共享 NLB 上端口映射(host port → sandbox) | ⚠️ 仅兜底 | 单 IP 仅 65535 端口、需客户端先查端口、体验差。只用于少数裸 TCP |
| **通配符子域名 + 共享 HTTP 代理(按 Host/SNI 路由)** | ✅ **主方案** | 一个通配符证书 + 一个共享 NLB + 一个 Ingress 代理,承载成千上万沙盒,零 per-sandbox 公网 IP。**这正是 Fly / E2B 的做法** |

**核心思想:外部不靠 IP 或外部端口区分沙盒,靠 hostname 区分;内部端口随便撞无所谓。**

```
*.sbx.example.com ─DNS─► 单个共享 NLB ─► ingress-nginx(按 Host 路由)
                                              │
        ┌────────────────┬───────────────────┴──────────────┐
   sbx-1:8080        sbx-2:8080          sbx-3:3000      ...(集群内 ClusterIP)
 8080-sbx1.sbx.…   8080-sbx2.sbx.…    3000-sbx3.sbx.…
```
沙盒 1 和 2 内部都监听 8080 也没关系——外部是 `8080-sbx1.sbx.example.com` 和 `8080-sbx2.sbx.example.com`,代理按 Host 头各自转发。命名约定参考 E2B 的 `https://<port>-<sandboxID>.e2b.app`。

**POC 落地步骤:**
```bash
# 1) 装一个全集群共享的 ingress-nginx —— 只建 1 个 NLB,所有沙盒共用
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm install ingress-nginx ingress-nginx/ingress-nginx \
  --set controller.service.annotations."service\.beta\.kubernetes\.io/aws-load-balancer-type"=nlb \
  --set controller.ingressClassResource.default=true
kubectl get svc ingress-nginx-controller   # 记下 EXTERNAL-IP(NLB DNS 名)

# 2) DNS:把通配符 *.sbx.example.com CNAME 到该 NLB 域名(Route 53)
#    POC 阶段可跳过真实 DNS,用 curl --resolve 或 Host 头直接测(见验证)
```
```yaml
# ingress.yaml — 沙盒声明暴露端口时才创建(按需,不是每个沙盒都有)
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata: { name: sbx-1-ing }
spec:
  ingressClassName: nginx
  rules:
  - host: 8080-sbx1.sbx.example.com    # 端口-沙盒ID.域名
    http:
      paths:
      - path: /
        pathType: Prefix
        backend: { service: { name: sbx-1, port: { number: 8080 } } }
```
```bash
kubectl apply -f ingress.yaml
# 验证:沙盒内起 dev server,从外部按子域名访问
kubectl exec claude-sbx-1 -- bash -c 'cd /tmp && python3 -m http.server 8080 &'
NLB=$(kubectl get svc ingress-nginx-controller -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')
curl --resolve 8080-sbx1.sbx.example.com:80:$(dig +short $NLB | head -1) \
     http://8080-sbx1.sbx.example.com/      # 应返回目录列表 → 端口暴露成功
```

**各类流量处理:**
| 流量 | 方案 |
|---|---|
| HTTP/HTTPS(主体,80/443/8080) | 通配符子域名 + ingress-nginx 按 Host 路由。**主方案,零 per-VM IP** |
| 需 TLS 的原始 TCP(客户端支持 SNI) | 同走 SNI 路由复用共享 NLB |
| 裸 TCP(无 SNI,少数) | 共享 NLB 上分配专属端口段,或端口映射兜底 |
| UDP | POC 暂不支持;确认客户是否真需要(见 R7) |

**TLS / HTTPS — 用 ACM 在 NLB 层终止(已确认):**
```bash
# 1) 申请通配符证书(DNS 验证),拿到 ARN
aws acm request-certificate --domain-name "*.sbx.example.com" \
  --validation-method DNS --query CertificateArn --output text
# 按返回的 CNAME 在 Route 53 加验证记录,等待状态 ISSUED
export ACM_ARN=arn:aws:acm:<region>:<acct>:certificate/<id>
```
```bash
# 2) 让共享 NLB 在 443 终止 TLS,后端转明文给 ingress-nginx
helm upgrade ingress-nginx ingress-nginx/ingress-nginx \
  --set controller.service.annotations."service\.beta\.kubernetes\.io/aws-load-balancer-type"=nlb \
  --set controller.service.annotations."service\.beta\.kubernetes\.io/aws-load-balancer-ssl-cert"=$ACM_ARN \
  --set controller.service.annotations."service\.beta\.kubernetes\.io/aws-load-balancer-ssl-ports"=443 \
  --set controller.service.annotations."service\.beta\.kubernetes\.io/aws-load-balancer-backend-protocol"=tcp
```
- **为什么在 NLB 终止而非 cert-manager**:ACM 证书 AWS 托管、自动续期,ingress 后面只收明文 HTTP,运维最省。
- ⚠️ **一级通配符限制**:`*.sbx.example.com` 只匹配**一层**子域名。所以命名必须是 `8080-sbx1.sbx.example.com`(端口与沙盒ID 用**连字符**拼在同一层),**不能**写成 `8080.sbx1.sbx.example.com`(两层点号,通配符不覆盖,证书会失配)。当前命名约定已满足此约束。

> **与 Fly.io 对比:** Fly 本质就是这套的成熟版——它对 HTTP/HTTPS 用 **Anycast 共享 IP + fly-proxy 按 TLS SNI / Host 路由**(所有 app 共用 80/443,无需 per-app IP),只有**裸 TCP / UDP 才需要专属 IP**($2/月专属 IPv4,IPv6 免费)。我们的 AWS 自建版就是把 fly-proxy 换成 NLB + ingress-nginx、把 Anycast 换成通配符 DNS + 共享 NLB。结论一致:**HTTP 走共享路由,只有裸 TCP/UDP 才考虑独立 IP/端口段。** (Fly 计费数字上线前请到 fly.io/docs 复核。)

---

## 4. 测试步骤(功能 + 裸机保真度)

> 在三种环境各跑一遍并对比:**(A) .metal 裸机宿主**、**(B) Firecracker/Kata microVM**、**(C) 普通容器(runc)** —— 用以量化"microVM ≈ 裸机,容器 ≠ 裸机"。

### 4.1 Claude Code 功能冒烟
```bash
# 鉴权走 Bedrock(见 1.8)
export CLAUDE_CODE_USE_BEDROCK=1
export AWS_REGION=us-east-1
export ANTHROPIC_MODEL='us.anthropic.claude-opus-4-8'   # inference profile ID
export AWS_BEARER_TOKEN_BEDROCK=<bedrock-api-key>        # 或走 IAM 凭据链
claude --version
# 非交互执行一个真实 agentic 任务
claude -p "克隆 https://github.com/sindresorhus/got,装依赖,跑测试,总结结果"
```
**通过标准:**克隆/装依赖/编译/跑测试全程无因隔离层导致的失败。

### 4.2 裸机保真度检查清单(逐项 A/B/C 对比)
```bash
nproc; cat /proc/cpuinfo | grep -c processor      # 应反映 VM 配额,非宿主全核
free -h; cat /proc/meminfo | head                 # 应反映 VM 内存,非宿主
cat /sys/fs/cgroup/memory.max 2>/dev/null          # cgroup 视图正确性
cat /proc/sys/fs/inotify/max_user_watches          # microVM 独立;容器共享宿主
ulimit -a                                          # 独立 ulimit
id; sudo whoami                                    # 完整 root
nc -l 80 &                                         # 绑低端口(容器常被 cap 限制)
```

### 4.3 文件监听(inotify)—— H2/容器差异的关键
```bash
# 在 workspace 起监听,另开终端改文件,看是否触发
inotifywait -m -r /workspace/testrepo &
touch /workspace/testrepo/file_$(date +%s)
# 进一步:跑一个真实 dev server 看热重载是否生效
cd /workspace && npx --yes create-vite@latest demo -- --template react \
  && cd demo && npm install && npm run dev &
# 修改 src 文件,确认 HMR 触发(JuiceFS 上务必重点验证)
```

### 4.4 fork/exec 与 syscall 密集(microVM vs gVisor/容器的痛点)
```bash
# fork/exec 风暴
time bash -c 'for i in $(seq 1 20000); do /bin/true; done'
# 真实编译(syscall + I/O 密集)
time bash -c 'cd /tmp && git clone --depth1 https://github.com/nodejs/node 2>/dev/null; echo done'
# io_uring 可用性(microVM 应可用;gVisor 默认不可用)
strace -e io_uring_setup -f node -e 'require("fs")' 2>&1 | grep io_uring || echo "no io_uring call"
```

### 4.5 嵌套 docker / sudo / 装包
```bash
sudo apt-get update && sudo apt-get install -y htop      # 装包
# 嵌套容器(microVM 内可行;普通容器需特权且有风险)
curl -fsSL https://get.docker.com | sh && sudo docker run --rm hello-world
```

**结果记录模板:**对 4.1–4.5 每项记录 A/B/C 的 通过/失败/耗时,产出一张"保真度对比表"作为 H1/H2 的结论证据。

---

## 5. 压力测试步骤(验 H4:密度/启动/快照)

> 目标数据:单台 `c6g.metal` 能稳定承载多少并发 Claude Code microVM、冷启动与快照恢复延迟、超售 vCPU 下的表现。这些直接喂给成本模型。

### 5.1 并发密度(按内存装箱、超售 vCPU)
```bash
# 脚本:循环启动 N 个 microVM(每个独立 tap + rootfs 副本),记录可启动上限与宿主内存
# 关键指标:
#   - 每 VM 稳态 RSS(Firecracker 开销 ~5 MiB + guest 内核 + Node/Claude Code 驻留)
#   - 单机最大并发 VM 数(受内存约束;vCPU 可超售,因 agent 多为突发/空闲)
#   - 启动到 Claude Code 可用的时间
free -h            # 每批后记录
```
**装箱估算:**单机可并发数 ≈ (主机内存 × 装箱效率) / 每沙盒驻留内存。用真实数据回填该公式。

### 5.2 启动延迟
```bash
# 冷启动:从 firecracker 进程拉起到 guest 内 init 完成
# Firecracker 标称 ~125ms 到用户代码、~150 VM/s/host;实测记录
```

### 5.3 快照 / 恢复(24×7 成本核心杠杆)
```bash
# 用 Firecracker API 模式创建快照,把空闲沙盒挂起释放内存,再恢复测延迟
# 经 vsock/API 触发:CreateSnapshot → 暂停 → 释放 → LoadSnapshot
# 记录:快照大小、保存到本地/S3 耗时、恢复到可响应耗时(目标亚秒~低秒级)
# 注意已知坑:恢复后需 NTP 重同步时钟、重建网络、丢弃旧 vsock 连接、禁止原始+克隆同跑
```

### 5.4 并发真实负载
```bash
# 在 K(如 50/100/200)个沙盒里并发跑同一个 agentic 任务(4.1 那种),
# 监控:任务成功率、p50/p95 完成时间、宿主 CPU steal、内存压力、有无 OOM
```

**压测产出:**一张"密度 × 延迟 × 成本"表 —— 例如 `m8g.metal-48xl`(192 vCPU/768 GiB)按每沙盒 1.5 GiB 驻留 + 快照空闲回收,可估算每千并发沙盒的 $/月,直接对标客户 Fly 账单。

---

## 6. Phase 6 — 统一控制面 v1(生产化)

> 本阶段在 Phase 3 的 EKS 集群基础上叠加,把 POC 阶段的两个独立 demo(`app.py` / `fc_snapshot_api.py`)
> 替换为一套统一的、后端可插拔的生产级控制面。

### 6.1 架构概览

```
┌─ EKS cluster ─────────────────────────────────────────────────────┐
│                                                                      │
│  Fargate(sandbox-system namespace)    .metal 节点组(沙盒池)          │
│  ┌────────────────────────┐           ┌──────────────────────────┐  │
│  │ sandbox-control-plane  │  HTTP     │  node-agent DaemonSet    │  │
│  │ Deployment(2 replica)  │──────────►│  (每 .metal 节点一个)    │  │
│  │  - KataDriver          │           │  - Firecracker REST      │  │
│  │  - FirecrackerDriver   │           │  - jailer/tap 管理       │  │
│  │  - WarmPool            │           │  - snapshot 本地/S3      │  │
│  │  无状态,读写 DynamoDB   │           └──────────────────────────┘  │
│  └────────────────────────┘                                          │
│                                                                      │
│  DynamoDB(状态/lease/幂等/暖池/tap_idx)                              │
│  ingress-nginx(共享 NLB,按 Host 路由)                               │
└──────────────────────────────────────────────────────────────────────┘
```

**设计原则:**
- 控制面**无状态**:所有沙盒状态写 DynamoDB,Pod 崩了重启不丢数据
- **driver 可插拔**:同一套 HTTP API,后端切换只改 `SANDBOX_DRIVER` 环境变量
- **Capability 模型**:Kata v1 的 suspend 返回 501,不假装支持
- **乐观锁**:DynamoDB 条件写替代 in-process LOCK,多 Pod 并发安全

### 6.2 核心组件

| 组件 | 文件 | 说明 |
|---|---|---|
| 统一 API | `sandbox-api/app.py` | HTTP 服务,Fly Machines 风格接口 |
| 抽象接口 | `sandbox-api/driver.py` | `SandboxDriver` Protocol + `Capabilities` |
| 状态层 | `sandbox-api/db.py` | DynamoDB CRUD / lease / 幂等 / warm pool |
| FC Driver | `sandbox-api/drivers/firecracker.py` | 调 node-agent,支持 suspend/resume |
| Kata Driver | `sandbox-api/drivers/kata.py` | kubectl + LiteLLM env,suspend → 501 |
| 暖池 | `sandbox-api/warm_pool.py` | FC: 预快照池;Kata: SandboxWarmPool CRD |
| on-host 执行手 | `node-agent/main.py` | tap/jailer/FC REST/S3 快照,替代 fc_snapshot_api.py |

### 6.3 Machine API(对齐 Fly Machines)

| 端点 | 说明 | 新增能力 |
|---|---|---|
| `POST /sandboxes` | 创建(支持 `idempotency_key`) | 幂等键防重复创建 |
| `GET /sandboxes/{id}/wait` | 等待目标状态 | 长轮询,替代客户端盲等 |
| `POST /sandboxes/{id}/suspend` | 挂起+快照(FC)/Hibernation(Kata) | 成本核心杠杆 |
| `POST /sandboxes/{id}/resume` | 从快照恢复 | FC 实测 1.2s(跨机)/7ms(同机) |
| `GET /capabilities` | 当前 driver 能力声明 | capability 模型 |
| `GET /sandboxes/{id}/locate` | 定位 VMM 进程/节点 | 调试/监控 |

### 6.4 DynamoDB 表结构

```
sandboxes          主状态表(PK: id)
  GSI: tenant_id-updated_at-index   按租户列出
  GSI: idempotency_key-index        幂等键查找
  GSI: pool_state-driver-index      暖池查询

sandbox_events     事件历史(PK: id, SK: ts,TTL 30天)

sandbox_tap_idx    tap_idx 分布式分配器(原子 ADD)
```

### 6.5 部署步骤（完整流程见 README.md 快速开始）

```bash
# Step 1: DynamoDB
cd terraform/stage1-dynamodb && terraform apply -auto-approve

# Step 2: EKS 集群
cd terraform/phase3
MY_IP=$(curl -s https://checkip.amazonaws.com)
terraform apply -auto-approve -var="endpoint_public_access_cidrs=[\"${MY_IP}/32\"]"
aws eks update-kubeconfig --name claude-sbx --region us-east-1

# Step 3: Kata 3.31（通过 helm，非 kata-deploy DaemonSet）
# kata-deploy DaemonSet 会触发 containerd 重启 → 节点 hang → EKS 替换循环
# 正确做法：helm install + 等待 rollout
cd /tmp && curl -sL https://github.com/kata-containers/kata-containers/archive/refs/tags/3.31.0.tar.gz -o kata.tar.gz
tar -xzf kata.tar.gz kata-containers-3.31.0/tools/packaging/kata-deploy/helm-chart/
helm repo add nfd https://kubernetes-sigs.github.io/node-feature-discovery/charts 2>/dev/null || true
helm repo update
helm dependency build kata-containers-3.31.0/tools/packaging/kata-deploy/helm-chart/kata-deploy/ || true
helm install kata-deploy kata-containers-3.31.0/tools/packaging/kata-deploy/helm-chart/kata-deploy --namespace kube-system
kubectl rollout status daemonset/kata-deploy -n kube-system --timeout=300s

# Step 4: ingress-nginx（必须指定 namespace）
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm install ingress-nginx ingress-nginx/ingress-nginx \
  --namespace ingress-nginx --create-namespace \
  --set controller.service.annotations."service\.beta\.kubernetes\.io/aws-load-balancer-type"=nlb \
  --set controller.ingressClassResource.default=true

# Step 5: 构建并推送镜像（arm64 机器上执行）
bash scripts/build_and_push.sh

# Step 6: 部署控制面（包含 LiteLLM + Karpenter IAM）
ACCT=$(aws sts get-caller-identity --query Account --output text)
cd terraform/stage2-control-plane && terraform init
terraform apply -auto-approve \
  -var="sandbox_image=public.ecr.aws/amazonlinux/amazonlinux:2023" \
  -var="control_plane_image=${ACCT}.dkr.ecr.us-east-1.amazonaws.com/sandbox-control-plane:latest" \
  -var="node_agent_image=${ACCT}.dkr.ecr.us-east-1.amazonaws.com/node-agent:latest" \
  -var="snapshot_s3_bucket=<your-bucket>" \
  -var="enable_fargate=false" \
  -var="create_ingress_nginx=false"

# Step 7: 手动安装 Karpenter（OCI Helm 需移除 Docker credsStore）
python3 -c "import json,pathlib; cfg=pathlib.Path.home()/'.docker/config.json'; d=json.loads(cfg.read_text()); d.pop('credsStore',None); cfg.write_text(json.dumps(d))"
CLUSTER_ENDPOINT=$(aws eks describe-cluster --name claude-sbx --query 'cluster.endpoint' --output text)
helm upgrade --install karpenter oci://public.ecr.aws/karpenter/karpenter --version 1.3.3 \
  --namespace karpenter --create-namespace \
  --set "settings.clusterName=claude-sbx" \
  --set "settings.clusterEndpoint=${CLUSTER_ENDPOINT}" \
  --set "serviceAccount.annotations.eks\.amazonaws\.com/role-arn=arn:aws:iam::${ACCT}:role/claude-sbx-karpenter" \
  --set "controller.resources.limits.memory=1Gi"
kubectl scale deployment karpenter -n karpenter --replicas=1

# Step 8: 端到端测试
kubectl rollout status deployment/sandbox-control-plane -n sandbox-system --timeout=300s
bash scripts/e2e_test.sh   # 期望: ALL TESTS PASSED
```

### 6.6 Warm Pool 机制

**FC 模式(v1 首选,成本核心杠杆):**
```
后台 loop: 预启动 N 个空白沙盒 → suspend → 快照 → 标记 warm
用户 create: claim 一个 warm 沙盒 → resume(~7ms) → 注入配置
                                   ↓ 池空时
                                   冷建(正常 boot)+ 异步补池
```

**Kata 模式:**
使用 `kubernetes-sigs/agent-sandbox` 的 `SandboxWarmPool` CRD,
controller 预热 Pod 并维持水位,`create` 通过 `SandboxClaim` 秒级绑定。

### 6.7 已完成项 vs 待完成项

**✅ 已完成（v1 控制面）：**

| 项目 | 完成状态 |
|---|---|
| LiteLLM 网关部署 | ✅ terraform/stage2-control-plane/litellm.tf，实测 Bedrock 调用通 |
| 节点 Bedrock 权限撤销 | ✅ phase3/main.tf 已移除 node_bedrock，凭据仅在 LiteLLM IRSA |
| Karpenter 双节点池 | ✅ karpenter.tf，kata-metal NodePool Ready=True，karpenter_node worker role |
| EKS Access Entry | ✅ karpenter_node role 绑定 EC2_LINUX，节点 join 正常 |
| 控制面 API 认证 | ✅ Bearer token，API_KEYS env，公开路径豁免 |
| 方案 B JuiceFS workspace | ✅ 实测：快照仅含内存(~2GB)，resume 1.16s，无 rootfs |
| diff 快照逻辑 | ✅ 首次 Full → 后续 Diff，代码已实现 |
| vsock exec 通路 | ✅ TAP SSH 优先 + vsock UDS 兜底，VM 启动时自动配 vsock 设备 |
| 冒烟测试 21/21 | ✅ |
| e2e 测试 17+/17 | ✅ Kata 15/15 + FC 全通过 + JuiceFS 方案 B 通过 |

**🔲 下一阶段：**

| 优先级 | 项目 | 说明 |
|---|---|---|
| P1 | JuiceFS FUSE kernel | 自编带 FUSE 的 guest kernel，验证 /workspace 数据真正落 S3 |
| P1 | diff 快照实测 | Full 快照 34.7s→diff 预计 <5s，需真实负载验证 |
| P1 | 请求驱动唤醒(proxy) | 挂起沙盒被流量自动拉起(scale-to-zero 闭环) |
| P1 | 可观测性 | metrics / 日志聚合 / 健康告警 |
| P2 | 多租户 NetworkPolicy | 沙盒间网络隔离 + IMDS 屏蔽 |
| P2 | jailer 生产化 | node-agent 当前 USE_BARE_FC=1，生产需开启 jailer |
| P2 | H4 真实负载密度 | 真实 Claude Code 工作集峰值测试 |

---

## 7. 成本与清理

```bash
# POC 用完务必销毁(.metal 按小时计费,较贵)
ACCT=$(aws sts get-caller-identity --query Account --output text)
S3_BUCKET="my-sandbox-snapshots-${ACCT}"

# 先删 stage2（K8s 资源/LiteLLM/Karpenter IAM）
cd terraform/stage2-control-plane && terraform destroy -auto-approve \
  -var="sandbox_image=x" \
  -var="control_plane_image=x" \
  -var="node_agent_image=x" \
  -var="snapshot_s3_bucket=${S3_BUCKET}" \
  -var="enable_fargate=false" \
  -var="create_ingress_nginx=false"

# 再删 EKS 集群（约 10 分钟）
MY_IP=$(curl -s https://checkip.amazonaws.com)
cd ../phase3 && terraform destroy -auto-approve \
  -var="endpoint_public_access_cidrs=[\"${MY_IP}/32\"]"

# 最后删 DynamoDB
cd ../stage1-dynamodb && terraform destroy -auto-approve

# 清理孤儿资源（Karpenter 可能遗留）
aws ec2 describe-network-interfaces --region us-east-1 \
  --filters Name=status,Values=available Name=tag:eks:cluster-name,Values=claude-sbx \
  --query 'NetworkInterfaces[].NetworkInterfaceId' --output text | \
  xargs -I{} aws ec2 delete-network-interface --network-interface-id {} --region us-east-1
aws s3 rb s3://${S3_BUCKET} --force --region us-east-1 2>/dev/null || true
```
- 省钱建议：Phase 1/2 在**同一台** `c6g.metal` 上做完再开 Phase 3 的 EKS；不并行开多台 .metal。
- 生产成本模型：Graviton .metal + Savings Plan/RI 覆盖稳态基线、快照回收空闲内存（方案 B 快照仅 ~2GB）、Spot 仅用于可恢复层。
- **~10000 并发粗估**：134 台 c7g.metal，Savings Plan 后约 $1,700–$4,500/月（见 POC-实测结果.md §三）。

---

## 7. 风险与待确认项(POC 前/中需拍板)

| # | 事项 | 影响 | 建议 |
|---|---|---|---|
| R1 | ~~JuiceFS 上 inotify/重 I/O~~ **已实测** | ✅ npm install 成功(慢 4.5×);方案 B 快照仅含内存(~2GB)，resume 1.16s；**仍需验：dev server HMR 大量文件持续监听（需 FUSE kernel）** |
| R2 | ~~kata-clh arm64 成熟度~~ **已实测坐实** | ✅ kata-deploy 3.31 arm64 默认未注册 clh handler → **已定案采用 kata-qemu**，三处代码已统一，不再使用 clh |
| R3 | ~~guest 内核无 FUSE~~ **已确认** | ✅ CI kernel 无 FUSE，JuiceFS mount 失败但 sbxinit 继续运行；**生产需 `scripts/build-fuse-kernel.sh` 自编内核** |
| R4 | ~~Claude Code 鉴权~~ **已实现** | ✅ LiteLLM IRSA 落地，Bedrock 凭据零进沙盒；节点 Bedrock IAM 已移除（phase3/main.tf） |
| R5 | ~~arm64 依赖~~ **已确认** | ✅ 全 arm64 clean，ECR 镜像已构建推送 |
| R6 | .metal 服务配额 | ⚠️ 生产上量前提前申请 c6g.metal / m8g.metal-48xl quota |
| R7 | ~~任意端口暴露~~ **已落地** | ✅ 通配符子域名 + 共享 NLB + ingress-nginx + 控制面 Ingress(api.<domain>) |
| R8 | ~~凭据隔离~~ **已落地** | ✅ LiteLLM IRSA；Bearer token 认证；EKS Access Entry for Karpenter node role |
| R9 | 沙盒生命周期 | ✅ 24×7 长驻验证通过；快照 suspend/resume 实测 1.16s（方案 B）/1.2s（方案 A） |

---

## 附:关键事实出处(已核实)
- Firecracker 必须 .metal:Firecracker getting-started 文档原文 "EC2 only supports KVM on .metal instance types"
- Kata Firecracker 后端无 virtio-fs/无热插拔:Kata `docs/design/virtualization.md`
- microVM 满足自定义镜像/任意端口/24×7:Firecracker network-setup / design / snapshotting 文档
- gVisor 否决理由:gVisor architecture/security/compatibility 文档(用户态重实现内核、syscall 缺口)
- 完整调研与对抗式核实见前序 workflow 报告。
