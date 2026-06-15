# Kata-on-K8s 快照/恢复:如何定位到具体的 VMM,以及怎么做

> 场景:所有容器都在 K8s 里、用 Kata 管理。要对某个 Pod 背后的虚拟机做快照/恢复,
> 必须先回答两个问题:**它在哪台机器?在哪个 VMM 进程?** 本文讲清这条链路和生产做法。

## 一、核心结论(先看这个)

- **"在哪台机器"** = Pod 的 `nodeName`,`kubectl get pod -o wide` 直接可得。
- **"在哪个 VMM"** = 用 **sandbox ID** 作为关联键,它把 `Pod → Kata shim → VMM 进程 → 各 socket` 串在一起。
- **K8s/Kata 不直接暴露"快照这个 Pod"的标准动作** —— 这层要自研(node agent + 控制平面)。这就是"EKS+Kata 白送调度,但快照要自建"的真正含义。

## 二、定位链路(逐层下钻,已在真集群实测)

```
Pod: claude-sbx-1
 │  ① kubectl get pod claude-sbx-1 -o wide
 │     → nodeName=ip-10-0-102-222 , runtimeClass=kata-qemu , podUID=b4638040-...
 ▼
节点: ip-10-0-102-222  (EC2 实例 i-0d5b5978...)
 │  ② SSM/SSH 进节点(或在节点上的 DaemonSet 里)
 ▼
Kata shim 进程:
   /opt/kata/bin/containerd-shim-kata-v2 ... -id 170c0658ae99...
                                              └─ 这个 id = sandbox ID(关联键)
 │  ③ shim 拉起 VMM,VMM 名字/路径都带同一个 sandbox ID
 ▼
VMM 进程(本例 kata-qemu 后端 → qemu):
   qemu-system-aarch64 -name sandbox-170c0658ae99...
     -qmp unix:fd=3,server=on,wait=off          ← QMP 控制接口(发快照指令入口)
     -pidfile /run/vc/vm/170c0658ae99.../pid     ← 进程 PID
   该 VM 的所有 socket 都在: /run/vc/vm/<sandboxID>/
```

**实测命令(在节点上):**
```bash
# 由 Pod 名 → sandbox ID(任选其一)
crictl pods --name claude-sbx-1 -q                      # crictl 报的 pod(sandbox) id
pgrep -af containerd-shim-kata-v2                        # shim 命令行里的 -id <sandboxID>
# 由 sandbox ID → VMM 进程 + socket 目录
pgrep -af 'qemu-system|cloud-hypervisor|firecracker' | grep <sandboxID>
ls /run/vc/vm/<sandboxID>/                               # console.sock / qmp / pid ...
```

## 三、关键现实:快照接口因 VMM 后端而异(不统一)

VMM 是哪个,取决于 Pod 的 `runtimeClassName`(= Kata 用哪个发动机):

| RuntimeClass | VMM 进程 | 快照控制接口 | 备注 |
|---|---|---|---|
| `kata-qemu` | `qemu-system-*` | **QMP socket**(`savevm`/migrate) | 我们集群当前用的;clh 默认没注册时的回退 |
| `kata-clh` | `cloud-hypervisor` | CH 自己的 HTTP API(`/api/v1/vm.snapshot`) | 需在节点 containerd 显式注册 clh runtime(方案 A 下即在 EC2NodeClass.userData 的 drop-in 里加 kata-clh) |
| `kata-fc` | `firecracker` | **Firecracker REST**(`/snapshot/create`) | 同 Fly 那套;块设备-only,限制多 |

> 这就是难点:裸 Firecracker 只有一套 REST 快照 API(我们 `fc_snapshot_api.py` 用的);
> 而 Kata 下 VMM 可换,**快照接口不统一**,控制平面要按后端分别适配。

另一个难点:**Kata 把这些 socket/进程当内部实现**,没有"kubectl suspend pod"这种动作。
所以你不能在 K8s 控制面直接操作快照,必须下到节点层用 sandbox ID 找到 VMM 再调它的原生接口。

## 四、生产架构:控制平面 + 每节点 node agent

```
                    ┌─────────────────────────────────────┐
   suspend Pod X →  │  控制平面 (你写,无状态 API)          │
                    │  1. kubectl 查 X 的 nodeName + UID   │
                    │  2. 路由到 X 所在节点的 node agent   │
                    └──────────────┬──────────────────────┘
                                   │ gRPC/HTTP
        ┌──────────────────────────┼──────────────────────────┐
        ▼ (DaemonSet,每节点一个)   ▼                          ▼
   node-agent @ node1         node-agent @ node2         node-agent @ nodeN
   - 由 PodUID/sandboxID 在本节点定位 VMM 进程 + socket
   - 按后端类型调 VMM 快照接口(qemu QMP / clh HTTP / fc REST)
   - 内存+状态文件落 本地NVMe / S3,记录元数据(哪个Pod、哪个快照、在哪)
   - 恢复:用快照文件重建 VMM,重新关联回 sandbox
```

