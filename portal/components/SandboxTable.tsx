import Link from "next/link";
import type { Sandbox } from "@/lib/types";
import { StatusBadge } from "./StatusBadge";
import { fmtMib, fmtRelative } from "@/lib/format";

export function SandboxTable({ sandboxes }: { sandboxes: Sandbox[] }) {
  if (!sandboxes.length) {
    return <div className="empty">暂无沙盒。去 API Playground 创建一个。</div>;
  }
  return (
    <div className="card" style={{ padding: 0, overflow: "hidden" }}>
      <table>
        <thead>
          <tr>
            <th>ID</th>
            <th>状态</th>
            <th>镜像</th>
            <th>规格</th>
            <th>节点</th>
            <th>租户</th>
            <th>更新时间</th>
          </tr>
        </thead>
        <tbody>
          {sandboxes.map((s) => (
            <tr key={s.id}>
              <td>
                <Link href={`/sandboxes/${s.id}`} className="mono" style={{ color: "var(--accent)" }}>
                  {s.id}
                </Link>
              </td>
              <td>
                <StatusBadge state={s.state} />
              </td>
              <td className="mono dim">{s.image || "—"}</td>
              <td className="dim">
                {s.cpu ?? "?"} vCPU · {fmtMib(s.mem_mib)}
              </td>
              <td className="mono dim">{s.node || "—"}</td>
              <td className="dim">{s.tenant_id || "—"}</td>
              <td className="faint">{fmtRelative(s.updated_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
