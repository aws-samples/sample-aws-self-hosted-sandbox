# 沙盒端口暴露设计（Firecracker 版）

> 目标：让沙盒里跑的服务（web / vibe coding 预览 / 任意 TCP 端口）能从集群外访问，
> 并在 Portal 上展示可点击的访问入口。**先用 NLB 自带域名，不依赖自定义域名/证书。**
>
> 状态：设计稿，待 review 后实现。

---

## 1. 背景：这个功能随 Kata 删除一起丢了

历史上（Kata-on-K8s 时代）**有**端口暴露，实现在已删除的 `sandbox-api/drivers/kata.py`：
create 沙盒时用 K8s API 自动建 **Pod + ClusterIP Service + Ingress**，Ingress host 用
`{port}-{sid}.sbx.example.com`（通配符子域名），链路：

```
用户 → 通配符 DNS → 共享 NLB → ingress-nginx → Service(label selector=sandboxId) → Pod
```

`chore: remove Kata driver`（commit `6754347`）删掉 `kata.py` 时，这套端口暴露**一并消失**。

### 为什么不能照搬回来

旧方案每一环都建立在**「沙盒 = K8s Pod」**上：Service 靠 label selector 选 Pod、Ingress 后端指向 Service。
而 Firecracker 沙盒是 **node-agent 在 metal 上直起的裸 microVM，K8s 完全不感知**，guest IP 是节点本地
`172.18.{tap_idx}.2`（`node-agent/main.py:97`，不可路由）。K8s Service 没有 Pod 可选 → 旧代码即便加回来也指向空。

**必须换一套不依赖 K8s Pod 的暴露机制。**

---

## 2. 关键约束（来自需求）与它如何决定方案

| 约束 | 影响 |
|---|---|
| **先用 NLB 自带域名**（`xxx.elb.us-east-1.amazonaws.com`），不上自定义域名/证书 | NLB 自带域名**无法挂通配符子域名**，所有请求 Host 头都一样 → **不能靠 Host 区分沙盒**（旧的 `{port}-{sid}.域名` 方案作废） |
| **多个沙盒可暴露同一内部端口**（如两个沙盒都开 80，且在同一台 metal 上） | 路由键必须是**沙盒 id（+目标端口）**，不能是「宿主端口」或「Host 子域名」。同 node 上 2 个 guest 的 `172.18.A.2:80` 与 `172.18.B.2:80` 靠 sid 天然区分 |

**结论：改用基于路径（path-based）的路由**，而非 Host 子域名：

```
http://<nlb-hostname>/s/<sandbox_id>/<port>/<原始路径...>
```

`s/{sid}/{port}` 唯一定位一个沙盒的一个端口，与「宿主端口是否冲突」「是否同 node」完全解耦。
两个沙盒都开 80：`/s/aaa/80/` 和 `/s/bbb/80/`，互不影响。

---

## 3. 目标架构

```
用户浏览器
  │  http://<nlb>/s/<sid>/<port>/...
  ▼
共享 NLB ──► ingress-nginx ──► [新增] sandbox-proxy (K8s Deployment+Service，常驻)
                                   │  1) 解析路径 → (sid, port)
                                   │  2) 查 DynamoDB sandboxes 表 → 拿 {node, guest_ip, state}
                                   │  3) 反代到  http://<node_ip>:<node_agent_port>/proxy/<sid>/<port>/...
                                   ▼
                            node-agent（每台 metal，hostNetwork）
                                   │  4) 转发到 guest 172.18.{tap_idx}.2:<port>
                                   ▼
                            Firecracker microVM 内的服务（如 :80 的 web）
```

**为什么要 sandbox-proxy 这一层**：它取代了旧方案里「K8s Service 自动选 Pod」的角色 —— 因为 Firecracker
沙盒不在 K8s 里，只能由一个知道「sid → node/guest_ip」映射（在 DynamoDB）的应用层代理来做动态路由。

**为什么还要经 node-agent 再转一跳**：guest IP `172.18.x.2` 只在**该 metal 节点本地**可达（tap /30 子网）。
sandbox-proxy 跑在别的 pod 里，路由不到 guest，必须让请求先到那台 node 的 node-agent（hostNetwork，能访问本机
tap 网段），再由它转进 guest。

---

## 4. 需要实现/改动的组件

### 4.1 node-agent：新增入站转发端点（当前只有出站 MASQUERADE，无入站）

