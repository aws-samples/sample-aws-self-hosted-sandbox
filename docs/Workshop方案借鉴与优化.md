# FlexAI Workshop 方案借鉴与优化 —— 含实现方式

> 📌 **历史存档**:本文借鉴点部分基于当时的 Kata + `k8s/sandbox.yaml` 实现,而 **Kata driver 已从项目移除**
> (当前为裸 Firecracker 单一后端)。借鉴思路(凭据隔离/节点自动化/生命周期等)仍有参考价值,但涉及 Kata/`k8s/sandbox.yaml` 的具体代码已不存在。仅作历史参考。

> 来源:AWS Workshop《FlexAI Workshop: 基于 EKS & Graviton 部署 OpenClaw & Hermes 智能助理》
> (`afba7f08-c987-40dc-afa5-da3e200ae7c5`)
> 对照对象:本仓库 `POC-技术文档.md` 的 Claude Code 沙盒方案
> 整理日期:2026-06-12

---

## 0. 两套方案的定位差异(先读这段,决定哪些能抄)

| | 本方案(我们) | Workshop |
|---|---|---|
| Agent 类型 | **Claude Code**:重 I/O、fork/exec 密集、文件监听重、24×7 长驻 | **OpenClaw / Hermes**:会话式 IM 助理,轻、短会话、CPU 需求低 |
| 关注主线 | 裸机**保真度** + 存储(JuiceFS/inotify) + 密度成本 | 多租户**编排/安全/运维**的工程化 |
| 成熟度 | POC | 已有生产实践(OpenClaw.rocks SaaS) |

**结论:** Workshop 在**编排层、凭据隔离、节点自动化、生命周期、备份**这些工程实践上很成熟,值得借鉴;但它**没碰到我们最在意的存储/保真度主线**(它的 agent 轻、短会话),所以存储相关的不能照搬,要先用我们自己的 inotify/重 I/O 测试压过再说。

本文档收录 **6 个可借鉴点**,每个都给出贴合我们现有代码(`terraform/phase3/main.tf`、`k8s/sandbox.yaml`、`sandbox-api/app.py`)的实现方式。
> (已按需求排除"出站防火墙"与"安全验证清单"两项。)

---

## 1. ⭐ LiteLLM 统一 AI 网关 —— 落地 R8 凭据隔离

### 借鉴点
Workshop 里沙盒 **不直接访问 Bedrock**,而是统一走集群内的 LiteLLM 代理:

```
Sandbox Pod → http://litellm.litellm.svc.cluster.local:4000 → AWS Bedrock
```

LiteLLM 统一管理:模型路由、API Key、用量统计、多模型切换。沙盒内只有一个**集群内地址**,**永远看不到真实 Bedrock 凭据**。

### 为什么对我们重要
这正是我们 `POC-技术文档.md` 1.8 / R8 反复强调的"**凭据绝不能进沙盒**、宿主侧出口代理注入凭据"——Workshop 用一个**成熟开源组件**就落地了,比我们规划的"自研 egress proxy / 每租户 STS 短期凭据"省事得多,且天然带多租户限流和用量计费。

### 当前代码的问题
现在 `k8s/sandbox.yaml` 和 `sandbox-api/app.py` 把 Bedrock 访问直接交给沙盒:
```yaml
env:
- { name: CLAUDE_CODE_USE_BEDROCK, value: "1" }
- { name: AWS_REGION, value: "us-east-1" }
- { name: ANTHROPIC_MODEL, value: "us.anthropic.claude-opus-4-8" }
# 节点已挂 Bedrock IAM 角色,沙盒走节点凭据链 —— 多租户下这是 key/权限泄露面
```
沙盒内的不可信代码可以直接用节点凭据链调 Bedrock(甚至探测节点角色的其他权限)。

### 实现方式

**(1) 在集群装 LiteLLM(单独 namespace),由它持有 Bedrock 凭据**

