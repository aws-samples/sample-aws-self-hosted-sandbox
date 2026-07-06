# Firecracker Driver 生产就绪 Gap 分析

> 编写日期：2026-07-06 · 基于 commit `e8129f8`
> 范围：`sandbox-api/`(控制面)+ `node-agent/`(每节点执行手)+ `drivers/firecracker.py`
> 目的：盘点 FC driver 从"POC happy-path 跑通(e2e 14/14 PASS)"到"大规模生产就绪"之间缺的编排/运维能力，按优先级排列，逐项给出代码位置、Kata(K8s) 对照、修复方向。
>
> **核心判断**：FC 的 suspend/resume 机制已验证可用，但其编排层是一个"跑通了主流程的 POC"，而非生产系统。差距不在"FC 能不能用"，而在"FC 周围要补一整圈 Kata 靠 K8s **白送**、而这里必须**自建**的运维层"。选 FC = 承诺自建一个 mini 编排系统。

---

## 零、结论速览

| 优先级 | Gap | 一句话风险 | 代码锚点 |
|---|---|---|---|
| **P0** | 无 reconcile loop | node-agent 重启丢内存态 → DynamoDB 状态永久漂移 | `node-agent/main.py:63-64` |
| **P0** | S3 快照上传"发射后不管" | suspend 假成功 + 内存已释放 → resume 时数据永久丢失 | `node-agent/main.py:238-250, 377-390` |
| **P0** | 节点发现靠环境变量硬编码 | 无法弹性伸缩；故障节点上的沙盒无人接管 | `drivers/firecracker.py:169-175` |
| **P1** | autostop/idle 检测未实现 | "自动挂起省钱"这一 FC 核心价值的**触发器**根本没做 | `driver.py:19` |
| **P1** | 调度只看 free_mem | 无 CPU 维度/装箱/反亲和 → 大规模碎片化、CPU 打满 | `drivers/firecracker.py:150-167` |
| **P1** | 无磁盘水位/快照 GC | 实测已撞过撑爆 200GB 触发 DiskPressure | 全局缺失 |
| **P1** | 控制面单进程、loop 无 leader 选举 | 多副本暖池互相打架；单副本是单点 | `app.py:51-52`, `warm_pool.py:117-126` |
| **P2** | 网络编排全自研 + sbxinit 硬编码 IP | 对外暴露沙盒服务基本空白；tap_idx≠1 时 SSH 通道连不上 | `node-agent/main.py:72-96` |
| **P2** | create 乐观写 running | 无真正就绪信号(两 driver 共有) | `app.py:159-160` |
| **P2** | jailer 隔离默认关闭 | `USE_BARE_FC=1` → 无 chroot/seccomp/cgroup 限制 | `node-agent/main.py:133-135` |
| **P2** | 可观测性缺失 | 日志被静音、无 metrics/tracing → 出事无排障抓手 | `app.py:318-319`, `main.py:626` |

---

## 一、P0 — 真实流量打进来前必须先堵

这三个洞会导致**静默数据丢失**和**状态漂移**这类最难排查的故障，是"不知道会遇到什么问题"的最大来源。

### P0-1　无 reconcile loop：状态会漂移

**现状**
- node-agent 的运行时表 `_VMS` 是**进程内内存 dict**(`node-agent/main.py:64`)，注释明写"重启后靠控制面重新 reconcile"(`:63`)。
- 但全仓库 grep `reconcile` / 对账 —— **这个 reconcile 根本没有实现**。控制面 `driver.py:74` 定义了 `get_runtime_state` 用于"健康对账"，却没有任何后台循环去调它。

**失败场景**
1. node-agent 因 OOM / 部署 / 宿主宕机重启 → `_VMS` 清空，所有 VM 操作句柄(pid/sock/tap)丢失。
2. 但 DynamoDB 里这些沙盒仍是 `running` / `suspended`。
3. 之后对它们的 exec/suspend/destroy 全部失败，而控制面**不知道**，继续当它们是活的。
4. tap_idx、磁盘目录、DynamoDB 记录全部泄漏，无人回收。

