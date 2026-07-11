import type { ServiceSpec } from "@/lib/types";

// 展示沙盒声明的公开端口,给出可点击的访问 URL。
// URL = {proxyBase}/s/{sid}/{port}/  —— proxyBase 来自 /admin/cluster 的 NLB hostname;
// 未配置 NLB 时(如纯本地 port-forward)回退相对路径,提示需经 NLB 才能公网访问。
export function ExposedServices({
  sid,
  services,
  proxyBase,
  running,
}: {
  sid: string;
  services?: ServiceSpec[];
  proxyBase: string;
  running: boolean;
}) {
  if (!services || services.length === 0) {
    return (
      <div className="faint" style={{ fontSize: 13 }}>
        未声明公开端口。创建沙盒时在 services 里加端口即可暴露。
      </div>
    );
  }

  return (
    <div>
      <table>
        <thead>
          <tr>
            <th>端口</th>
            <th>协议</th>
            <th>访问 URL</th>
          </tr>
        </thead>
        <tbody>
          {services.map((s) => {
            const path = `/s/${sid}/${s.port}/`;
            const url = proxyBase ? `${proxyBase}${path}` : path;
            return (
              <tr key={s.port}>
                <td className="mono">{s.port}</td>
                <td className="dim">{s.protocol || "tcp"}</td>
                <td>
                  {running ? (
                    <a
                      href={url}
                      target="_blank"
                      rel="noreferrer"
                      className="mono"
                      style={{ color: "var(--accent)" }}
                    >
                      {url} ↗
                    </a>
                  ) : (
                    <span className="faint mono">{url}(沙盒非 running)</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
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