LiteLLM 通过 IRSA / Pod Identity 拿 Bedrock 权限(凭据只在 LiteLLM Pod,不在沙盒)。`config.yaml` 示例:
```yaml
# litellm-config.yaml (放进 ConfigMap)
model_list:
  - model_name: claude-opus-4-8
    litellm_params:
      model: bedrock/us.anthropic.claude-opus-4-8
      aws_region_name: us-east-1
  - model_name: claude-sonnet-4-6
    litellm_params:
      model: bedrock/us.anthropic.claude-sonnet-4-6
      aws_region_name: us-east-1
litellm_settings:
  # 每个租户一个 virtual key,便于限流和用量统计
  master_key: os.environ/LITELLM_MASTER_KEY
```
Deployment 用 ServiceAccount 绑 IRSA(把现在 `terraform/phase3/main.tf` 里加在**节点角色**上的 `aws_iam_role_policy.node_bedrock` 移到 **LiteLLM 的 IRSA 角色**上,并从节点角色移除)。

**(2) 沙盒改成走 LiteLLM,环境变量不再含任何 AWS 凭据**

`k8s/sandbox.yaml` 与 `sandbox-api/app.py` 的 `sandbox_manifest()` 把 env 改为:
```yaml
env:
- { name: ANTHROPIC_BASE_URL, value: "http://litellm.litellm.svc.cluster.local:4000" }
- { name: ANTHROPIC_AUTH_TOKEN, valueFrom: { secretKeyRef: { name: sbx-llm-key, key: token } } }  # 每租户 virtual key
- { name: ANTHROPIC_MODEL, value: "claude-opus-4-8" }
# 注意:不再有 CLAUDE_CODE_USE_BEDROCK / AWS_REGION / 节点凭据链
```
> Claude Code 支持 `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` 指向兼容 Anthropic API 的网关。LiteLLM 提供 Anthropic 兼容端点(`/v1/messages`)。上线前用 `claude -p "hello"` 实测一次该路径连通性与流式是否正常。

**(3) Terraform 改动要点**
- 从 `aws_iam_role_policy.node_bedrock`(节点角色)→ 迁到 LiteLLM 的 IRSA role。
- 节点角色去掉 Bedrock 权限后,沙盒即使逃逸到 Pod 层也拿不到 Bedrock 凭据。

### 收益
- ✅ 凭据零进沙盒(实现 R8)
- ✅ 多租户限流/用量计费(LiteLLM virtual key per tenant)
- ✅ 模型热切换不动沙盒
- ⚠️ 多一跳网络(集群内,延迟可忽略);LiteLLM 自身需要 HA(多副本)

---

## 2. ⭐ Karpenter 自动管理 .metal 节点池 —— 生产化蓝本

### 借鉴点
Workshop 用 Karpenter 管两个节点池,而不是固定的托管节点组:

| 节点池 | 用途 | 配置 |
|---|---|---|
| `standard-arm64` | 通用负载(控制器、LiteLLM、ingress) | m6g/m7g、On-Demand、AMI=AL2023 ARM64、空闲 1 分钟整合 |
| `kata-metal` | Kata microVM 沙盒 | **c6g.metal**、**Taint `kata-dedicated=true:NoSchedule`**、Ubuntu 24.04 ARM64、空闲 30 分钟整合、**UserData 自动配 devmapper thin-pool + containerd Kata 运行时** |

关键洞察:**.metal 节点上跑 Kata 最麻烦的"devmapper thin-pool + containerd kata runtime"配置,被 Karpenter EC2NodeClass 的 UserData 自动化了**;沙盒和普通负载靠 taint/toleration 分池调度。

