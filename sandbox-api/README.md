# 沙盒控制平面 API(demo)

本目录有**两个** API demo,对应两条技术路线——它们不在同一层,能力也不同:

| 文件 | 路线 | 沙盒= | 调度 | 快照/恢复 |
|---|---|---|---|---|
| `app.py` | **EKS + Kata**(Phase 3) | 一个 Pod | ✅ K8s 自动 | ❌ Kata-on-K8s 无 turnkey 快照 |
| `fc_snapshot_api.py` | **裸 Firecracker**(Phase 1,"Fly 模式") | 一个 FC 进程 | ❌ 要自建 | ✅ suspend/resume 秒级 |

> **关键架构事实(给客户):** Firecracker 的快照/恢复是**裸 Firecracker 的能力**,Kata 没把它做成 K8s 上的开箱功能。所以:"EKS+Kata 白送调度但无 turnkey 快照;裸 Firecracker 有快照但调度自建(Fly 就是自建了这套)"。生产若两者都要,需在 EKS+Kata 基础上自行集成快照,或用裸 Firecracker + 自研调度。

---

## A. `app.py` —— Kata-on-EKS 沙盒 API

把"创建/销毁/列出沙盒"封装成 REST API。**沙盒 = 带 `runtimeClassName=kata-qemu` 的 Pod + Service + Ingress**。
这层就是"唯一需要自研的部分":K8s 负责调度/装箱/健康,你只加一层薄 API。

```bash
# 前提:aws eks update-kubeconfig --name claude-sbx --region us-east-1(kubectl 能连集群)
python3 app.py    # 监听 127.0.0.1:8000,零外部依赖(纯标准库 + kubectl)
```

## 接口与实测

| 操作 | 命令 | 实测结果 |
|---|---|---|
| 创建 | `curl -XPOST :8000/sandboxes` | 返回 `{id,url,status}`,~12s 后 Running |
| 列出 | `curl :8000/sandboxes` | 列出所有沙盒(id/status/ready/url/node) |
| **定位 VMM** | `curl :8000/sandboxes/{id}/locate` | 沙盒→节点→EC2→VMM 进程→后端→socket(继承自 `../snapshot-agent/locate.py`) |
| 在沙盒内执行 | `curl -XPOST :8000/sandboxes/{id}/exec -d '{"cmd":"uname -r"}'` | 实测 guest 内核 6.18.28、Claude Code 2.1.173 → 真 microVM |
| 端口访问 | 经 ingress `Host: 8080-{id}.sbx.example.com` | 返回沙盒内 8080 服务内容 |
| 销毁 | `curl -XDELETE :8000/sandboxes/{id}` | Pod+Service+Ingress 全回收,列表清空 |

### `/locate` 实测输出

```json
{
  "id": "234049", "node": "ip-10-0-102-222...", "runtimeClass": "kata-qemu",
  "ec2_instance": "i-0d5b5978...", "sandbox_id": "170c0658ae99...", "vmm_pid": "28649",
  "backend": "qemu", "snapshot_interface": "QMP socket (Kata 独占,不可外部直连)",
  "vm_runtime_dir": "/run/vc/vm/170c0658ae99.../", "sockets": "console.sock,pid,qmp.sock,vhost-fs.sock"
}
```

> ⚠️ **为什么没有 `/suspend` `/resume`**:实测发现 Kata 的 qemu `qmp.sock` 被 kata-runtime **独占**,
> 外部进程直连会挂起 —— 不能"把 fc_snapshot_api 的快照逻辑搬进来直连 VMM"。Kata-on-K8s 无 turnkey 快照。
> 所以 `app.py` 干净继承的是**定位**(`/locate`:调试/监控/未来接 Kata 暂停接口的基础);
> 真正的快照能力见 `fc_snapshot_api.py`(裸 Firecracker)。详见 [`../Kata快照定位机制.md`](../Kata快照定位机制.md) §四点五。

## 演示流水(已验证)