**挂起(suspend)流程:**
1. 控制平面:`kubectl get pod X -o jsonpath nodeName,uid` → 定位节点
2. 调该节点 node agent:`suspend(podUID)`
3. agent:podUID → sandbox ID → VMM 进程/socket(第二节链路)
4. agent:按后端调快照(qemu=QMP savevm / clh=vm.snapshot / fc=/snapshot/create),
   把 mem+state 写到本地 NVMe 或 S3,记元数据
5. (可选)释放该 VM 的 RAM —— 这才是省钱关键(实测恢复仅 ~ms 级)

**恢复(resume)流程:** 反向 —— 选节点 → agent 用快照文件重建 VMM → resume → 重新接入网络/重同步时钟。

**生命周期与 K8s 的张力(要想清楚):**
- 一个被"快照挂起并释放RAM"的沙盒,在 K8s 眼里那个 Pod 还在不在?两种设计:
  - **A. Pod 保留**(占调度位但不占RAM):agent 直接冻结 VMM。K8s 仍认为 Pod Running,简单但 K8s 资源账面不准。
  - **B. Pod 删除 + 元数据外存**:挂起=删 Pod+存快照;恢复=按快照在合适节点重建 Pod 并 load。账面准、可跨节点迁移,但要自管"沙盒↔快照↔Pod"映射(更接近 Fly/E2B 自研控制平面)。
- 这也是为什么很多团队**主沙盒平台干脆走裸 Firecracker + 自研调度**(Fly 模式),而不是硬在 Kata-on-K8s 上叠快照。

## 四点五、⚠️ 实测纠偏:定位到 VMM ≠ 能直接操作它的快照接口

**这是本调研最硬核、也最容易被想当然搞错的一点。** 直觉上,既然 `locate.py` 能定位到 VMM 进程和它的 `qmp.sock`,那 node-agent 直接连这个 socket 发快照指令不就行了?**实测发现:不行。**

**实测(kata-qemu 后端):** 在节点上用第三方进程去连 Kata qemu 的 `/run/vc/vm/<id>/qmp.sock`,连接**挂起 30+ 秒无响应**。原因:
- QMP 是 qemu 的**单客户端管理通道**,**kata-runtime/shim 已经独占连着它**做 VM 生命周期管理;
- 第二个连接被挂起/拒绝。这个 socket 是 **Kata 的私有实现**,不对外开放。

**结论:不能"绕过 Kata、外部直连 VMM 的 socket 做快照"。** `locate.py` 的定位结果用于**调试/监控/路由**是对的,但要真正暂停/快照,必须走 **Kata 自己暴露的接口**(`kata-runtime` 的实验性暂停/快照能力),而不是裸 QMP/裸 VMM socket。而 Kata 在 K8s 上**没有 turnkey 的快照动作,该能力成熟度低**(本调研一路验证的核心结论)。

**因此 "继承到 API" 的正确做法分两层:**

| 继承到哪 | 定位逻辑 | 快照动作 |
|---|---|---|
| `fc_snapshot_api.py`(裸 FC) | 控制平面自持,无需 locate | ✅ 直接调 Firecracker REST,已实测 2.7ms 恢复 |
| `app.py`(Kata-on-EKS) | ✅ 可继承 locate(已做成 `/locate` 端点) | ⚠️ **不能直连 QMP**;须走 Kata 官方暂停接口,K8s 上无 turnkey、不成熟 |

> 所以 `app.py` 干净能继承的是**定位能力**(`/locate`——调试、监控、未来接 Kata 暂停接口的基础);
> suspend/resume 在 Kata-on-K8s 上**不是简单"把 fc_snapshot_api 的逻辑搬过来"**就能成的——这是 Kata 路线的真实短板。

## 五、与裸 Firecracker 方案对比

| | 裸 Firecracker(`fc_snapshot_api.py`) | Kata-on-K8s + node agent |
|---|---|---|
| 定位 VMM | 控制平面自己持有,直接拿 | 必须 Pod→节点→shim→VMM 下钻 |
| 快照接口 | 单一 Firecracker REST | 按后端分(qemu/clh/fc),要适配 |
| 调度 | 自建(Fly 自己干的) | K8s 白送 |
| 实现复杂度 | 控制平面较重 | 调度省,但快照层(agent+元数据+映射)要自研 |

**选型建议:** 若快照省钱是核心诉求且要海量沙盒 → 认真评估"裸 Firecracker + 自研调度"(Fly 路线);
若已重度依赖 K8s 生态、快照是优化项 → Kata-on-K8s + node agent。

> 下一节的最小 demo(`snapshot-agent/locate.py`)实现了第二节的"由 Pod 名定位到 VMM 进程/socket",
> 这是 node agent 的第一步(也是最容易被低估的一步)。