### 当前代码的问题
`terraform/phase3/main.tf` 现在是**固定的托管节点组**(`min=1 max=2 desired=1`),手动管容量,且只有一个池(沙盒和系统组件混跑):
```hcl
eks_managed_node_groups = {
  metal_arm64 = { instance_types = [var.metal_instance_type]; min_size=1; max_size=2; ... }
}
```
.metal 按小时计费很贵,固定容量在低负载时浪费、高负载时撑不住,且没有"空闲整合"省钱。

### 实现方式

**(1) 双节点池结构**(用 Karpenter NodePool + EC2NodeClass)

- 系统池 `standard-arm64`:跑 LiteLLM、ingress-nginx、sandbox-api、Karpenter 自身。
- 沙盒池 `kata-metal`:只跑带 `runtimeClassName: kata-*` 的沙盒 Pod。

`kata-metal` NodePool 关键字段:
```yaml
apiVersion: karpenter.sh/v1
kind: NodePool
metadata: { name: kata-metal }
spec:
  template:
    spec:
      requirements:
        - { key: node.kubernetes.io/instance-type, operator: In, values: ["c6g.metal"] }
        - { key: kubernetes.io/arch, operator: In, values: ["arm64"] }
      taints:
        - { key: kata-dedicated, value: "true", effect: NoSchedule }  # 隔离调度
      nodeClassRef: { name: kata-metal }
  disruption:
    consolidationPolicy: WhenEmpty
    consolidateAfter: 30m        # 空闲 30 分钟整合(.metal 贵,但沙盒长驻,别太激进)
```

**(2) EC2NodeClass 的 UserData 自动装 Kata**(这是最值钱的一段)— ✅ **已落地为方案 A**
把"手动 `kata-deploy` + 注册 RuntimeClass"前移到节点启动的 UserData,实现节点起来即带 Kata。

> ✅ 此条已实测落地并成为最终方案("方案 A")。背景:照 README 早期的 `kata-deploy` DaemonSet 在
> c6g.metal 上会让节点 hang ~12 分钟 + ASG 替换循环(根因见 `部署验证日志-2026-06-14.md`);改为
> UserData **bootstrap 阶段(kubelet 注册前)预装 kata** 后,新 c6g.metal 节点 30-60s Ready、零抖动。
> 权威 manifest 见 `README.md` Step 7,要点已固化如下注释。

```yaml
apiVersion: karpenter.k8s.aws/v1
kind: EC2NodeClass
metadata: { name: kata-metal }
spec:
  amiSelectorTerms: [{ alias: al2023@latest }]   # 最终用 AL2023 arm64
  role: claude-sbx-karpenter-node
  blockDeviceMappings:
    - deviceName: /dev/xvda
      ebs: { volumeSize: 200Gi, volumeType: gp3 }
  userData: |
    #!/bin/bash
    # 实测要点(踩坑后固化):
    # 1) 下载 kata-static-3.31.0-arm64.tar.zst(是 .zst 不是 .xz)→ tar --use-compress-program=unzstd -xf ... -C /
    # 2) 写 containerd v2 drop-in: [plugins."io.containerd.cri.v1.runtime".containerd.runtimes.kata-qemu]
    #    ConfigPath=/opt/kata/share/defaults/kata-containers/configuration-qemu.toml
    # 3) base config.toml 加 imports → 指向 drop-in;然后 systemctl restart containerd
    # 4) NodePool labels 须含 katacontainers.io/kata-runtime=true,否则 Karpenter 拒绝起节点
```
> qemu 系列在 arm64 开箱可用;clh 需在 drop-in 里额外注册 kata-clh handler(默认未注册)。

**(3) 沙盒 Pod 加 toleration**
`k8s/sandbox.yaml` 和 `app.py` 的 manifest 现在只有 `nodeSelector: { sandbox: "true" }`,改为同时容忍 taint:
```yaml
spec:
  runtimeClassName: kata-qemu          # 已定案 kata-qemu(clh arm64 默认未注册)
  nodeSelector: { sandbox: "true" }    # 仍可保留,或改用 karpenter.sh/nodepool
  tolerations:
    - { key: kata-dedicated, operator: Exists, effect: NoSchedule }
```
> 实测 `app.py` / 控制面 KataDriver 创建 sandbox pod 已用 `runtimeClassName: kata-qemu` +
> `nodeSelector: sandbox=true` + `kata-dedicated` toleration,与本条一致。