```
POST /sandboxes              → id=228243, status=creating
GET  /sandboxes              → [{id:228243, status:Running, ready:true, url:...}]
POST /sandboxes/228243/exec  → stdout: kernel=6.18.28 / nproc=3 / 2.1.173 (Claude Code)
(ingress Host 路由)           → "sandbox 228243 ready"
DELETE /sandboxes/228243     → deleted:true
GET  /sandboxes              → []
```

## 从 demo 到生产

- **后端**:demo 用 `kubectl` subprocess(零依赖)。生产换成 K8s client SDK,或直接采用开源 **`kubernetes-sigs/agent-sandbox`** 的 CRD(`Sandbox`/`SandboxClaim`/`SandboxWarmPool`)+ Python SDK——把"创建/销毁/暖池"变成声明式,底层隔离仍是这套 Kata microVM。
- **鉴权/多租户**:加 API key/JWT;按租户隔离 namespace + NetworkPolicy;Bedrock 凭据走节点 IRSA / 出口代理,不进沙盒(R8)。
- **暖池**:预创建一批 Pending 沙盒,`POST` 时直接 claim,把 ~12s 创建延迟降到亚秒(对应文档 warm buffer)。
- **端口**:配 Route53 通配符 `*.sbx.example.com` → NLB,ACM 通配符证书在 NLB 层终止 TLS(见主文档 3.4)。
- **Kata 后端**:demo 用 `kata-qemu`;若要 clh(virtio-fs/热插拔),在 kata-deploy 显式启用 clh shim 后改 `RUNTIME_CLASS=kata-clh`。

---

## B. `fc_snapshot_api.py` —— 裸 Firecracker 快照 API(Fly suspend/resume 同款)

含 Fly 同款 **suspend(快照+释放RAM)/ resume(秒级恢复)**。直接调 Firecracker API socket + 管本地 tap/snapshot 文件,**必须跑在 .metal 主机本机**(需 root)。

```bash
# 在 .metal 主机:前提 /opt/sbx/{vmlinux,rootfs.ext4} 已就绪(setup-host.sh)
sudo python3 fc_snapshot_api.py    # :8001,零依赖
```

| 操作 | 命令 |
|---|---|
| 创建启动 | `curl -XPOST :8001/sandboxes` → `{id, ip}` |
| **挂起(快照+释放RAM)** | `curl -XPOST :8001/sandboxes/{id}/suspend` |
| **恢复(秒级)** | `curl -XPOST :8001/sandboxes/{id}/resume` |
| 列出 | `curl :8001/sandboxes` |
| 销毁 | `curl -XDELETE :8001/sandboxes/{id}` |

### 演示流水(已实测,全程经 REST API)

```
POST /sandboxes              → {id:229235, state:running, ip:172.18.2.2}
  heartbeat HEARTBEAT=3, host_used=1188MB
POST /sandboxes/229235/suspend → {state:suspended, snapshot_create_time_s:33.5, mem:4GB}
  host_used 1188→1004MB   ✅ RAM 释放
POST /sandboxes/229235/resume  → {state:running, restore_time_s:0.0027}  ← 2.7ms!
  heartbeat HEARTBEAT=4,5,6  ✅ 从快照点续上(3→4),MAGIC 状态原样在
DELETE /sandboxes/229235     → {deleted:true}
```

**结论:** resume **~2.7ms** 亚秒级、RAM 完全回收、内存状态精确续上——这是"24×7 可达≠24×7 常驻"的成本核心杠杆。

### 到生产要补
- **Full 快照 33s 慢**:换 **diff 快照(只存脏页)+ 本地 NVMe**(`i` 系列 .metal,本测试用 gp3);空载沙盒脏页少,快得多。
- **恢复 caveat**(Firecracker 已知):重建 guest 网络、重同步时钟(NTP)、丢弃旧 vsock、禁止原始+克隆同跑——编排层需处理。
- **调度自建**:本 demo 单机;多机要自己做放置/装箱/IP 分配(即 Fly/E2B 自建的那套控制平面)。
- 快照文件落 S3/本地 NVMe 做持久与跨主机迁移。
