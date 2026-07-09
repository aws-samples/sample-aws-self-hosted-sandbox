# Intel x86(amd64)上线 Checklist

> 📌 **历史存档**:本文含 Kata Pod / `k8s/sandbox.yaml` 等**已从项目移除**的步骤(Kata driver 已删除,
> 当前为裸 Firecracker 单一后端)。x86 架构切换本身(`node_arch=amd64`)仍有效,但涉及 Kata 的步骤已不适用。仅作历史参考。

把 sandbox 从 Graviton(arm64)切到 Intel x86(amd64)的完整步骤。设计为 `node_arch` 参数化二选一,默认机型 `c5n.metal`(最便宜的主流 Intel x86 裸金属,72 vCPU/192 GiB)。

> 背景与代码改动范围见 `terraform/README.md` 的"选择 CPU 架构"小节。本文是按顺序执行的操作手册。

---

## 阶段 0:可行性验证(真机,先于一切)

x86 链路的多数外部事实已用公开信息核实(见下"已核实"),但 `/dev/kvm` 可用性与自编 x86 FUSE 内核能否 boot **只有真机能定论**。务必先跑:

```bash
# 1. 申请 c5n.metal 配额(若没有):Service Quotas → EC2 → L-1216C47A,c5n.metal=72 vCPU
# 2. 起一台 c5n.metal(可临时用 phase1: terraform apply -var="node_arch=amd64" ...)
# 3. SSM/SSH 登录后:
sudo RUN_FUSE_KERNEL=1 bash scripts/verify-x86-feasibility.sh
```

验证项:A=`/dev/kvm`,B=Firecracker x86 二进制,C=CI 内核 boot microVM,D=自编 FUSE x86 内核 boot。
**全 PASS 才继续**;有 `[FAIL]` 看 `/tmp/x86-verify-*.log`。

### 已核实(无需真机)
- `firecracker-ci/v1.10/x86_64/vmlinux-5.10.223` 存在(与 arm64 同名同版)。
- Firecracker release 资产 `firecracker-vX.Y.Z-x86_64.tgz`(命名对称)。
- 内核 config `microvm-kernel-ci-x86_64-6.1.config` 存在。
- x86 内核镜像格式 = 未压缩 ELF `./vmlinux`(官方 docs/rootfs-and-kernel-setup.md,非 bzImage)。

---

## 阶段 1:构建并上传 x86 镜像与 rootfs

```bash
# 1a. 控制面 + node-agent 镜像(x86)
PLATFORM=linux/amd64 bash scripts/build_and_push.sh --region us-east-1 --cluster claude-sbx
#  也可一次构多架构 manifest list(需 buildx):
#  PLATFORM=linux/arm64,linux/amd64 bash scripts/build_and_push.sh ...

# 1b. 沙盒 rootfs(x86):在阶段0那台 c5n.metal 上产出
sudo ARCH=x86_64 bash scripts/setup-host.sh           # 产出 /opt/sbx/rootfs.ext4 + vmlinux-fuse
#  然后把 rootfs 打包上传到 phase3 UserData 约定的 S3 key:
cd /opt/sbx && tar -czf rootfs-juicefs-x86_64.tar.gz -C /tmp/rootfs_mount .   # 或按 setup-host 实际产物
aws s3 cp rootfs-juicefs-x86_64.tar.gz \
  s3://427169985960-23-09-05-01-18-49-bucket/rootfs/rootfs-juicefs-x86_64.tar.gz --region us-east-1
```

> ⚠️ phase3 UserData 对 amd64 拉的 key 是 `rootfs/rootfs-juicefs-x86_64.tar.gz`(见 phase3/main.tf `arch_cfg.amd64.rootfs_key`)。该桶名是当前硬编码值,换账号需同步改。

---

## 阶段 2:Terraform apply(各阶段 node_arch 必须一致)

```bash
# phase3:EKS + x86 .metal 系统节点
cd terraform/phase3
terraform apply -var="node_arch=amd64" \
  -var='endpoint_public_access_cidrs=["'$(curl -s https://checkip.amazonaws.com)'/32"]'

# stage2:控制面 + Karpenter x86 NodePool(+ 可选 JuiceFS)
cd ../stage2-control-plane
terraform apply -var="node_arch=amd64" \
  -var="control_plane_image=<ECR>/sandbox-control-plane:latest" \
  -var="node_agent_image=<ECR>/node-agent:latest" \
  -var="sandbox_image=<ECR>/claude-sbx:poc"
```

切 amd64 时自动随架构变化:AMI 类型(`AL2023_x86_64_STANDARD`)、默认机型(`c5n.metal`)、
Karpenter `kubernetes.io/arch: amd64`、Firecracker/内核下载架构、JuiceFS Redis 节点族(`t4g`→`t3`)。

---

## 阶段 3:端到端功能验证(集群内)

```bash
aws eks update-kubeconfig --name claude-sbx --region us-east-1

# 3a. 节点架构正确
kubectl get nodes -L kubernetes.io/arch,node.kubernetes.io/instance-type
#  期望:出现 amd64 + c5n.metal 节点;sandbox=true 标签在 kata-metal 节点上

# 3b. 控制面 / node-agent 镜像能拉起(无 ImagePullBackOff/Exec format error)
kubectl -n sandbox-system get pods
kubectl -n sandbox-system logs ds/node-agent | tail -20

# 3c. 起一个沙盒 Pod(Kata microVM),确认 RuntimeClass 在 x86 节点工作
envsubst < k8s/sandbox.yaml | kubectl apply -f -
kubectl wait --for=condition=Ready pod/claude-sbx-1 --timeout=180s
kubectl exec claude-sbx-1 -- uname -m          # 期望 x86_64
kubectl exec claude-sbx-1 -- claude --version  # Claude Code 可运行

# 3d. 跑仓库自带 e2e(若适用)
bash scripts/e2e_test.sh                        # 见脚本内说明
```

通过标准:节点 amd64 + 沙盒 Pod Ready + `uname -m`=x86_64 + Claude Code 可执行 + e2e 绿。

---

## 回滚

各阶段 `terraform apply`(去掉 `-var="node_arch=amd64"` 即回 arm64 默认),或 `terraform destroy`。
arm64 与 amd64 是互斥的 `node_arch` 取值,不并存。
