"use client";

import { useState } from "react";
import type { ServiceSpec } from "@/lib/types";

// 展示沙盒的公开端口,给出可点击的访问 URL。
// URL = {proxyBase}/s/{sid}/{port}/  —— proxyBase 来自 /admin/cluster 的 NLB hostname;
// 未配置 NLB 时(如纯本地 port-forward)回退相对路径。
//
// allowAllPorts(来自后端 ALLOW_ALL_PORTS):任意端口模式下,用户在 guest 内起在任何端口
// 的服务都能访问,无需预声明 → 额外提供一个"打开任意端口"输入框。
export function ExposedServices({
  sid,
  services,
  proxyBase,
  running,
  allowAllPorts,
  exposeToken,
}: {
  sid: string;
  services?: ServiceSpec[];
  proxyBase: string;
  running: boolean;
  allowAllPorts: boolean;
  exposeToken: string;
}) {
  const [customPort, setCustomPort] = useState("");

  const urlFor = (port: number | string) => {
    const path = `/s/${sid}/${port}/`;
    const base = proxyBase ? `${proxyBase}${path}` : path;
    // 鉴权开启时附 ?token=,首次访问会种 Cookie,后续子请求免带。
    return exposeToken ? `${base}?token=${encodeURIComponent(exposeToken)}` : base;
  };

  const declared = services ?? [];

  const openCustom = () => {
    const p = Number(customPort);
    if (!Number.isFinite(p) || p < 1 || p > 65535) return;
    window.open(urlFor(p), "_blank", "noreferrer");
  };

  return (
    <div>
      {declared.length > 0 ? (
        <table>
          <thead>
            <tr>
              <th>端口</th>
              <th>协议</th>
              <th>访问 URL</th>
            </tr>
          </thead>
          <tbody>
            {declared.map((s) => (
              <tr key={s.port}>
                <td className="mono">{s.port}</td>
                <td className="dim">{s.protocol || "tcp"}</td>
                <td>
                  {running ? (
                    <a href={urlFor(s.port)} target="_blank" rel="noreferrer" className="mono" style={{ color: "var(--accent)" }}>
                      {urlFor(s.port)} ↗
                    </a>
                  ) : (
                    <span className="faint mono">{urlFor(s.port)}(沙盒非 running)</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <div className="faint" style={{ fontSize: 13 }}>
          {allowAllPorts
            ? "未预声明端口 —— 任意端口模式下无需声明,直接在下方输入你在沙盒里起服务的端口即可打开。"
            : "未声明公开端口。创建沙盒时在 services 里加端口即可暴露。"}
        </div>
      )}

      {/* 任意端口模式:输入任意端口直接打开 */}
      {allowAllPorts ? (
        <div style={{ marginTop: 14 }}>
          <div className="field-label">打开任意端口(沙盒内起服务后)</div>
          <div className="row" style={{ gap: 8 }}>
            <input
              style={{ maxWidth: 160 }}
              value={customPort}
              onChange={(e) => setCustomPort(e.target.value)}
              placeholder="端口,如 3000"
              onKeyDown={(e) => e.key === "Enter" && openCustom()}
            />
            <button className="btn btn-sm" disabled={!running || !customPort} onClick={openCustom}>
              打开 ↗
            </button>
          </div>
          <div className="faint mono" style={{ fontSize: 12, marginTop: 6 }}>
            {customPort ? urlFor(customPort) : `${proxyBase || ""}/s/${sid}/<port>/`}
          </div>
        </div>
      ) : null}

      {!proxyBase ? (
        <div className="faint" style={{ fontSize: 12, marginTop: 10 }}>
          注:未检测到 NLB(NLB_HOSTNAME 未配置),上面是相对路径。要从公网访问,需部署时启用
          ingress-nginx(共享 NLB)并注入 NLB_HOSTNAME;本地可对控制面 port-forward 后访问
          <span className="mono"> http://localhost:18000{`/s/${sid}/<port>/`}</span>。
        </div>
      ) : null}
    </div>
  );
}
