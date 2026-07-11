"use client";

import { useRef, useState } from "react";
import type { ApiCallResult } from "@/lib/types";

// 沙盒文件上传/下载(经控制面 base64 over exec)。适合中小文件(代码/产物/配置)。
export function FileTransfer({ sid, running }: { sid: string; running: boolean }) {
  const [uploadPath, setUploadPath] = useState("/root/");
  const [downloadPath, setDownloadPath] = useState("/root/");
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState<"up" | "down" | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const toB64 = (buf: ArrayBuffer) => {
    const bytes = new Uint8Array(buf);
    let bin = "";
    for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin);
  };

  const upload = async () => {
    const f = fileRef.current?.files?.[0];
    if (!f) {
      setMsg("请先选择文件");
      return;
    }
    setBusy("up");
    setMsg(null);
    try {
      const b64 = toB64(await f.arrayBuffer());
      // 目标路径:以 / 结尾则拼文件名,否则用作完整路径
      const dest = uploadPath.endsWith("/") ? uploadPath + f.name : uploadPath;
      const res = await fetch(
        `/api/sandboxes/${sid}/files?path=${encodeURIComponent(dest)}`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ content_b64: b64 }),
        },
      );
      const j = (await res.json()) as ApiCallResult<{ bytes?: number }>;
      setMsg(
        j.ok
          ? `✓ 已上传 ${f.name} → ${dest}(${j.body?.bytes ?? "?"} 字节,${j.elapsed_ms}ms)`
          : `✗ 上传失败:${j.error || (j.body as any)?.error || j.status}`,
      );
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(null);
    }
  };

  const download = async () => {
    if (!downloadPath || downloadPath.endsWith("/")) {
      setMsg("请填写要下载的文件完整路径");
      return;
    }
    setBusy("down");
    setMsg(null);
    try {
      const res = await fetch(
        `/api/sandboxes/${sid}/files?path=${encodeURIComponent(downloadPath)}`,
        { cache: "no-store" },
      );
      const j = (await res.json()) as ApiCallResult<{ content_b64?: string }>;
      if (!j.ok || !j.body?.content_b64) {
        setMsg(`✗ 下载失败:${j.error || (j.body as any)?.error || j.status}`);
        return;
      }
      // base64 → Blob → 触发浏览器下载
      const bin = atob(j.body.content_b64);
      const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      const blob = new Blob([bytes], { type: "application/octet-stream" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = downloadPath.split("/").pop() || "download";
      a.click();
      URL.revokeObjectURL(url);
      setMsg(`✓ 已下载 ${downloadPath}(${j.elapsed_ms}ms)`);
    } catch (e) {
      setMsg(`✗ ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div>
      {/* 上传 */}
      <div className="field-label">上传到沙盒(目标目录/路径)</div>
      <div className="row" style={{ gap: 8, marginBottom: 8 }}>
        <input
          style={{ flex: 1 }}
          value={uploadPath}
          onChange={(e) => setUploadPath(e.target.value)}
          placeholder="/root/ 或 /root/app.py"
        />
      </div>
      <div className="row" style={{ gap: 8, alignItems: "center" }}>
        <input type="file" ref={fileRef} style={{ flex: 1 }} />
        <button className="btn btn-sm" disabled={!running || busy === "up"} onClick={upload}>
          {busy === "up" ? "上传中…" : "上传"}
        </button>
      </div>

      {/* 下载 */}
      <div className="field-label" style={{ marginTop: 16 }}>
        从沙盒下载(文件完整路径)
      </div>
      <div className="row" style={{ gap: 8 }}>
        <input
          style={{ flex: 1 }}
          value={downloadPath}
          onChange={(e) => setDownloadPath(e.target.value)}
          placeholder="/root/output.txt"
        />
        <button className="btn btn-sm" disabled={!running || busy === "down"} onClick={download}>
          {busy === "down" ? "下载中…" : "下载"}
        </button>
      </div>

      {msg ? (
        <div className="mono" style={{ fontSize: 12, marginTop: 10, color: msg.startsWith("✓") ? "var(--green)" : "var(--red)" }}>
          {msg}
        </div>
      ) : null}
      <div className="faint" style={{ fontSize: 12, marginTop: 8 }}>
        走 base64 over exec,适合中小文件(≤10MB)。大文件建议经端口暴露 + guest 内 http。
      </div>
    </div>
  );
}
