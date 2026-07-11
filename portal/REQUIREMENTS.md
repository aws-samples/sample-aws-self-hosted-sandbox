# Sandbox Portal — Demo Dashboard 需求文档

> 面向本仓库自托管 Firecracker microVM 沙盒平台的**轻量级 Demo Dashboard**(E2B / Fly.io 观感)。
> **形态定位:纯本地展示工具** —— 只在开发者本机运行,不上公网、不部署到 EKS,不追求生产级鉴权与高可用。

---

## 1. 背景与动机

本仓库是一个自托管的 Fly.io 式 **Firecracker microVM 沙盒平台**(AWS + EKS + DynamoDB):

- **控制面** `sandbox-api/app.py`:纯 Python 标准库 `http.server`,端口 8000,Bearer Token 认证,DynamoDB 为唯一真源。
- **node-agent**:每台 `c6g.metal` 节点上的执行手(端口 8002,内网),直接管 Firecracker microVM、快照、网络。
- **snapshot-agent**:快照(Full base + Diff 增量内存)相关。

平台此前**完全没有 UI**,所有操作靠 curl。本 Portal 是**第一个 UI**,目标有二:

1. **API Demo**:快速演示沙盒 创建 / 销毁 / snapshot(suspend) / resume / exec,并直观展示每次 API 的
   完整 response 与**耗时**。平台已在沙盒 record 里存了 `restore_time_s`、`snapshot_create_time_s`、
   `snapshot_size_bytes` 等指标,天然适合展示。
2. **SaaS 全局总览 Dashboard**:看到当前所有沙盒的状态、节点水位、暖池水位、事件时间线。

---

## 2. 目标与非目标

### 目标
- 一个 Dashboard 总览页 + 单沙盒详情页 + API Playground 页,观感对齐 E2B / Fly.io。
- 全租户沙盒总览(现有对外 API 只能按单租户列)。
- 每次 API 调用展示 `method + path + HTTP status + 耗时(ms) + 格式化 JSON`。
- 沙盒生命周期一键操作:create / suspend / resume / exec / destroy。

### 非目标(本期不做)
- **不部署到公网 / EKS**,不做 Dockerfile、Terraform、NLB、域名 —— 纯本地 `npm run dev`。
- 不做用户 / 租户注册登录体系(沿用平台的 API Key)。
- 不引入 WebSocket 实时推送(先用轮询)。
- 不做计费、告警、Prometheus 指标导出。
- 不做 mock 数据层(只连真实环境)。

---

## 3. 现有后端 API 与缺口

### 现有对外 API(`sandbox-api/app.py`,端口 8000,`Authorization: Bearer <key>`)
| Method | 路径 | 用途 |
|---|---|---|
| GET | `/` · `/capabilities` | 服务信息 / 能力(免鉴权) |
| POST | `/sandboxes` | 创建(body: image/cpu/mem_mib/env/services/tenant_id/idempotency_key) |
| GET | `/sandboxes?tenant_id=x` | 列出**单租户**沙盒(默认 limit=100) |
| GET | `/sandboxes/{id}` · `/{id}/wait` · `/{id}/locate` | 查询 / 长轮询 / 调试定位 |
| DELETE | `/sandboxes/{id}` | 销毁 |
| POST | `/sandboxes/{id}/suspend` · `/resume` · `/exec` | 挂起(快照)/ 恢复 / 执行命令 |

- 认证:`API_KEYS` env,`default` key 为管理员(可跨租户)。
- state 枚举(徽章需全覆盖):`creating / running / suspending / suspended / resuming /
  destroying / failed / warm / orphaned / needs_reschedule`。

### 缺口
现有对外 API **只能按单租户列表**,拿不到 SaaS 全局总览所需的聚合视图。这些数据都在 DynamoDB 里,
`db.py` 已有现成函数,只是没暴露成 HTTP。**解决方式:给 `sandbox-api` 补只读聚合 endpoint**(而非让
Portal BFF 直连 DynamoDB),核心服务改动最小、UI 侧零 AWS 凭证。

---

## 4. Part A — sandbox-api 补只读聚合 endpoint(已实现)

在 `app.py` 新增以下 GET 路由(仅 admin/`default` key;无鉴权开发模式放行);数据复用 `db.py` 现有函数;
仅在 `db.py` 新增了一个 `list_events()` 读函数。**未改动任何写路径。**

| 新增 endpoint | 复用 db 函数 | 返回 |
|---|---|---|
| `GET /admin/sandboxes` | `list_by_states(全部状态)` | `{sandboxes: [...]}` 全租户列表 |
| `GET /admin/nodes` | `list_active_nodes()` | `{nodes: [...]}` 活节点(free_mem_mib/vm_count/last_seen/labels) |
| `GET /admin/stats` | 上述 + `count_warm()` | 汇总:`total_sandboxes / by_state / node_count / cluster_free_mem_mib / running_vm_count / warm_pool` |
| `GET /admin/events?id=&limit=` | **新增** `list_events()` | `{events: [...]}` 时间线;`id` 为空则全局 |

