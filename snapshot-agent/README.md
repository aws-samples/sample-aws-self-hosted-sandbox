# snapshot-agent —— Kata 快照 node-agent 的"定位"环节 demo

回答:"K8s 里用 Kata 管理容器,要对某个 Pod 背后的虚拟机做快照,怎么找到它在哪台机器、哪个 VMM?"

完整背景与生产架构见上层文档 [`../Kata快照定位机制.md`](../Kata快照定位机制.md)。

## locate.py —— 由 Pod 名定位到 VMM

输入一个 Pod 名,自动走完整链路,输出它的节点、VMM 进程 PID、后端类型、快照接口、socket。

```bash
# 前提:本机 kubectl 能连集群;节点能用 SSM
python3 locate.py claude-sbx-1
```

### 实测输出(活集群)

```
[控制平面] Pod=claude-sbx-1  node=ip-10-0-102-222  uid=b4638040-...  runtimeClass=kata-qemu
[控制平面] node ... → EC2 i-0d5b5978...,下发节点侧定位...

===== 定位结果 =====
  sandbox_id   : 170c0658ae99d9add5a5b2b73c2bf0e002b25f759c48cfe4831327fa3dff1831
  VMM 进程 PID : 28649  (在 ip-10-0-102-222)
  后端类型     : qemu
  快照接口     : QMP socket (savevm/migrate)
  VM 运行时目录: /run/vc/vm/170c0658ae99.../
  socket 文件  : console.sock, pid, qmp.sock, vhost-fs.sock
```

### 它做了什么(两段逻辑)

| 段 | 在哪跑 | 做什么 |
|---|---|---|
| 控制平面侧 | 本机 | `kubectl` 拿 Pod 的 `nodeName` + `uid` + `runtimeClassName`;node→EC2 实例 |
| 节点侧 | 节点(经 SSM) | podUID→sandbox ID(crictl,兜底 kata-shim);sandbox ID→VMM 进程/后端/socket 目录 |

关联键是 **sandbox ID**:Kata shim 的 `-id`、VMM 的 `-name sandbox-<id>`、socket 目录 `/run/vc/vm/<id>/` 都用它。

### 从定位到真正快照(node-agent 的下一步,本 demo 未实现)

`locate.py` 止于"定位"。完整 node-agent 还要按 `后端类型` 调对应快照接口:
- **qemu** → 连 `qmp.sock` 发 QMP(`savevm` / migrate-to-file)
- **cloud-hypervisor** → CH HTTP API `/api/v1/vm.snapshot`
- **firecracker** → Firecracker REST `/snapshot/create`(同 `../sandbox-api/fc_snapshot_api.py`)

并处理:内存/状态文件落本地 NVMe/S3、记元数据(Pod↔快照↔节点映射)、释放 RAM、恢复时重建 VMM + 重接网络/时钟。

### 已知限制(demo)
- 用 SSM 临时下发节点脚本;生产应是常驻 **DaemonSet**(gRPC/HTTP 暴露 locate/suspend/resume)。
- crictl 按 podUID 精确匹配;若 crictl 版本字段不同会回退到"取节点上唯一 kata-shim"(仅适合少量 Kata Pod 的测试环境)。
- 需要节点 sudo(crictl/pgrep/读 /run/vc)。