### 收益
- ✅ .metal 按需扩缩,空闲整合省钱
- ✅ Kata 节点配置自动化(devmapper/containerd 无需手动)
- ✅ 沙盒与系统组件分池,互不抢占
- ⚠️ 引入 Karpenter 运维复杂度;UserData 脚本要针对 arm64 + 我们选的 Kata 后端(clh/qemu/fc)固化测试

---

## 3. ⭐ Agent Sandbox(WarmPool + Hibernation)—— 命中 H4 启动与成本

### 借鉴点
Workshop 真正落地了 `kubernetes-sigs/agent-sandbox`(我们文档 3.3 只是"可选增强"提了一句),揭示两个直击我们痛点的能力:

| CRD | 作用 |
|---|---|
| `Sandbox` | 一个隔离运行环境(Pod + PVC + 稳定 hostname) |
| `SandboxTemplate` | 标准配置模板,可复用 |
| `SandboxClaim` | 用户请求一个 Sandbox(类似 PVC 之于 PV) |
| `SandboxWarmPool` | **预创建池,秒级分配,消除冷启动** |

- **WarmPool**:预热 N 个沙盒,用户来了秒级分配 —— 对应我们 Phase 5 "启动延迟"。
- **Hibernation(深度休眠 + 自动唤醒)**:空闲沙盒休眠省内存/成本 —— 对应我们 R9 / Phase 5 想用 Firecracker 快照挂起达到的目标,但这是 **K8s 原生 CRD 能力**,比我们经 vsock/API 自己做 `CreateSnapshot→暂停→释放→LoadSnapshot` 简单得多。

### 与我们现有 `sandbox-api/app.py` 的关系
现在 `app.py` 是我们**自研的最小控制平面**(用 kubectl 拼 Pod+Service+Ingress)。它的注释已经写明:
> "无 suspend/resume —— Kata 的 VMM(qmp.sock)被 kata-runtime 独占,外部不能直连做快照"

这恰好是 Agent Sandbox 能补的洞:**Hibernation 由 controller 在 K8s 层做,不需要我们去直连 VMM**。

### 实现方式(渐进式,不推翻现有 app.py)

**阶段 A —— 保持 app.py,先验证 Agent Sandbox controller**
1. 装 controller:`kubectl apply` agent-sandbox 的 CRD + controller(跑在 `standard-arm64` 系统池)。
2. 写一个 `SandboxTemplate`,把现在 `sandbox_manifest()` 里的 Pod spec(runtimeClass、image、env、resources)搬进去:
```yaml
apiVersion: agents.x-k8s.io/v1alpha1
kind: SandboxTemplate
metadata: { name: claude-sbx }
spec:
  podTemplate:
    spec:
      runtimeClassName: kata-clh
      tolerations: [{ key: kata-dedicated, operator: Exists, effect: NoSchedule }]
      containers:
      - name: agent
        image: <ACCT>.dkr.ecr.<region>.amazonaws.com/claude-sbx:poc
        env:   # 走 LiteLLM(见第 1 节),不放 AWS 凭据
        - { name: ANTHROPIC_BASE_URL, value: "http://litellm.litellm.svc.cluster.local:4000" }
```

**阶段 B —— 暖池 + 休眠**
3. 建 `SandboxWarmPool`,预热若干沙盒:
```yaml
apiVersion: agents.x-k8s.io/v1alpha1
kind: SandboxWarmPool
metadata: { name: claude-pool }
spec:
  template: { name: claude-sbx }
  replicas: 5          # 预热 5 个,按压测密度调
```
4. 空闲沙盒走 Hibernation(controller 配置休眠超时)。