现状：`_setup_tap`（`node-agent/main.py:94-123`）只配了 SNAT MASQUERADE + FORWARD，让 guest **能出网**；
**没有任何 PREROUTING/DNAT**，外部进不到 guest。

新增 `GET/POST/* /proxy/{sid}/{port}/{path...}`：
- 从进程内 `_VMS` 表（`main.py:84`）查 sid → `guest_ip`（`172.18.{tap_idx}.2`）。
- 反向代理到 `http://{guest_ip}:{port}/{path}`，透传 method/headers/body/响应（支持 WebSocket 更佳，vibe coding 预览常用热更新）。
- sid 不存在 / 非 running → 404 / 409。

> 用应用层反代（而非 iptables DNAT）的理由：无需管理宿主端口分配、天然支持「多沙盒同内部端口」、
> 便于加鉴权/日志、随沙盒销毁自动失效。iptables DNAT 方案要给每个暴露端口分配唯一宿主端口，
> 与「多沙盒同端口」约束冲突，弃用。

### 4.2 sandbox-api：新增 sandbox-proxy（独立轻量服务，或控制面内新模块）

- 路由 `/s/{sid}/{port}/{path...}`：查 `db.get(sid)` → 拿 `node`、`state`、校验该 port 在 `services` 声明内。
- 反代到 `http://{node}:{NODE_AGENT_PORT}/proxy/{sid}/{port}/{path}`。
- 建议独立 Deployment（与控制面 API 解耦，避免代理流量压到控制面），复用同一 DynamoDB 只读权限。

### 4.3 services 字段真正落地（当前是「半成品」）

现状：create 收 `services:[{port,...}]` 存进 DynamoDB，但 **driver 没把它传给 node-agent**
（`firecracker.py:44-52` 的 body 无 services），`autostop/autostart` 注释写「字段占位」。

改动：
- 把 `services` 一并写入沙盒 record（已存）→ proxy 用它校验「该端口是否允许暴露」。
- （可选）create 时把 declared ports 传给 node-agent 备用；应用层反代方案其实不强依赖，端口是运行时决定的。

### 4.4 terraform：启用 ingress-nginx + NLB（地基现成）

- `create_ingress_nginx`（`main.tf:689`，默认 true）+ ingress-nginx helm + NLB 注解**都还在**，
  POC 部署时传了 `create_ingress_nginx=false` 跳过。重新部署时**去掉该 false** 即可拉起共享 NLB。
- 新增一条 Ingress：`path: /s` → 后端 sandbox-proxy Service。
- **NLB 自带域名获取**：`kubectl get svc -n ingress-nginx ingress-nginx-controller -o jsonpath='{.status.loadBalancer.ingress[0].hostname}'`
  → 形如 `xxxx.elb.us-east-1.amazonaws.com`。Portal 展示的 URL 前缀即用它。

---

## 5. Portal 展示

### 5.1 沙盒详情页新增「公开服务 / Exposed Services」卡片
- 列出该沙盒 `services` 里每个端口：`port`、协议、**可点击访问 URL**、状态。
- URL = `http://<nlb-hostname>/s/<sid>/<port>/`。
- NLB hostname 来源：新增 `GET /admin/cluster`（或复用 `/admin/stats`）返回 `nlb_hostname`
  （控制面读环境变量或查 K8s），BFF 透传给前端拼 URL。

### 5.2 创建表单 / Playground
- create 表单加「暴露端口」输入（多个），提交进 `services`。
- 创建成功后直接给出可点击的预览链接 —— 正是 vibe coding 客户要的「起个 web 服务 → 拿到 URL 打开」。

### 5.3 Dashboard 总览
- 沙盒表格可加一列「公开端口」，有暴露的显示端口数徽章。

---

## 6. 安全与边界（务必在实现时处理）

- **鉴权**：`/s/{sid}/{port}` 默认无鉴权即公网可达任意沙盒服务 —— demo 可接受，生产需加 token / 租户校验
  （proxy 层校验 caller 是否有权访问该 sid，复用 API key→tenant 映射）。
- **HTTP 明文**：NLB 自带域名无证书，走 HTTP。生产才上自定义域名 + ACM/cert-manager TLS。
- **仅 declared ports**：proxy 只转 `services` 里声明过的端口，避免把 guest 任意内部端口暴露出去。
- **WebSocket / SSE**：vibe coding 预览（Vite HMR 等）依赖 ws，反代需支持 Upgrade。
- **沙盒销毁 / suspend**：proxy 每次查 DynamoDB 实时状态，非 running 直接拒绝，无需额外清理。

