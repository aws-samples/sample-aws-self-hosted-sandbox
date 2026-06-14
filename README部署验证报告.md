# README 部署指引验证报告

> 验证日期：2026-06-14 ｜ 账号：427169985960 ｜ Region：us-east-1
> 验证方式：在真实 AWS 环境按 README「快速开始（Agent 部署指南）」逐步执行
> 工具版本：terraform 1.15.2 / kubectl 1.36 / helm 4.2.0 / aws-cli / docker(colima) 29.5.3

---

## 一、验证结论速览

| Step | 内容 | 结果 | 说明 |
|---|---|---|---|
| Step 0 | git clone | ✅ 通过 | 远程地址正确 |
| Step 1 | DynamoDB 3 表 | ✅ 通过 | 表名/验证命令均符 |
| Step 2 | EKS + c6g.metal | ✅ 通过 | 控制面+节点 Ready，耗时约 13 分钟 |
| Step 5 | arm64 镜像构建推送 | ✅ 通过 | control-plane + node-agent 均推送成功 |
| 本地 | smoke_test.py | ✅ 21/21 | 与 README 一致 |
| **Step 3** | **安装 Kata（kata-deploy）** | ❌ **阻断** | **照原文命令执行后节点进入 NotReady↔Ready 重启循环** |
| Step 4 | ingress-nginx | ⛔ 受阻 | admission pod 因节点不稳定无法稳定调度 |
| Step 6-9 | 控制面/Karpenter/e2e | ⛔ 未达 | 依赖稳定节点，被 Step 3 阻断 |

**核心结论：照 README 原文部署，会在 Step 3（安装 Kata）卡住。** 根因是 kata-deploy 安装末尾的 `systemctl restart containerd` 在 `c6g.metal + AL2023(containerd 2.2.3) + EKS 1.31` 上触发节点自发 Rebooted，节点 kubelet 失联约 10-15 分钟（远超 README 写的 `--timeout=300s`）后才自愈，且此后每次 Kata 相关 containerd 活动都会再次触发抖动，导致 pod 被反复驱逐，集群无法稳定承载后续工作负载。

> 注：Kata 功能本身可用（实测 kata-qemu pod 能 Running），问题在于**节点稳定性**与**文档未告知这一行为**。

---

## 二、阻断级问题（必须修复）

### P0-1. Step 3 kata-deploy 导致节点重启循环，文档完全未提示

**现象**：逐字执行 README Step 3 默认命令
```
helm install kata-deploy .../kata-deploy --namespace kube-system
kubectl rollout status daemonset/kata-deploy -n kube-system --timeout=300s   # ← 这里超时失败
kubectl get runtimeclass | grep kata-qemu                                     # ← 紧接着连不上 apiserver
```
- 安装后约 30s，c6g.metal 节点 kubelet 停止上报，节点 NotReady
- 事件日志出现 `Warning Rebooted node ... has been rebooted`（节点自发重启）
- 节点 NotReady 持续 **10-15 分钟**后自愈，`--timeout=300s` 必然超时
- 实测 4 台 c6g.metal 节点重复完全相同的故障模式（非偶发硬件问题）

**对照实测文档**：`POC-实测结果.md` 第 92 行记载，作者的 Kata 验证环境用的是 **c7g.metal**，而 README/Terraform 部署的是 **c6g.metal**。文档假设"两者结论通用"，但本次实测在 c6g.metal 上无法复现稳定的 Kata 部署。

**建议修复**：
1. 在 Step 3 显著位置加警示框：
   > ⚠️ 装完 kata-deploy 后，c6g.metal 节点会因 containerd 重启而 **NotReady 约 10-15 分钟并自发重启一次**，属已知行为。**请耐心等待自愈，切勿手动 reboot/terminate 节点**（会打断恢复）。
2. 把 `--timeout=300s` 放宽到 `--timeout=1200s`（chart 的 startupProbe 本身给了 20 分钟预算）。
3. 增加"等待节点恢复"的显式步骤：
   ```bash
   # kata-deploy 会重启 containerd，节点将 NotReady 约 10-15 分钟，等待自愈：
   kubectl wait node --all --for=condition=Ready --timeout=1200s
   ```
4. **强烈建议**：要么把 Terraform 默认机型 `metal_instance_type` 改为作者实测通过的 `c7g.metal`，要么在文档中明确标注「c6g.metal 上 kata-deploy 存在节点重启循环，推荐 c7g.metal」，并补充 containerd 重启的规避方案（如预先在节点 user_data 写好 kata 的 containerd drop-in，避免运行时 restart）。

---

## 三、命令级 Bug（会导致命令失败）

### P1-1. Step 7 中文版 KARPENTER_NODE_ROLE 写法错误（中英文不一致）

| 版本 | README 写法 | 结果 |
|---|---|---|
| 中文版（第 254 行） | `KARPENTER_NODE_ROLE="${ACCT}:role/claude-sbx-karpenter-node"` | ❌ 生成非法 role 值 |
| 英文版（第 625 行） | `KARPENTER_NODE_ROLE="claude-sbx-karpenter-node"` | ✅ 正确 |

`terraform/stage2-control-plane/karpenter.tf` 第 218/257 行确认 `EC2NodeClass.spec.role` 取的是 role **名字**（`aws_iam_role.karpenter_node.name`）。中文版会让 `role:` 字段变成 `427169985960:role/claude-sbx-karpenter-node`，EC2NodeClass 校验失败。