**Kata 对照**：kubelet 持续把真实 Pod 状态 reconcile 回 etcd，Pod 挂了自动重建或标记 Failed。这一层 K8s 白送，FC 完全没有。

**修复方向**
- 控制面加后台 reconcile loop：定期扫 `running`/`suspended`/`suspending`/`resuming` 记录 → 逐个调 `get_runtime_state` 对账 → 漂移则修正状态或告警。
- node-agent 启动时可选自恢复：扫 `SBX_BASE` 下残留目录 + 探测 FC api.sock 存活，重建 `_VMS`(比纯内存表更健壮)。
- reconcile loop 本身需 leader 选举(见 P1-4)，避免多副本重复对账互相打架。

### P0-2　S3 快照上传"发射后不管"，失败静默 → 数据丢失

**现状**
- `_s3_upload` 起一个 daemon thread 就返回，且 `subprocess.run(..., check=False)`(`node-agent/main.py:243-250`)。
- `op_suspend` **不等上传完成就返回成功**(`:382/:390` 调完 `_s3_upload` 直接 return)，控制面随即标 `suspended` 并 kill VMM 释放内存(`:364-368`)。

**失败场景**
1. suspend 返回 200，VM 内存已释放，DynamoDB 标 `suspended`。
2. 后台 S3 上传因网络抖动 / S3 限流 / 节点此刻宕机而失败 —— **无人知道**(`check=False` 吞掉错误，无重试无告警)。
3. resume 时 `_s3_download` 从 S3 拉不到快照三件套 → **沙盒数据永久丢失**。

对"挂起省钱"这个 FC 核心卖点，这是不可接受的静默数据丢失。

**修复方向**
- 上传**成功确认后**才把状态置 `suspended`；上传中用中间态(如 `snapshotting`)。
- 上传失败 → 重试(指数退避)→ 仍失败则状态回滚 + 告警，绝不 kill VMM 前置。
- 或：先确保内存快照落 S3 成功，再释放本地内存。

### P0-3　节点发现靠环境变量硬编码

**现状**
- `_list_metal_nodes` 直接读 `FC_NODES=ip1,ip2` 环境变量(`drivers/firecracker.py:174`)，注释自承"POC 阶段"。docstring 说未来应从 DynamoDB node registry 或 EC2 DescribeInstances 拉，但**未实现**。

**失败场景**
- 加/减节点要改环境变量 + 重启控制面 → 无法弹性伸缩。
- 节点宕机后仍在列表里，`_pick_node` 每次都去试它、超时、skip → 徒增建沙盒延迟。
- **已落在故障节点上的沙盒无人接管**：没有 VM 级故障检测与重调度。

**Kata 对照**：K8s 节点 NotReady 自动驱逐 + 重调度 Pod；节点池由 Karpenter 弹性伸缩。

**修复方向**
- 落地真正的节点注册表：node-agent 定期心跳写 DynamoDB(node_id/IP/free_mem/vm_count/last_seen)，控制面从表里读活节点(last_seen 超时即剔除)。
- 与 P0-1 的 reconcile 结合：故障节点上的沙盒标记 + 告警(FC 快照跨机 resume 已支持，可作为重调度基础)。

---

## 二、P1 — 规模化必需，但当前缺失