---

## 7. 实施顺序（建议）

1. **node-agent** 加 `/proxy/{sid}/{port}/{path}` 应用层反代（+ WebSocket）。
2. **sandbox-proxy** 服务 + K8s Deployment/Service + Ingress `/s` 规则。
3. **terraform** 重新启用 ingress-nginx（去掉 `create_ingress_nginx=false`），拿到 NLB hostname。
4. **控制面** 加 `/admin/cluster` 暴露 nlb_hostname；`services` 字段串到 record。
5. **Portal** 详情页 Exposed Services 卡片 + 创建表单端口输入 + 可点击 URL。
6. 端到端验证：**两个沙盒同开 80** → `/s/A/80/` 与 `/s/B/80/` 分别打开各自 web，互不串。

---

## 8. 与旧 Kata 方案对照

| 维度 | 旧 Kata 方案（已删） | 本方案（Firecracker） |
|---|---|---|
| 路由键 | Host 子域名 `{port}-{sid}.域名` | URL 路径 `/s/{sid}/{port}` |
| 后端选址 | K8s Service label selector 选 Pod | sandbox-proxy 查 DynamoDB → node/guest_ip |
| 进 guest 的最后一跳 | K8s 网络直达 Pod | node-agent 应用层反代进 tap /30 |
| 域名/证书 | 需通配符 DNS + 证书 | 先用 NLB 自带域名，HTTP，零 DNS |
| 多沙盒同端口 | 靠不同子域名区分 | 靠不同 sid 路径区分（天然支持） |
| 自动化 | create 时建 Pod/Svc/Ingress | create 无需建 K8s 对象，proxy 运行时动态路由 |

---

## 9. 已实现增强(2026-07 真机验证)

在第 3-8 节的路径反代基础上,后续增强均已实现并在 EKS + c6g.metal 真机验证通过:

### 任意端口暴露(`ALLOW_ALL_PORTS`,默认开)
- 控制面 `resolve_proxy_target` 默认放行**任意端口**,用户在 guest 内起在任何端口的服务
  都可经 `/s/{id}/{port}/` 访问,**无需 create 时预声明**(对齐 E2B/Fly)。
- 设 `ALLOW_ALL_PORTS=0` 退回"仅 services 声明端口"的白名单模式(多租户生产更安全)。

### WebSocket 透传
- 控制面 `/s/` 与 node-agent `/proxy/` 均检测 `Upgrade: websocket`,命中则**原始 socket 双向隧道**
  (101 切换后是二进制帧流,代理不解析帧只转发字节)。支持 Vite HMR、SSE、以及下面的 Web Terminal。
- 实现:`_raw_tunnel()` + `_tunnel_ws()`(app.py / node-agent 各一份)。

### 端口暴露鉴权(`EXPOSE_TOKEN`,默认关)
- 留空 = 公开可达(demo)。设置后访问 `/s/` 必须带 token:
  `?token=` > Cookie `sbx_token` > Header `X-Sbx-Token`。首次用 `?token=` 通过会种 Cookie,
  之后浏览器内子请求(JS/CSS/XHR)自动带,免重复。
- `/admin/cluster` 返回 `expose_token` 供 Portal 拼可点击链接。

### 交互式 Web Terminal
- **不改 node-agent、不重建 rootfs**:Portal 一键 exec 一段自包含的 PTY-over-WebSocket
  python 服务(`portal/lib/termServer.ts`,guest 自带 python3 + pty + 完整 stdlib,自己做 WS 帧编解码
  驱动 bash PTY),起在某端口,浏览器经端口暴露反代(已支持 WS)连上 → 得真实交互终端(xterm.js)。
- 详情页"打开终端"按钮:exec 起服务 + 新标签打开。
- 真机验证:浏览器内 `uname` → `Linux`,PTY 双向交互经两层代理正常。

> Portal 本地运行(无 NLB)时,`/api/cluster` 的 BFF 把空 `proxy_base` 回退为控制面地址
> (`SANDBOX_API_URL`,port-forward 的 `http://localhost:18000`),使终端/Web 链接指向控制面而非 Portal 自身。