**阶段 C —— app.py 改为调 CRD,而非直接 kubectl 拼 Pod**
把 `app.py` 的 `POST /sandboxes` 从"`kubectl apply` 三件套"改为"创建一个 `SandboxClaim`",由 controller 从 WarmPool 分配:
```python
# 现在:kubectl(["apply","-f","-"], stdin=sandbox_manifest(sid))
# 改为:创建 SandboxClaim,controller 秒级绑定 WarmPool 里的热沙盒
```
> 仍保留我们的 Ingress/子域名路由逻辑(Agent Sandbox 不管外部路由,见下方"需自建"清单)。

### ⚠️ 它需要我们自建的部分(Workshop "对比与选型"页明确列出)
Agent Sandbox 是**通用底座**,以下要自己做(我们大多已有):
- RBAC / NetworkPolicy(租户隔离)
- Ingress 动态路由(我们的通配符子域名方案,已有,继续用)
- 租户 API / 计费 / 管理后台
- Config / Secret 挂载

### 收益
- ✅ 秒级分配(WarmPool),消除 .metal + Kata 的冷启动等待(Workshop 实测 kata 冷启 30–90s)
- ✅ 空闲休眠省成本,且不用自研快照挂起
- ✅ 复用社区维护的生命周期能力,减少自研控制平面
- ⚠️ Hibernation 对 Claude Code 的长任务/文件监听状态是否安全,需实测(它为会话式 agent 设计)

---

## 4. SecretRef + Pod Identity —— 沙盒内 secret 不落配置文件

### 借鉴点
Workshop 的 OpenClaw 用 **SecretRef** 机制:配置文件 `openclaw.json` 里不写明文凭据,运行时从外部源(env / file / **exec**)解析。生产用 `exec` provider 调 AWS Secrets Manager,凭据注入靠 **Pod Identity**(`AWS_CONTAINER_CREDENTIALS_FULL_URI`)。

配套工程细节值得参考:
- **急切解析**:Gateway 启动时一次性解析所有活跃 SecretRef,后续请求读内存快照,无每请求延迟。
- **原子快照 + last-known-good**:全部成功才原子替换;任一失败保留上次的好快照,外部 secret 源临时挂了也能继续服务。
- **快速失败**:启动时活跃凭据解析不了就拒绝启动,避免半残运行。
- **活跃表面过滤**:只校验当前启用功能用到的凭据,未启用功能的 SecretRef 不阻塞启动。

### 对我们的意义
我们走 LiteLLM 后,模型凭据已不进沙盒(第 1 节);但沙盒内若还需要别的 secret(如用户的 git token、第三方 API key),这套"**凭据不落配置文件、运行时从 Secrets Manager 拉、Pod Identity 注入**"的模式直接适用,且 last-known-good 降级思路对任何 secret 加载都值得抄。

### 实现方式
1. 给沙盒 Pod 的 ServiceAccount 绑 **EKS Pod Identity**(或 IRSA),最小权限读指定 Secrets Manager 路径(按租户隔离前缀)。
2. 沙盒内需要 secret 时,用 `exec` 方式拉(示意):
```bash
aws secretsmanager get-secret-value --secret-id tenant/<id>/git-token --query SecretString --output text
```
3. 控制平面侧(`app.py` 创建沙盒时)给 Pod 注入 ServiceAccount 名,而非把 secret 塞进 env。
4. 借鉴"启动时急切解析 + 原子快照 + 快速失败"的加载策略,避免运行中因 secret 源抖动而认证失败。

### 收益
- ✅ 沙盒内不可信代码读不到明文 secret(只有受 Pod Identity 限权的临时凭据)
- ✅ secret 轮转不动沙盒配置
- ⚠️ Pod Identity 权限要按租户严格收窄,否则 A 租户能读 B 租户的 secret

---

