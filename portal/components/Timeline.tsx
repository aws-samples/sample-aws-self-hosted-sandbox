import type { SandboxEvent } from "@/lib/types";
import { fmtTime } from "@/lib/format";

export function Timeline({ events }: { events: SandboxEvent[] }) {
  if (!events.length) {
    return <div className="empty">暂无事件。</div>;
  }
  return (
    <ul className="timeline">
      {events.map((e, i) => (
        <li key={`${e.id}-${e.ts}-${i}`}>
          <span className="timeline-dot" />
          <div className="between">
            <span className="timeline-event">{e.event}</span>
            <span className="timeline-ts">{fmtTime(e.ts)}</span>
          </div>
          {e.prev_state ? (
            <div className="faint" style={{ fontSize: 12 }}>
              from <span className="mono">{e.prev_state}</span>
              {e.id ? (
                <>
                  {" · "}
                  <span className="mono">{e.id}</span>
                </>
              ) : null}
            </div>
          ) : null}
          {e.detail && Object.keys(e.detail).length ? (
            <div className="mono faint" style={{ fontSize: 11.5, marginTop: 3 }}>
              {JSON.stringify(e.detail)}
            </div>
          ) : null}
        </li>
      ))}
    </ul>
  );
}
