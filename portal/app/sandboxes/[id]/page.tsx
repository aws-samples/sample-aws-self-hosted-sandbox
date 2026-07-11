"use client";

import { useCallback, useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import { StatusBadge } from "@/components/StatusBadge";
import { Timeline } from "@/components/Timeline";
import { ApiResponseViewer } from "@/components/ApiResponseViewer";
import { ExposedServices } from "@/components/ExposedServices";
import { fmtBytes, fmtMib, fmtSecs, fmtTime } from "@/lib/format";
import { demoWebCommand } from "@/lib/demoWeb";
import { termServerCommand, TERMINAL_PORT } from "@/lib/termServer";
import type { ApiCallResult, ClusterInfo, Sandbox, SandboxEvent } from "@/lib/types";

export default function SandboxDetailPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const [sb, setSb] = useState<Sandbox | null>(null);
  const [events, setEvents] = useState<SandboxEvent[]>([]);
  const [proxyBase, setProxyBase] = useState("");
  const [allowAllPorts, setAllowAllPorts] = useState(false);
  const [exposeToken, setExposeToken] = useState("");
  const [loading, setLoading] = useState(true);
  const [notFound, setNotFound] = useState(false);
  const [lastCall, setLastCall] = useState<ApiCallResult | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  // 集群信息(NLB hostname)只取一次,用于拼接端口暴露 URL。
  useEffect(() => {
    fetch("/api/cluster", { cache: "no-store" })
      .then((r) => r.json())
      .then((j) => {
        if (j?.ok && j.body) {
          const c = j.body as ClusterInfo;
          setProxyBase(c.proxy_base || "");
          setAllowAllPorts(!!c.allow_all_ports);
          setExposeToken(c.expose_token || "");
        }
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

  const proxyUrl = (port: number) => {
    const path = `/s/${id}/${port}/`;
    const base = proxyBase ? `${proxyBase}${path}` : path;
    return exposeToken ? `${base}?token=${encodeURIComponent(exposeToken)}` : base;
  };

  const act = async (action: "suspend" | "resume" | "destroy" | "exec" | "demoweb" | "terminal") => {
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
      } else if (action === "demoweb") {
        // 在 guest 里一键起一个好看的演示页,让暴露端口有内容可看。
        // 默认用第一个声明的端口,没声明则默认 8080。
        const port = sb?.services?.[0]?.port ?? 8080;
        res = await fetch(`/api/sandboxes/${id}/exec`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ cmd: demoWebCommand(id, port) }),
        });
      } else if (action === "terminal") {
        // 在 guest 内起 PTY-over-WebSocket 终端服务,起好后新标签打开(经端口暴露反代 + WS 透传)。
        res = await fetch(`/api/sandboxes/${id}/exec`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ cmd: termServerCommand(TERMINAL_PORT) }),
        });
        const j = (await res.json()) as ApiCallResult;
        setLastCall(j);
        if (j.ok) window.open(proxyUrl(TERMINAL_PORT), "_blank", "noreferrer");
        setBusy(null);
        return;
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
          <button
            className="btn btn-sm btn-primary"
            disabled={!!busy || sb?.state !== "running"}
            title="在沙盒内起交互式终端并在新标签打开"
            onClick={() => act("terminal")}
          >
            {busy === "terminal" ? "…" : "打开终端"}
          </button>
          <button
            className="btn btn-sm btn-primary"
            disabled={!!busy || sb?.state !== "running"}
            title="在沙盒内一键起一个演示 web 页,便于展示端口暴露效果"
            onClick={() => act("demoweb")}
          >
            {busy === "demoweb" ? "…" : "启动 Demo Web"}
          </button>
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
              allowAllPorts={allowAllPorts}
              exposeToken={exposeToken}
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
