// 覆盖 sandbox-api 的全部 state 枚举。颜色语义:绿=活跃/就绪,琥珀=过渡中,红=异常。
// slept(自动休眠)用靛蓝,与手动 suspended(灰)在视觉上一眼区分:靛="自己睡的,来请求会自动醒"。
const STATE_COLOR: Record<string, string> = {
  running: "var(--green)",
  warm: "var(--blue)",
  suspended: "var(--text-dim)",
  slept: "var(--indigo, #818cf8)",
  creating: "var(--amber)",
  suspending: "var(--amber)",
  resuming: "var(--amber)",
  destroying: "var(--amber)",
  failed: "var(--red)",
  orphaned: "var(--red)",
  needs_reschedule: "var(--red)",
};

export function StatusBadge({ state }: { state: string }) {
  const color = STATE_COLOR[state] || "var(--text-faint)";
  return (
    <span
      className="badge"
      style={{ background: `${color}1a`, borderColor: `${color}55`, color }}
    >
      <span className="badge-dot" style={{ background: color }} />
      {state}
    </span>
  );
}