| 能力 | 现状 | 大规模影响 | 修复方向 |
|---|---|---|---|
| **autostop / idle 检测** | `ServiceSpec.autostop` 是占位字段(`driver.py:19` 注释"下阶段实现") | "自动挂起省钱"的**触发器没做**，现在只能手动 POST /suspend。这是 FC 相对 Kata 的核心价值却未落地 | 加 idle 探测(无连接/无 exec 一段时间)→ 自动触发 suspend；配合唤醒代理在有流量时 auto-resume |
| **调度策略** | 仅按 `free_mem_mib` 挑最空节点(`drivers/firecracker.py:150-167`) | 无 CPU 维度、无装箱、无超卖比、无反亲和 → 内存够但 CPU 打满 / 碎片化 | 多维评分(CPU+mem+vm_count)；可选装箱策略；同租户反亲和 |
| **磁盘水位 / 快照 GC** | 无 | 实测报告 §七已撞过"磁盘泄漏撑爆 200GB → DiskPressure 驱逐"(op_destroy kill 失败连带 rmtree 没执行) | 磁盘配额 + 孤儿目录清理(结合 reconcile)+ 旧快照 S3 生命周期策略 GC |
| **控制面 HA** | 单进程 `ThreadingHTTPServer`(`app.py:458-460`)；`warm_pool` replenish loop 进程内跑(`warm_pool.py:117-126`)；启动即 `start_replenish_loop()`(`app.py:51-52`) | 多副本会**互相打架**(暖池重复补、reconcile 重复跑、无 leader 选举)；单副本是单点 | loop 类逻辑加 leader 选举(DynamoDB lock)；无状态请求路径可多副本，后台 loop 单实例 |
| **长阻塞占线程 / 背压** | suspend 同步阻塞最长 300s(`app.py` timeout)；exec 同步 | ThreadingHTTPServer 无并发上限，大量并发 suspend 耗尽线程 | suspend 异步化(提交任务 + 轮询状态)；加并发上限与背压 |

---

## 三、P2 — 相比 Kata 明确"功能不完整"处

这些是 Kata 因跑在 K8s 上而**免费拥有**、FC 要么自研要么没有的能力。

1. **网络编排**：Kata = Service + Ingress + CoreDNS，多端口/域名路由现成；FC = 手搓 tap `/30` + iptables MASQUERADE(`node-agent/main.py:72-96`)。对外暴露沙盒服务这块基本空白。
   - 附带 bug：`sbxinit` 硬编码 guest IP `172.18.1.2`(实测报告 §8.4)，只有 tap_idx=1 时对，SSH 通道对其他沙盒本就连不上(已改用 vsock 通道规避，但 bug 未修)。
2. **健康探针 / 就绪**：Kata = liveness/readiness probe；FC = create 乐观写 running(`app.py:159-160`，两 driver 共有)，靠 exec 层重试兜底，无真正就绪信号。
3. **资源限制强制**：Kata = K8s cgroup limits；FC = jailer 的 chroot+seccomp+cgroup **默认关闭**(`node-agent/main.py:133-135`，`USE_BARE_FC=1`)。生产必须切 jailer，否则隔离性和资源限制都降级。
4. **滚动更新 / 优雅排水**：Kata = K8s Deployment / node drain；FC = 无节点排水机制，节点下线沙盒直接消失。
5. **RBAC / 审计 / 事件**：Kata = K8s RBAC + audit log + events；FC = app 层自建(tenant 校验有 `_check_tenant_access`，审计只有 `write_event`，无细粒度 RBAC)。

---

## 四、已经做对的部分（避免重复造轮子）

FC 编排层并非一穷二白，以下已实现且方向正确，无需推翻：

- **DynamoDB lease 乐观锁**防并发操作同一沙盒(`db.acquire_lease` + `ConditionalCheckFailedException` → 409)。
- **幂等键**(`idempotency_key`)防重复创建(`app.py:121-125`)。
- **tap_idx 原子分配**(DynamoDB counter，`db.alloc_tap_idx`)避免网段碰撞。
- **状态机**(creating→running→suspending→suspended→resuming，带 prev_state 条件更新)。
- **多租户隔离**(tenant_id 过滤 + 访问校验)。
- **暖池**(warm_pool，FC 模式 ~7ms claim)。
- **driver 抽象**(Protocol 可插拔，Kata/FC 同一套控制面 API)。

---

## 五、建议推进顺序

1. **先堵 P0**(reconcile loop → S3 上传可靠性 → 节点注册表)：这三个不堵，任何真实流量都可能触发静默数据丢失/状态漂移。
2. **再补 P1 的 autostop**：这是 FC 相对 Kata 的核心价值主张，不做则"省钱"停留在手动。
3. **P1 其余 + P2** 随规模增长按需补齐。

> 战略提醒：走 FC = 承诺自建 mini 编排系统。若某天 suspend/resume 不再是硬需求，Kata(K8s 白送上述全部运维层)仍是更省心的退路——两个 driver 是同一控制面下的可插拔后端，保留 Kata 的边际成本几乎为零。
