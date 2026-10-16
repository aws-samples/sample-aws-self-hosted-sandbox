# CRD 路线 A：架构、兼容性与迁移

## 结论

路线 A 不把 Firecracker microVM 改造成 Pod，也不替换现有 node-agent。

- `FirecrackerSandbox` CRD 保存生命周期期望状态。
- `firecracker-operator` 持续对账 CRD，复用现有 `FirecrackerDriver`。
- `FirecrackerDriver` 继续通过 HTTP 调用每个数据节点上的 node-agent。
- node-agent 继续直接操作 Firecracker socket、TAP、rootfs、快照和 vsock。
- DynamoDB 保留为现有 REST/Portal 的兼容投影，并继续承载幂等索引、事件、
  活跃信号、节点心跳、暖池索引、tap 分配和分布式租约。

因此现有外部 API、Portal、exec、文件传输、任意端口代理和 WebSocket 链路不需要改协议。

## 组件关系

```text
REST / Portal
      |
      v
sandbox-control-plane
  - 鉴权、租户、幂等
  - exec / files / proxy
  - 写 FirecrackerSandbox.spec.desiredState
      |
      v
Kubernetes API: FirecrackerSandbox CRD
      |
      v
firecracker-operator
  - watch + level-triggered reconcile
  - finalizer
  - operation lease renewal
  - warm pool / autosleep maintenance
  - CRD status -> DynamoDB compatibility projection
      |
      v
FirecrackerDriver -> node-agent DaemonSet -> Firecracker API
```

## CRD 状态模型

示例：

```yaml
apiVersion: sandbox.memorion.ai/v1alpha1
kind: FirecrackerSandbox
metadata:
  name: 8f21c0ab
  namespace: sandbox-system
spec:
  desiredState: Running
  operationId: 43c3...
  tenantId: tenant-a
  image: min
  cpu: 2
  memoryMiB: 2048
  pool: protected
  services:
    - port: 8080
      protocol: tcp
      autostop: true
      autostart: true
status:
  phase: running
  observedGeneration: 2
  observedOperationId: 43c3...
  node: 10.0.101.20
  guestIP: 172.18.17.2
  tapIndex: 17
  conditions:
    - type: Ready
      status: "True"
      reason: Reconciled
```

`desiredState` 只负责生命周期：

- `Running`：创建或从快照恢复。
- `Suspended + suspendReason=manual`：手动挂起，对外状态为 `suspended`。
- `Suspended + suspendReason=idle`：自动休眠，对外状态为 `slept`。
- 删除 CR：finalizer 先销毁 microVM，再删除 DynamoDB 投影和 CR。

exec、文件传输、HTTP/WebSocket 代理不是声明式生命周期操作，仍直接走现有
node-agent 通道，避免把高频流量写入 Kubernetes API。

## 兼容性不变式

1. 对外 REST 路径和响应字段不变。
2. `suspended` 不会被网关自动唤醒；只有 `slept + autostart` 会透明恢复。
3. warm pool 仍使用原有 Firecracker 快照和隐藏 DynamoDB pool 记录。
4. 自定义 rootfs、pool 放置、S3/EBS 快照字段继续透传到原响应。
5. node-agent DaemonSet、hostNetwork、KVM、TAP、vsock 和 Firecracker API 不变。
6. `CRD_CONTROL_ENABLED=0` 可把 API 回滚到原直接生命周期路径。

## 并发与故障恢复

- 每个沙盒的长操作使用 DynamoDB 租约，并在操作期间续租。
- create 在调用 node-agent 前持久化 node/tap 操作日志；重试复用同一放置。
- node-agent 的 create 对已运行的同 ID microVM 幂等。
- `operationId` 与 `observedOperationId` 防止旧请求完成状态覆盖新请求。
- finalizer 保证 CR 删除前先清理 Firecracker runtime。
- Operator 重启后通过全量 resync + watch 继续收敛。
- 旧 DynamoDB 实例会被自动创建对应 CR；处于 `running` 的实例只接管，不重建。
- `needs_reschedule` 且有可恢复快照的实例可以由 Operator 消费并恢复。

运行中节点突然消失时，历史快照可能落后于最新 guest 状态。Operator 不会偷偷加载旧
快照，否则会产生静默数据回滚；这种情况标为 `orphaned`。要实现 Spot 运行态自动恢复，
仍需在节点终止前完成“最新快照上传 + 状态 CAS + 排除 draining 节点”的中断疏散链路。
这是数据正确性边界，不应通过盲目加载旧快照绕过。

## 千级规模设计

- CRD 以 watch 为主，周期 resync 兜底，列表按 500 条分页。
- Operator 每副本使用有界 worker 数，默认 8。
- base snapshot 使用有界线程池，避免每个沙盒创建一个后台线程。
- DynamoDB 新增 `state-updated_at-index`，autosleep/maintenance 按 state Query，
  升级期间索引未就绪会临时回退旧 scan。
- API 和 Operator 使用不同 Kubernetes ServiceAccount/ClusterRole。
- 两个 Operator 副本运行；per-sandbox lease 防止重复副作用，leader 只负责
  warm pool 与 autosleep 等全局维护任务。

## 部署与验证

```bash
terraform -chdir=terraform/stage1-dynamodb apply
terraform -chdir=terraform/stage2-control-plane apply \
  -var='crd_control_enabled=true'

kubectl -n sandbox-system rollout status deployment/firecracker-operator
kubectl -n sandbox-system rollout status deployment/sandbox-control-plane
kubectl -n sandbox-system get firecrackersandboxes
kubectl -n sandbox-system describe fcsbx <sandbox-id>
```

部署顺序：

1. 先创建 DynamoDB GSI 和 CRD/RBAC/Operator。
2. Operator 自动接管旧记录并写入 status。
3. 再滚动 API Deployment，使新生命周期请求写 CRD。
4. 执行通用 E2E、warm pool E2E、autosleep E2E。

快速回滚：

```bash
terraform -chdir=terraform/stage2-control-plane apply \
  -var='crd_control_enabled=false'
```

回滚关闭 Operator 和 CRD 写路径，API 使用原直接生命周期逻辑。DynamoDB 投影一直保留，
因此不需要做业务数据反向迁移。正式回滚时不要先删除 CRD；应先关闭 CRD 控制并确认 API
已滚动完成，避免 finalizer 与旧路径同时操作 runtime。