## 5. rclone 增量同步备份 —— 低成本状态持久化

### 借鉴点
Workshop 的 Operator 用 **rclone 增量同步 PVC → S3** 做备份:
- 增量同步到固定 `latest/` 路径(只传变更文件)
- 每日快照:`latest` → `snapshots/YYYY-MM-DD`
- `retentionDays` 自动清理过期快照
- **删除实例 / 升级前自动触发一次备份**

S3 路径结构:
```
backups/<tenantId>/<instanceName>/periodic/latest/              # 增量同步
backups/<tenantId>/<instanceName>/periodic/snapshots/2026-03-24/ # 每日快照(自动清理)
```
认证:即使用 Pod Identity 也需一个 `s3-backup-credentials` Secret,但省略 AK/SK 后 rclone 自动走 AWS 原生凭据链(Pod Identity / IRSA)。

### 对我们的意义
对照我们的 `沙盒状态存储设计.md`:那篇讲的是"**运行时状态/业务元数据**该不该进数据库"(结论:Kata 路线用 etcd + labels/annotations,先不建 DB)。本节是**另一个维度——沙盒 workspace 的数据持久化/备份**,两者互补:
- 元数据 → etcd / labels(已有结论)
- **workspace 文件数据 → rclone 增量同步到 S3**(本节补充)

这比"自研快照挂起"轻得多,适合做 24×7 长驻沙盒的**数据兜底**(节点故障/Pod 重建后能恢复 workspace),也契合客户现有 S3 习惯。

### 实现方式
1. 沙盒 workspace 用 PVC(EFS 或 EBS)。
2. 起一个 CronJob(每租户或全局),容器内跑 rclone:
```bash
rclone sync /workspace s3:<bucket>/backups/<tenant>/<sandboxId>/latest \
  --s3-provider AWS --s3-env-auth   # 走 Pod Identity/IRSA 凭据链
```
3. 每日 copy 一份到 `snapshots/$(date +%F)`,并按 `retentionDays` 清理。
4. 在 `app.py` 的 `DELETE /sandboxes/{id}` 里,删 Pod 前先触发一次同步(对应 Workshop "删除前自动备份")。
5. 恢复:新沙盒起来后 `rclone sync` 反向拉回 `/workspace`。

### 收益
- ✅ workspace 数据兜底,低成本(增量 + 生命周期清理)
- ✅ 复用 S3 + Pod Identity,无新组件
- ⚠️ 这是**数据备份**,不是"内存级挂起恢复"(那是第 3 节 Hibernation / 或裸 FC 快照的范畴),两者用途不同

---

## 6. EFS 作为 workspace 存储候选(谨慎,需实测)

### 借鉴点
Workshop 用 **EFS Access Point** 做持久化:动态供给、ReadWriteMany(RWX)、按租户目录隔离、静态加密 + 传输加密(TLS)。EFS 是托管服务,运维几乎为零。

### 对我们的意义 —— ⚠️ 不能盲抄
我们文档 H2 / R1 的核心担忧是:**JuiceFS(FUSE)上 inotify / 文件监听 / 重 I/O 能否撑住 Claude Code**。EFS 是 NFS,在这两点上和本地盘差异**更大**:
- inotify 在 NFS 上**不可靠**(跨客户端的文件变更不一定触发本地 inotify) —— 直接威胁 dev server HMR、`vite`/`webpack` 监听。
- 海量小文件 I/O(`npm install`、`git`、编译)在 EFS 上延迟特征和本地盘差很多。

Workshop 没踩到这些坑,**因为它的 agent 是会话式轻负载,不跑这些重活**。

