"use client";

import { useCallback, useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import { StatusBadge } from "@/components/StatusBadge";
import { Timeline } from "@/components/Timeline";
import { ApiResponseViewer } from "@/components/ApiResponseViewer";
import { ExposedServices } from "@/components/ExposedServices";
import { fmtBytes, fmtMib, fmtSecs, fmtTime } from "@/lib/format";
import type { ApiCallResult, ClusterInfo, Sandbox, SandboxEvent } from "@/lib/types";

export default function SandboxDetailPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const [sb, setSb] = useState<Sandbox | null>(null);
  const [events, setEvents] = useState<SandboxEvent[]>([]);
  const [proxyBase, setProxyBase] = useState("");
  const [loading, setLoading] = useState(true);
  const [notFound, setNotFound] = useState(false);
  const [lastCall, setLastCall] = useState<ApiCallResult | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  // 集群信息(NLB hostname)只取一次,用于拼接端口暴露 URL。
  useEffect(() => {
    fetch("/api/cluster", { cache: "no-store" })
      .then((r) => r.json())
      .then((j) => {
        if (j?.ok && j.body) setProxyBase((j.body as ClusterInfo).proxy_base || "");
      })
      .catch(() => {});
  }, []);

  const refresh = useCallback(async () => {
    const [r1, r2] = await Promise.all([
      fetch(`/api/sandboxes/${id}`, { cache: "no-store" }).then((r) => r.json()),
      fetch(`/api/events?id=${id}&limit=50`, { cache: "no-store" }).then((r) => r.json()),
    ]);
    if (r1?.ok && r1.body) {
      setSb(r1.body as Sandbox);
      setNotFound(false);
    } else if (r1?.status === 404) {
      setNotFound(true);
    }
    if (r2?.ok && r2.body) setEvents((r2.body as { events: SandboxEvent[] }).events);
    setLoading(false);
  }, [id]);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, [refresh]);

  const act = async (action: "suspend" | "resume" | "destroy" | "exec") => {
    setBusy(action);
    try {
      let res: Response;
      if (action === "destroy") {
        res = await fetch(`/api/sandboxes/${id}`, { method: "DELETE" });
      } else if (action === "exec") {
        const cmd = window.prompt("要执行的命令:", "echo hello from sandbox");
        if (cmd === null) {
          setBusy(null);
          return;
        }
        res = await fetch(`/api/sandboxes/${id}/exec`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ cmd }),
        });
      } else {
        res = await fetch(`/api/sandboxes/${id}/${action}`, { method: "POST" });
      }
      const json = (await res.json()) as ApiCallResult;
      setLastCall(json);
      await refresh();
      if (action === "destroy" && json.ok) {
        setTimeout(() => router.push("/"), 800);
      }
    } finally {
      setBusy(null);
    }
  };

  if (loading) return <div className="spinner">加载中…</div>;
  if (notFound)
    return (
      <div>
        <Link href="/" className="dim">
          ← 返回 Dashboard
        </Link>
        <div className="empty">沙盒 {id} 不存在或已销毁。</div>
      </div>
    );

  return (
    <div>
      <Link href="/" className="dim" style={{ fontSize: 13 }}>
        ← 返回 Dashboard
      </Link>
      <div className="between" style={{ margin: "10px 0 20px" }}>
        <div className="row" style={{ alignItems: "center" }}>
          <h1 className="page-title mono" style={{ margin: 0 }}>
            {id}
          </h1>
          {sb ? <StatusBadge state={sb.state} /> : null}
        </div>
        <div className="btn-row">
          <button className="btn btn-sm" disabled={!!busy} onClick={() => act("exec")}>
            {busy === "exec" ? "…" : "Exec"}
          </button>
          <button className="btn btn-sm" disabled={!!busy} onClick={() => act("suspend")}>
            {busy === "suspend" ? "…" : "Suspend"}
          </button>
          <button className="btn btn-sm" disabled={!!busy} onClick={() => act("resume")}>
            {busy === "resume" ? "…" : "Resume"}
          </button>
          <button
            className="btn btn-sm btn-danger"
            disabled={!!busy}
            onClick={() => act("destroy")}
          >
            {busy === "destroy" ? "…" : "Destroy"}
          </button>
        </div>
      </div>

      <div className="row" style={{ alignItems: "flex-start" }}>
        {/* 左:record 详情 + 指标 */}
        <div style={{ flex: 1.4 }}>
          <div className="card">
            <div className="section-title" style={{ marginTop: 0 }}>
              配置
            </div>
            <dl className="kv">
              <dt>镜像</dt>
              <dd>{sb?.image || "—"}</dd>
              <dt>规格</dt>
              <dd>
                {sb?.cpu ?? "?"} vCPU · {fmtMib(sb?.mem_mib)}
              </dd>
              <dt>租户</dt>
              <dd>{sb?.tenant_id || "—"}</dd>
              <dt>节点</dt>
              <dd>{sb?.node || "—"}</dd>
              <dt>Guest IP</dt>
              <dd>{sb?.guest_ip || "—"}</dd>
              <dt>创建时间</dt>
              <dd>{fmtTime(sb?.created_at)}</dd>
              <dt>更新时间</dt>
              <dd>{fmtTime(sb?.updated_at)}</dd>
              {sb?.error ? (
                <>
                  <dt>错误</dt>
                  <dd style={{ color: "var(--red)" }}>{sb.error}</dd>
                </>
              ) : null}
            </dl>
          </div>

          <div className="card" style={{ marginTop: 14 }}>
            <div className="section-title" style={{ marginTop: 0 }}>
              性能指标
            </div>
            <dl className="kv">
              <dt>Resume 耗时</dt>
              <dd>{fmtSecs(sb?.restore_time_s)}</dd>
              <dt>Merge 耗时</dt>
              <dd>{fmtSecs(sb?.merge_time_s)}</dd>
              <dt>快照类型</dt>
              <dd>{sb?.snapshot_type || "—"}</dd>
              <dt>快照大小</dt>
              <dd>{fmtBytes(sb?.snapshot_size_bytes)}</dd>
              <dt>快照实际字节</dt>
              <dd>{fmtBytes(sb?.snapshot_actual_bytes)}</dd>
              <dt>快照耗时</dt>
              <dd>{fmtSecs(sb?.snapshot_create_time_s)}</dd>
            </dl>
          </div>

          <div className="card" style={{ marginTop: 14 }}>
            <div className="section-title" style={{ marginTop: 0 }}>
              公开服务(端口暴露)
            </div>
            <ExposedServices
              sid={id}
              services={sb?.services}
              proxyBase={proxyBase}
              running={sb?.state === "running"}
            />
          </div>

          {lastCall ? (
            <div className="card" style={{ marginTop: 14 }}>
              <div className="section-title" style={{ marginTop: 0 }}>
                最近一次操作响应
              </div>
              <ApiResponseViewer result={lastCall} />
            </div>
          ) : null}
        </div>

        {/* 右:事件时间线 */}
        <div style={{ flex: 1 }}>
          <div className="section-title" style={{ marginTop: 0 }}>
            事件时间线
          </div>
          <div className="card">
            <Timeline events={events} />
          </div>
        </div>
      </div>
    </div>
  );
}