**建议**：中文版第 254 行改为 `KARPENTER_NODE_ROLE="claude-sbx-karpenter-node"`，与英文版一致。

---

## 四、文档准确性问题（不阻断，但误导读者）

### P2-1. 部署机型 c6g.metal 与性能/成本数据机型 c7g.metal 混用，未说明关系
README 全文 c6g.metal 出现 7 处（部署/NodePool/配额），c7g.metal 出现 9 处（性能基准/成本估算）。两者关系无说明，读者会困惑"到底用哪个"。
**建议**：明确"部署默认 c6g.metal；性能基准在 c7g.metal 实测，二者同为 Graviton 64vCPU/128GiB"，并在第 137、528 行配额说明处统一。鉴于 P0-1，建议直接统一为 c7g.metal。

### P2-2. Step 3「helm dependency build 报错可忽略」描述偏差
README 称 `helm dependency build ... || true`「报错不影响安装」。实测：只要先按 README 添加了 nfd repo，`dependency build` 其实**成功**（exit 0），并不会报错。描述偏保守，`|| true` 与"报错可忽略"的注释在正确加 nfd repo 后已无必要。
**建议**：改为「添加 nfd repo 后 dependency build 正常成功；`|| true` 仅为防御未加 repo 的情况」。

### P2-3. e2e「期望 17/17 ALL PASS」与脚本实际输出格式不符
`scripts/e2e_test.sh` 结尾输出的是 `ALL TESTS PASSED`（或 `N TEST(S) FAILED`），**不会打印"17/17"**。脚本中 pass/fail/info 三类共 19/14/30 个调用，且 T10/T11/T17 等按 driver 条件 skip。"17/17"是作者自述数字，非脚本产物。
**建议**：改为「期望结尾显示 `ALL TESTS PASSED`」，避免读者按字面找"17/17"。

### P2-4. 全新部署前未提示清理上一次残留资源
本次实测中，phase3 首次 apply 因上次 destroy 残留的 **CloudWatch Log Group `/aws/eks/claude-sbx/cluster`** 和 **ECR 仓库 `claude-sbx`** 报 `ResourceAlreadyExistsException` 而失败。这两类资源不随 `terraform destroy` 自动清除。
**建议**：在 Step 2 前或清理段补充：
```bash
# 若曾部署过同名集群，先清残留（destroy 不会删这两类）：
aws logs delete-log-group --log-group-name /aws/eks/claude-sbx/cluster --region us-east-1 2>/dev/null || true
aws ecr delete-repository --repository-name claude-sbx --force --region us-east-1 2>/dev/null || true
```

### P2-5. Step 2 节点冷启动「约 10 分钟」偏乐观
实测 EKS 控制面创建约 11 分钟，整个 phase3 apply（含节点组）约 13 分钟。
**建议**：改为「EKS 控制面约 10-12 分钟，加节点组冷启动整体约 15 分钟」。

### P2-6. Step 5 镜像构建对「无 docker 环境」缺乏可执行指引
`build_and_push.sh` 在无 docker 时直接退出并指向"方式 B SSM 构建"，但 README 与脚本注释都未给出**完整可复制的 SSM 构建命令**，只说"详见注释"。本次实测靠本地安装 colima 才完成。
**建议**：补一段无 docker 时的最小可行路径（安装 colima/finch，或给出完整 SSM remote build 命令块）。

### P2-7. phase3 自建 ECR `claude-sbx` 与 Step 5 手建仓库的关系未说明
`terraform/phase3/main.tf` 第 139 行已自动创建 ECR 仓库 `claude-sbx`（sandbox 镜像用），而 Step 5 让用户手动 `create-repository` 的是另两个仓库（`sandbox-control-plane`/`node-agent`）。三个仓库的归属/创建者未说明，易混淆。
**建议**：在 Step 5 注明「`claude-sbx` 仓库由 phase3 自动创建，本步只需建 control-plane 与 node-agent 两个」。

---

## 五、Karpenter 配置潜在问题（静态审查，未实跑到）

`terraform/stage2-control-plane/karpenter.tf` 第 126 行注释称"aws:auth ConfigMap 中需要映射此 Role（让 Karpenter 节点能 join 集群）"，但文件内**并未实际创建该 aws-auth 映射**。EKS（非 access-entry 模式）下，Karpenter 启动的节点若未在 aws-auth/access-entry 注册其 node role，将无法 join 集群。README Step 7 也未涵盖此映射。
**建议**：在 stage2 补充 `aws_eks_access_entry`（或 aws-auth ConfigMap 映射）将 `karpenter_node` role 注册为 EC2 节点；或在 README Step 7 增加该步骤。（建议实际验证后再定稿）

---

## 六、本次为复现部署对线上做的变更（已清理）

- 创建并随后 destroy：EKS 集群 claude-sbx、c6g.metal 节点组、VPC 相关、DynamoDB 3 表
- 删除上次残留：CloudWatch Log Group、ECR `claude-sbx`（空仓库）
- 推送镜像至 ECR：`sandbox-control-plane:latest`、`node-agent:latest`（保留，未删）
- 本机安装：colima + docker CLI（用于 arm64 镜像构建）