- 权限:`_require_admin()`(仅 `default` 租户 key,开发模式放行)。
- `list_events`:给 `id` → 走主键 query;否则全表有界 scan 后按 `ts` 倒序取 `limit`(POC 规模可接受)。
- 测试:`sandbox-api/smoke_test.py` 新增 `TestAdminAggregates`(跨租户可见性 / stats 计数 / 事件 / 非 admin→403)
  与 `TestDB.test_list_events_single_and_global`。全部 29 用例通过。

---

## 5. Part B — Portal(Next.js App Router + BFF)

### 目录结构
```
portal/
├── REQUIREMENTS.md            本文件
├── README.md                  本地运行步骤
├── package.json / next.config.js / tsconfig.json
├── .env.local.example         SANDBOX_API_URL / SANDBOX_API_KEY
├── app/
│   ├── layout.tsx             侧边栏 + 主区
│   ├── globals.css            深色主题
│   ├── page.tsx               ① Dashboard 总览
│   ├── sandboxes/[id]/page.tsx  ② 单沙盒详情 + 时间线 + 操作
│   ├── playground/page.tsx    ③ API Playground
│   └── api/                   BFF Route Handlers(服务端持 Token)
│       ├── _lib/client.ts     统一封装:注入 token + 计时 + 错误归一化
│       ├── stats/route.ts · nodes/route.ts · events/route.ts
│       └── sandboxes/route.ts · [id]/route.ts · [id]/[action]/route.ts
├── components/  Sidebar · StatusBadge · StatCard · SandboxTable · NodeCard · Timeline · ApiResponseViewer
└── lib/         types.ts(对齐 record 字段)· format.ts · usePolling.ts
```

### BFF 的意义
浏览器**不直连** sandbox-api:Bearer Token 只留在本机 Node 进程(Route Handler),浏览器永不接触。
`app/api/_lib/client.ts` 统一给每次上游调用**计时**,并把 `{ ok, status, elapsed_ms, method, path, body, error }`
(`ApiCallResult`)一并回传前端 —— 直接支撑"展示 API response + 耗时"。上游不可达时归一化为 `status=0` +
友好错误提示(引导 `kubectl port-forward` / `.env.local`),不抛异常。

### 三个页面
1. **Dashboard(`/`)**:顶部汇总卡片(沙盒总数 / 运行中 / 活节点 / 集群空闲内存 / 暖池水位)+ 沙盒表格
   (id、state 徽章、镜像、规格、节点、租户、更新时间)+ 节点水位卡片 + 近期事件时间线。5s 轮询
   `/api/stats` `/api/sandboxes`,10s 轮询 `/api/nodes` `/api/events`。
2. **沙盒详情(`/sandboxes/[id]`)**:完整 record(配置)+ **性能指标**(resume/merge/snapshot 耗时与大小)
   + 事件时间线 + 操作按钮(Exec / Suspend / Resume / Destroy),每次操作的响应用 `ApiResponseViewer`
   就地展示。5s 轮询刷新。
3. **API Playground(`/playground`)**:E2B 式左表单右响应。选操作(create/get/exec/suspend/resume/destroy)
   → 填参数 → 发起,右侧展示 method+path、HTTP status、**耗时(ms)**、格式化 JSON。create 成功自动回填
   id 串起后续操作,并给出详情页跳转。

### 数据模型(`lib/types.ts`)
`Sandbox`(对齐 record 全字段,含指标)、`NodeInfo`、`SandboxEvent`、`Stats`、`ApiCallResult<T>`。

---

## 6. 运行方式(纯本地)

1. 把真实控制面通到本机:
   ```bash
   kubectl port-forward -n sandbox-system svc/sandbox-control-plane 18000:80
   ```
2. 配置环境:
   ```bash
   cd portal && cp .env.local.example .env.local
   # 填 SANDBOX_API_URL=http://localhost:18000  与  SANDBOX_API_KEY=<default key>
   ```
3. 启动:
   ```bash
   npm install && npm run dev   # → http://localhost:3000
   ```

Token 只在本机 BFF 进程,不出本机。若控制面未起,页面展示友好空态/错误提示。
**无 Dockerfile、无 Terraform、无 K8s Secret。**

---

## 7. 验证(end-to-end)

1. **聚合 endpoint 单测**:`python3 sandbox-api/smoke_test.py`(moto mock DynamoDB)—— 覆盖 `/admin/*`。✅ 29/29 通过。
2. **Portal 构建**:`cd portal && npm run build` —— 类型检查 + 编译通过。✅ 9 路由。
3. **本地联调**:port-forward 通到真实控制面 → `npm run dev` → 走一遍
   create → 详情页看指标 → suspend(看 snapshot 大小/耗时)→ resume(看 restore_time_s)→ exec → destroy,
   Dashboard 总览与事件时间线实时反映。
4. **Playground 计时校验**:发起 exec,确认响应面板 `elapsed_ms` 与 rc/stdout/stderr 正确。

---

## 8. 已实现文件清单

**sandbox-api(Part A)**
- `sandbox-api/app.py` — 新增 `_ALL_STATES` / `_require_admin` / `admin_*` 函数 + `GET /admin/*` 路由 + `/` endpoints 列表。
- `sandbox-api/db.py` — 新增 `list_events()`。
- `sandbox-api/smoke_test.py` — 新增 `TestAdminAggregates` + `test_list_events_single_and_global`。

**portal(Part B)** — 见第 5 节目录结构,全部新增。
