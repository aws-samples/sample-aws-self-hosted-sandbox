// 展示层格式化工具。
//
// 注意:控制面的数值指标(restore_time_s / snapshot_size_bytes / mem_mib 等)可能以
// 字符串形式到达 —— DynamoDB 不支持 float,db.py 的 _sanitize 把 float 存成了 string。
// 所以所有数值格式化都先经 toNum() 归一,避免 "0.0028".toFixed() 这类崩溃。

function toNum(v: unknown): number | null {
  if (v === undefined || v === null || v === "") return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isNaN(n) ? null : n;
}

export function fmtBytes(v?: number | string): string {
  const n = toNum(v);
  if (n === null) return "—";
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let val = n / 1024;
  let i = 0;
  while (val >= 1024 && i < units.length - 1) {
    val /= 1024;
    i++;
  }
  return `${val.toFixed(val < 10 ? 2 : 1)} ${units[i]}`;
}

export function fmtMib(v?: number | string): string {
  const mib = toNum(v);
  if (mib === null) return "—";
  if (mib >= 1024) return `${(mib / 1024).toFixed(1)} GiB`;
  return `${mib} MiB`;
}

export function fmtSecs(v?: number | string): string {
  const s = toNum(v);
  if (s === null) return "—";
  if (s < 1) return `${Math.round(s * 1000)} ms`;
  return `${s.toFixed(2)} s`;
}

export function fmtTime(iso?: string): string {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    return d.toLocaleString();
  } catch {
    return iso;
  }
}

export function fmtRelative(iso?: string): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return iso;
  const diff = Date.now() - then;
  const s = Math.round(diff / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.round(h / 24)}d ago`;
}
