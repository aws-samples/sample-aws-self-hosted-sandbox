import type { NodeInfo } from "@/lib/types";
import { fmtMib, fmtRelative } from "@/lib/format";

export function NodeCard({ node }: { node: NodeInfo }) {
  return (
    <div className="card">
      <div className="between">
        <span className="mono" style={{ fontWeight: 600 }}>
          {node.node_id}
        </span>
        <span className="faint mono">{node.ip}</span>
      </div>
      <div className="kv" style={{ marginTop: 12, gridTemplateColumns: "110px 1fr" }}>
        <dt>空闲内存</dt>
        <dd>{fmtMib(node.free_mem_mib)}</dd>
        <dt>运行 VM</dt>
        <dd>{node.vm_count ?? 0}</dd>
        <dt>最近心跳</dt>
        <dd>{fmtRelative(node.last_seen)}</dd>
      </div>
    </div>
  );
}
