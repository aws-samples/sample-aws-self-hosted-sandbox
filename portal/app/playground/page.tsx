"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { ApiResponseViewer } from "@/components/ApiResponseViewer";
import type { ApiCallResult } from "@/lib/types";

interface ImageOption {
  name: string;
  desc: string;
}

type Op = "create" | "get" | "suspend" | "resume" | "exec" | "destroy";

const OPS: { value: Op; label: string; method: string; needsId: boolean }[] = [
  { value: "create", label: "创建沙盒", method: "POST /sandboxes", needsId: false },
  { value: "get", label: "查询沙盒", method: "GET /sandboxes/{id}", needsId: true },
  { value: "exec", label: "执行命令", method: "POST /sandboxes/{id}/exec", needsId: true },
  { value: "suspend", label: "挂起(快照)", method: "POST /sandboxes/{id}/suspend", needsId: true },
  { value: "resume", label: "恢复", method: "POST /sandboxes/{id}/resume", needsId: true },
  { value: "destroy", label: "销毁", method: "DELETE /sandboxes/{id}", needsId: true },
];

export default function PlaygroundPage() {
  const [op, setOp] = useState<Op>("create");
  const [id, setId] = useState("");
  const [image, setImage] = useState("");
  const [images, setImages] = useState<ImageOption[]>([]);
  const [cpu, setCpu] = useState(2);
  const [memMib, setMemMib] = useState(4096);
  const [ports, setPorts] = useState(""); // 逗号分隔的暴露端口,如 "8080,3000"
  const [autoSleep, setAutoSleep] = useState(false); // 自动休眠/唤醒(默认关,opt-in)
  const [cmd, setCmd] = useState("echo hello from sandbox");
  const [result, setResult] = useState<ApiCallResult | null>(null);
  const [busy, setBusy] = useState(false);

  // 拉可用镜像/rootfs 模板列表(供下拉)
  useEffect(() => {
    fetch("/api/images", { cache: "no-store" })
      .then((r) => r.json())
      .then((j) => {
        if (j?.ok && j.body?.images) setImages(j.body.images as ImageOption[]);
      })
      .catch(() => {});
  }, []);

  const current = OPS.find((o) => o.value === op)!;

  const run = async () => {
    setBusy(true);
    try {
      let res: Response;
      switch (op) {
        case "create": {
          const services = ports
            .split(",")
            .map((p) => p.trim())
            .filter(Boolean)
            .map((p) => ({
              port: Number(p),
              // 自动休眠开启时,给每个声明端口也带上 autostop/autostart(Fly 语义)
              ...(autoSleep ? { autostop: true, autostart: true } : {}),
            }))
            .filter((s) => Number.isFinite(s.port) && s.port > 0);
          res = await fetch("/api/sandboxes", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              image: image || undefined,
              cpu,
              mem_mib: memMib,
              services: services.length ? services : undefined,
              // meta 开关:不依赖端口声明也能启用自动休眠/唤醒(后端 _autostop/_autostart_enabled 认它)
              ...(autoSleep ? { meta: { auto_sleep: true, auto_wake: true } } : {}),
            }),
          });
          break;
        }
        case "get":
          res = await fetch(`/api/sandboxes/${id}`, { cache: "no-store" });
          break;
        case "destroy":
          res = await fetch(`/api/sandboxes/${id}`, { method: "DELETE" });
          break;
        case "exec":
          res = await fetch(`/api/sandboxes/${id}/exec`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ cmd }),
          });
          break;
        default: // suspend / resume
          res = await fetch(`/api/sandboxes/${id}/${op}`, { method: "POST" });
      }
      const json = (await res.json()) as ApiCallResult;
      setResult(json);
      // create 成功后自动回填 id,方便串起后续操作
      if (op === "create" && json.ok && json.body && typeof json.body === "object") {
        const newId = (json.body as { id?: string }).id;
        if (newId) setId(newId);
      }
    } catch (e) {
      setResult({
        ok: false,
        status: 0,
        elapsed_ms: 0,
        method: current.method,
        path: "",
        body: null,
        error: e instanceof Error ? e.message : String(e),
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <h1 className="page-title">API Playground</h1>
      <p className="page-sub">选操作 → 填参数 → 发起,右侧实时展示 response 与耗时</p>

      <div className="row" style={{ alignItems: "flex-start" }}>
        {/* 左:表单 */}
        <div className="card" style={{ flex: 1, maxWidth: 420 }}>
          <label className="field">
            <span className="field-label">操作</span>
            <select value={op} onChange={(e) => setOp(e.target.value as Op)}>
              {OPS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label} — {o.method}
                </option>
              ))}
            </select>
          </label>

          {current.needsId ? (
            <label className="field">
              <span className="field-label">沙盒 ID</span>
              <input
                value={id}
                onChange={(e) => setId(e.target.value)}
                placeholder="例如 a1b2c3d4"
              />
            </label>
          ) : null}

          {op === "create" ? (
            <>
              <label className="field">
                <span className="field-label">镜像 / rootfs 模板</span>
                {images.length ? (
                  <select value={image} onChange={(e) => setImage(e.target.value)}>
                    <option value="">默认(min)</option>
                    {images.map((im) => (
                      <option key={im.name} value={im.name}>
                        {im.name}
                        {im.desc ? ` — ${im.desc}` : ""}
                      </option>
                    ))}
                  </select>
                ) : (
                  <input
                    value={image}
                    onChange={(e) => setImage(e.target.value)}
                    placeholder="如 web / min(留空用默认)"
                  />
                )}
              </label>
              <div className="row">
                <label className="field" style={{ flex: 1 }}>
                  <span className="field-label">vCPU</span>
                  <input
                    type="number"
                    value={cpu}
                    min={1}
                    onChange={(e) => setCpu(Number(e.target.value))}
                  />
                </label>
                <label className="field" style={{ flex: 1 }}>
                  <span className="field-label">内存 (MiB)</span>
                  <input
                    type="number"
                    value={memMib}
                    min={128}
                    step={128}
                    onChange={(e) => setMemMib(Number(e.target.value))}
                  />
                </label>
              </div>
              <label className="field">
                <span className="field-label">暴露端口(逗号分隔,可选)</span>
                <input
                  value={ports}
                  onChange={(e) => setPorts(e.target.value)}
                  placeholder="例如 8080,3000 — 创建后详情页给出可点击 URL"
                />
              </label>
              <label
                className="field"
                style={{ flexDirection: "row", alignItems: "center", gap: 8, cursor: "pointer" }}
              >
                <input
                  type="checkbox"
                  checked={autoSleep}
                  onChange={(e) => setAutoSleep(e.target.checked)}
                  style={{ width: "auto", margin: 0 }}
                />
                <span className="field-label" style={{ margin: 0 }}>
                  自动休眠 / 唤醒(空闲自动 sleep,来请求自动醒)
                </span>
              </label>
            </>
          ) : null}

          {op === "exec" ? (
            <label className="field">
              <span className="field-label">命令</span>
              <textarea rows={3} value={cmd} onChange={(e) => setCmd(e.target.value)} />
            </label>
          ) : null}

          <button
            className="btn btn-primary"
            style={{ width: "100%" }}
            disabled={busy || (current.needsId && !id)}
            onClick={run}
          >
            {busy ? "请求中…" : `发起 ${current.method}`}
          </button>

          {result?.ok && op === "create" && id ? (
            <div style={{ marginTop: 12, fontSize: 12 }}>
              <Link href={`/sandboxes/${id}`} style={{ color: "var(--accent)" }}>
                → 打开沙盒 {id} 详情页
              </Link>
            </div>
          ) : null}
        </div>

        {/* 右:响应 */}
        <div className="card" style={{ flex: 1.4 }}>
          <div className="section-title" style={{ marginTop: 0 }}>
            Response
          </div>
          <ApiResponseViewer result={result} />
        </div>
      </div>
    </div>
  );
}
