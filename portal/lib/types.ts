// 与 sandbox-api 的 DynamoDB record 字段对齐(见 sandbox-api/app.py create_sandbox
// 与 drivers/firecracker.py 增补字段)。所有字段均可能缺省,按 optional 处理。

export type SandboxState =
  | "creating"
  | "running"
  | "suspending"
  | "suspended"
  | "resuming"
  | "destroying"
  | "failed"
  | "warm"
  | "orphaned"
  | "needs_reschedule";

export interface ServiceSpec {
  port: number;
  protocol?: string;
  autostop?: boolean;
  autostart?: boolean;
}

export interface Sandbox {
  id: string;
  tenant_id?: string;
  state: SandboxState | string;
  driver?: string;
  image?: string;
  cpu?: number;
  mem_mib?: number;
  created_at?: string;
  updated_at?: string;
  meta?: Record<string, unknown>;
  services?: ServiceSpec[];
  // driver 运行时增补
  node?: string;
  guest_ip?: string;
  tap_idx?: number;
  // 性能/快照指标
  restore_time_s?: number;
  merge_time_s?: number;
  net_fix_ok?: boolean;
  snapshot_type?: string;
  snapshot_size_bytes?: number;
  snapshot_actual_bytes?: number;
  snapshot_create_time_s?: number;
  snapshot_s3?: string;
  pool_state?: string;
  error?: string;
  reconcile_reason?: string;
}

export interface NodeInfo {
  node_id: string;
  ip?: string;
  free_mem_mib?: number;
  vm_count?: number;
  last_seen?: string;
  labels?: Record<string, unknown>;
}

export interface SandboxEvent {
  id: string;
  ts: string;
  event: string;
  prev_state?: string;
  detail?: Record<string, unknown>;
}

export interface Stats {
  total_sandboxes: number;
  by_state: Record<string, number>;
  node_count: number;
  cluster_free_mem_mib: number;
  running_vm_count: number;
  warm_pool: number;
  driver: string;
}

export interface ClusterInfo {
  nlb_hostname: string;
  proxy_base: string; // 端口暴露 URL 前缀,如 http://<nlb>;为空则用相对路径
}

// BFF 统一响应封装:把上游 API 的 status/耗时/body 一起回给前端,
// 直接支撑 Playground 的 "展示 API response + 耗时" 诉求。
export interface ApiCallResult<T = unknown> {
  ok: boolean;
  status: number;        // 上游 HTTP status(0 表示连接失败)
  elapsed_ms: number;    // BFF→上游 往返耗时
  method: string;
  path: string;
  body: T | null;
  error?: string;        // 连接层错误(上游不可达等)
}
