# i7i x86 Firecracker 真机测试报告

> 实测日期：2026-08-10
> 区域：`us-east-1`
> 沙盒节点：`i7i.8xlarge`，`amd64`，Nitro nested virtualization
> 范围：Terraform 部署、宿主 KVM、Firecracker guest、API 生命周期、鉴权、持久状态盘与完整清理

## 结论

`i7i.8xlarge` 已通过本仓库 Firecracker 主线的真实 AWS 兼容性 E2E。Terraform
显式开启 Nitro nested virtualization，宿主获得 `/dev/kvm`，amd64 guest 完成
create、exec、suspend、resume 和 destroy。

| 验证项 | 结果 | 证据 |
|---|---|---|
| i7i EKS 节点 | PASS | `i7i.8xlarge` 在 `us-east-1b` Ready |
| Nested virtualization | PASS | 宿主 `/dev/kvm` 存在 |
| Firecracker | PASS | `v1.16.1` |
| amd64 guest | PASS | `x86_64`，kernel `5.10.223` |
| 生命周期 E2E | PASS | create/exec/suspend/resume/destroy |
| Bearer 鉴权 | PASS | 无 key 被拒绝，正确 key 放行 |
| 本地回归 | PASS | `sandbox-api/smoke_test.py` 48/48 |
| 资源清理 | PASS | AWS 测试资源残留为空 |

本报告证明兼容性和功能正确性，不代表 i7i 与 `c6g.metal` 的性能对比。冷启动、
Full/Diff 快照吞吐、密度和本地 NVMe 缓存收益仍需单独 benchmark。

## 测试配置

```text
node_arch              = amd64
sandbox_instance_type = i7i.8xlarge
sandbox_az_index       = 1
region                 = us-east-1
state EBS              = 400 GiB gp3
rootfs platform        = linux/amd64
```

`us-east-1a` 当时返回 `InsufficientInstanceCapacity`，AWS 建议
`us-east-1b/1c/1d/1f`。保持单 AZ EBS 约束，将节点组切到
`sandbox_az_index=1` 后成功创建。

## 宿主与存储

```bash
uname -m
test -e /dev/kvm
firecracker --version
findmnt /var/lib/sbx
lsblk -o NAME,SIZE,TYPE,MOUNTPOINTS,MODEL
```

- 宿主为 `x86_64`，`/dev/kvm` 存在。
- Firecracker 为 `v1.16.1`。
- 400 GiB Amazon EBS 按 UUID 持久挂载到 `/var/lib/sbx`。
- 两块 i7i instance store 未格式化，未被误识别为状态 EBS。

当前仍以 EBS 为权威状态盘。本次未把本地 NVMe 接入快照缓存路径。

## Guest 与 API E2E

```bash
bash scripts/e2e_test.sh \
  --driver firecracker \
  --api-url http://localhost:18000 \
  --api-key "$API_KEY"
```

脚本覆盖健康检查、create、vsock exec、suspend、resume、destroy 和 Bearer token
正反向鉴权，最终输出：

```text
ALL TESTS PASSED
```

guest 侧：

```text
uname -m  -> x86_64
uname -r  -> 5.10.223
```

## 本地回归

```bash
python3 sandbox-api/smoke_test.py
# Ran 48 tests ... OK

terraform -chdir=terraform/phase1 validate
terraform -chdir=terraform/phase3 validate
terraform -chdir=terraform/stage2-control-plane validate
terraform fmt -check -recursive terraform

bash -n scripts/build-min-rootfs.sh scripts/build-rootfs-image.sh \
  scripts/e2e_test.sh scripts/verify-x86-feasibility.sh
python3 -m py_compile node-agent/main.py sandbox-api/warm_pool.py
git diff --check
```

以上全部通过，活跃仓库内容中已无旧 x86 实例类型引用。

## 真机发现与修复

| 问题 | 处理 |
|---|---|
| 默认 AZ 无 i7i 即时容量 | 增加 `sandbox_az_index` |
| instance store 可能被误认作状态盘 | 只选择 Amazon EBS 作为状态盘 |
| AL2023 重启后挂载不稳定 | UUID + systemd mount |
| EKS module v21 基础 add-on | 显式声明 `vpc-cni`、`kube-proxy`、`coredns` |
| 并发 Firecracker API 操作冲突 | 增加 per-sandbox 操作锁 |
| 暖池快照时机过早 | vsock ready 后等待 settle 再 snapshot |
| E2E 未覆盖生产鉴权 | 增加 `--api-key` |

## 清理验证

按 `docs/deploy.md` 顺序销毁 stage2、phase3、stage1，并删除测试 ECR repository、
amd64 rootfs 和测试私钥。最终检查：

```text
EKS=[]  active_i7i=[]  VPC=[]  EBS=[]  ENI=[]
ASG=[]  LaunchTemplates=[]  DynamoDB=[]  ECR=[]  IAM=[]
```

四个 Terraform state 均为 0。GuardDuty 自动创建的 VPC endpoint 曾短暂占用私有
子网；删除 endpoint 并重跑 destroy 后，VPC 成功清空。

## 后续性能基准

- microVM create 到 vsock ready 的 P50/P95/P99。
- Full/Diff suspend 与 resume 延迟。
- EBS 路径与本地 NVMe 缓存路径吞吐。
- 单节点 50/100/200 VM 的 CPU、内存和 I/O 水位。
