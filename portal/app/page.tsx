"use client";

import { usePolling } from "@/lib/usePolling";
import { StatCard } from "@/components/StatCard";
import { SandboxTable } from "@/components/SandboxTable";
import { NodeCard } from "@/components/NodeCard";
import { Timeline } from "@/components/Timeline";
import { fmtMib } from "@/lib/format";
import type { Stats, Sandbox, NodeInfo, SandboxEvent } from "@/lib/types";

export default function DashboardPage() {
  const stats = usePolling<Stats>("/api/stats", 5000);
  const sandboxes = usePolling<{ sandboxes: Sandbox[] }>("/api/sandboxes", 5000);
  const nodes = usePolling<{ nodes: NodeInfo[] }>("/api/nodes", 10000);
  const events = usePolling<{ events: SandboxEvent[] }>("/api/events?limit=15", 10000);

  const s = stats.data;
  const connError = stats.error || sandboxes.error;

  return (
    <div>
      <h1 className="page-title">Dashboard</h1>
      <p className="page-sub">所有沙盒的运行总览 · 每 5s 刷新</p>

      {connError ? <div className="banner-err">{connError}</div> : null}

      {/* 汇总卡片 */}
      <div className="grid stat-grid">
        <StatCard label="沙盒总数" value={s?.total_sandboxes ?? "—"} />
        <StatCard
          label="运行中"
          value={s?.by_state?.running ?? 0}
          hint={`挂起 ${s?.by_state?.suspended ?? 0} · 失败 ${s?.by_state?.failed ?? 0}`}
        />
        <StatCard label="活节点" value={s?.node_count ?? "—"} />
        <StatCard
          label="集群空闲内存"
          value={s ? fmtMib(s.cluster_free_mem_mib) : "—"}
          hint={`运行 VM ${s?.running_vm_count ?? 0}`}
        />
        <StatCard label="暖池水位" value={s?.warm_pool ?? "—"} hint="预热待命" />
      </div>

      {/* 沙盒表格 */}
      <div className="section-title">沙盒</div>
      {sandboxes.loading && !sandboxes.data ? (
        <div className="spinner">加载中…</div>
      ) : (
        <SandboxTable sandboxes={sandboxes.data?.sandboxes ?? []} />
      )}

      {/* 节点 + 事件时间线 */}
      <div className="row" style={{ marginTop: 26, alignItems: "flex-start" }}>
        <div style={{ flex: 1.3 }}>
          <div className="section-title" style={{ marginTop: 0 }}>
            节点
          </div>
          <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(240px, 1fr))" }}>
            {(nodes.data?.nodes ?? []).map((n) => (
              <NodeCard key={n.node_id} node={n} />
            ))}
            {!nodes.data?.nodes?.length ? <div className="empty">无活节点心跳。</div> : null}
          </div>
        </div>
        <div style={{ flex: 1 }}>
          <div className="section-title" style={{ marginTop: 0 }}>
            近期事件
          </div>
          <div className="card">
            <Timeline events={events.data?.events ?? []} />
          </div>
        </div>
      </div>
    </div>
  );
}
