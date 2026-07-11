# Sandbox Portal

自托管 Firecracker microVM 沙盒平台的 **Demo Dashboard**(E2B / Fly.io 观感)。
**纯本地展示工具** —— 只在本机跑,不上公网、不部署到 EKS。

详细需求见 [`REQUIREMENTS.md`](./REQUIREMENTS.md)。

## 功能

- **Dashboard**:全租户沙盒总览、汇总卡片(各 state 计数 / 活节点 / 集群空闲内存 / 暖池水位)、节点水位、事件时间线。
- **沙盒详情**:完整 record + 性能指标(resume / snapshot 耗时与大小)+ 时间线 + 一键 Suspend/Resume/Exec/Destroy。
- **API Playground**:选操作 → 填参数 → 发起,实时展示 response 与**耗时(ms)**。

## 本地运行

前置:已能 `kubectl` 访问部署了 `sandbox-control-plane` 的集群,并拿到 admin(`default`)API Key。

```bash
# 1. 把真实控制面通到本机(单独开一个终端保持运行)
kubectl port-forward -n sandbox-system svc/sandbox-control-plane 18000:80

# 2. 配置
cd portal
cp .env.local.example .env.local
#   编辑 .env.local:
#     SANDBOX_API_URL=http://localhost:18000
#     SANDBOX_API_KEY=<你的 default key>

# 3. 启动
npm install
npm run dev            # → http://localhost:3000
```

> Bearer Token 只留在本机 Node(BFF)进程,浏览器永不接触。控制面未起时页面显示友好错误提示。

## 架构

```
浏览器 ──/api/*──▶ Next.js BFF(Route Handler,持 Token + 计时)──Bearer──▶ sandbox-api :8000
                                                                          ├─ /admin/*(聚合视图,本项目新增)
                                                                          └─ /sandboxes/*(创建/操作)
```

聚合视图(`/admin/sandboxes`、`/admin/nodes`、`/admin/stats`、`/admin/events`)是本项目在
`sandbox-api` 侧新增的只读 endpoint(见 `REQUIREMENTS.md` 第 4 节)。

## 验证

```bash
npm run build                              # 前端类型检查 + 构建
python3 ../sandbox-api/smoke_test.py       # 后端聚合 endpoint 单测(moto mock)
```