### 实现方式(仅作 H2 的对比候选)
把 EFS 列为我们 Phase 2 文件系统对比的**第三个候选**(本地 ext4 / JuiceFS+S3 / EFS),用我们文档 **4.3 的 inotify 测试 + 4.4 的 fork/exec & I/O 测试**三者同台对比:
```bash
# 在 EFS 挂载的 /workspace 上跑:
inotifywait -m -r /workspace/testrepo &   # 改文件看是否触发(关键裁决点)
cd /workspace && npm run dev               # 看 HMR 是否生效
time bash -c 'cd /workspace && git clone ... && npm install'   # 重 I/O 耗时
```

### 结论
- ✅ 若只需持久化、不依赖 inotify 的场景,EFS 运维最省
- ❌ **在 inotify/HMR/重 I/O 通过我们的实测前,不能选 EFS 做 Claude Code 的 workspace**;大概率仍需本地盘(workspace 落本地 + 第 5 节 rclone 异步同步 S3)

---

## 7. 相同 / 重合的点(互为印证)

两套方案在底层判断高度一致:

| 维度 | 我们 | Workshop |
|---|---|---|
| 隔离技术 | Kata Containers,VM 级隔离 | 一致 |
| Hypervisor | Firecracker(+可选 Kata 后端) | 一致(~125ms 启动,~5MB 开销) |
| 硬件平台 | Graviton ARM64 + `.metal`(KVM 前提) | 一致(c6g.metal;理由也相同:agent I/O 密集非 CPU 密集) |
| 编排 | EKS + Kata RuntimeClass | 一致 |
| 运行时切换 | `runtimeClassName` | 一致(kata-fc / kata-qemu) |
| 模型后端 | Amazon Bedrock | 一致 |
| 多租户立场 | 互不可信 → 必须 microVM、凭据不进沙盒、控爆炸半径 | 完全一致 |
| 通用底座 | 提到 `kubernetes-sigs/agent-sandbox` | 一致(且已落地) |
| 自定义 Guest 内核 | 强调自编固化 config | 一致(自编 6.18.x) |

**几处分歧:**
- **Kata 后端**:我们倾向 `kata-clh`(有 virtio-fs/热插拔);Workshop 生产用 `kata-fc` + `kata-qemu`。他们的实践印证 **kata-fc 在 EKS 生产可用**(我们 R2 的旁证),`kata-qemu` 是稳妥兜底。
- **任意端口暴露**:**我们更强**——通配符子域名 + 共享 NLB + ingress-nginx 解决了"任意端口";Workshop 只有 HTTP via ALB,无此能力。这部分不用借鉴。
- **入口层**:Workshop 多一层 CloudFront(CDN + WAF 挂载点);我们直接 NLB。是否加取决于是否需要全球加速 / WAF。

---

## 8. 落地优先级建议

| 优先级 | 借鉴点 | 理由 | 难度 |
|---|---|---|---|
| P0 | **① LiteLLM 网关** | 直接落地 R8 凭据隔离,补当前最大安全短板 | 中 |
| P0 | **② Karpenter 管 .metal** | 生产化必经,且自动化 Kata 节点配置 | 中高 |
| P1 | **③ Agent Sandbox(WarmPool/Hibernation)** | 解决冷启动 + 空闲成本,替代自研快照挂起 | 高 |
| P1 | **⑤ rclone 增量备份** | workspace 数据兜底,低成本现成模式 | 低 |
| P2 | **④ SecretRef + Pod Identity** | 沙盒内其他 secret 的隔离(走 LiteLLM 后优先级降低) | 中 |
| P2 | **⑥ EFS 候选** | 仅作 H2 对比候选,**必须先过 inotify/重 I/O 实测** | 低(评估) |

**一句话:** 最该立刻吸收的是 **① LiteLLM(凭据隔离)** 和 **② Karpenter 管 .metal(含 Kata 自动化)**——补上生产化路径的现成短板;**③ WarmPool/Hibernation** 在 Phase 5 认真评估能否替代自研快照;**⑤ rclone** 顺手就能上;**⑥ EFS 不要盲抄**,没经过 Claude Code 重 I/O / inotify 的考验。
