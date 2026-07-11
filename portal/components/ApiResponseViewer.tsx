import type { ApiCallResult } from "@/lib/types";
import { fmtBytes, fmtSecs } from "@/lib/format";

// 从响应 body 里拎出平台的关键指标(快照/耗时/exec 结果),做成高亮摘要条。
// suspend/resume/create 的核心数据(如 diff 快照只写几 MB、恢复亚秒)藏在一大坨 JSON 里
// 不显眼,这里把它们提到最上方一眼可见 —— 这正是本平台最亮眼的 demo 点。
interface Metric {
  label: string;
  value: string;
}

function extractMetrics(body: unknown): Metric[] {
  if (!body || typeof body !== "object") return [];
  const b = body as Record<string, unknown>;
  const m: Metric[] = [];
  const has = (k: string) => b[k] !== undefined && b[k] !== null && b[k] !== "";

  if (has("state")) m.push({ label: "状态", value: String(b.state) });
  if (has("snapshot_type")) m.push({ label: "快照类型", value: String(b.snapshot_type) });
  if (has("snapshot_actual_bytes"))
    m.push({ label: "实际写入", value: fmtBytes(b.snapshot_actual_bytes as number | string) });
  if (has("snapshot_size_bytes"))
    m.push({ label: "逻辑大小", value: fmtBytes(b.snapshot_size_bytes as number | string) });
  if (has("snapshot_create_time_s"))
    m.push({ label: "打快照耗时", value: fmtSecs(b.snapshot_create_time_s as number | string) });
  if (has("restore_time_s"))
    m.push({ label: "恢复耗时", value: fmtSecs(b.restore_time_s as number | string) });
  if (has("merge_time_s"))
    m.push({ label: "合并耗时", value: fmtSecs(b.merge_time_s as number | string) });
  // exec 结果
  if (b.rc !== undefined) m.push({ label: "退出码", value: String(b.rc) });
  return m;
}

// 展示一次 API 调用的完整 response:method+path、HTTP status、耗时、格式化 JSON。
// 这是 Playground 的核心组件,直接呈现平台的性能指标(create/resume/snapshot 耗时)。
export function ApiResponseViewer({ result }: { result: ApiCallResult | null }) {
  if (!result) {
    return (
      <div className="empty">发起一次调用,响应与耗时会显示在这里。</div>
    );
  }

  const statusOk = result.ok;
  const statusColor = result.status === 0 ? "var(--red)" : statusOk ? "var(--green)" : "var(--amber)";
  const metrics = extractMetrics(result.body);

  return (
    <div>
      <div className="meta-row">
        <span className="chip">
          <b>{result.method}</b>&nbsp;<span className="dim">{result.path}</span>
        </span>
        <span className="chip" style={{ color: statusColor }}>
          {result.status === 0 ? "ERR" : result.status}
        </span>
        <span className="chip chip-strong">{result.elapsed_ms} ms</span>
      </div>
      {result.error ? <div className="banner-err">{result.error}</div> : null}
      {metrics.length ? (
        <div className="metric-bar">
          {metrics.map((mm) => (
            <div className="metric" key={mm.label}>
              <span className="metric-label">{mm.label}</span>
              <span className="metric-value">{mm.value}</span>
            </div>
          ))}
        </div>
      ) : null}
      <pre className="code-block">
        {JSON.stringify(result.body ?? { note: "empty body" }, null, 2)}
      </pre>
    </div>
  );
}
