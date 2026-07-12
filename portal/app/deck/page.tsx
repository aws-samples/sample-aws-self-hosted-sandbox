"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { SLIDES } from "@/components/deck/slides";

export default function DeckPage() {
  const [i, setI] = useState(0);
  const n = SLIDES.length;

  const go = useCallback(
    (next: number) => setI((cur) => Math.max(0, Math.min(n - 1, next))),
    [n]
  );

  // 键盘翻页:← / → / 空格 / Home / End
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "ArrowRight" || e.key === " " || e.key === "PageDown") {
        e.preventDefault();
        go(i + 1);
      } else if (e.key === "ArrowLeft" || e.key === "PageUp") {
        e.preventDefault();
        go(i - 1);
      } else if (e.key === "Home") {
        go(0);
      } else if (e.key === "End") {
        go(n - 1);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [i, n, go]);

  const pct = ((i + 1) / n) * 100;

  return (
    <div className="deck">
      {/* 顶部工具条 */}
      <div className="deck-bar">
        <div className="deck-title">
          方案讲解 <span>· AWS 自建 Firecracker Sandbox</span>
        </div>
        <div className="deck-spacer" />
        <button className="btn btn-sm" onClick={() => go(i - 1)} disabled={i === 0}>
          ← 上一页
        </button>
        <div className="deck-counter">
          {i + 1} / {n}
        </div>
        <button className="btn btn-sm" onClick={() => go(i + 1)} disabled={i === n - 1}>
          下一页 →
        </button>
        <Link href="/" className="btn btn-sm" style={{ marginLeft: 6 }}>
          ✕ 退出
        </Link>
      </div>

      {/* 进度条 */}
      <div className="deck-progress">
        <i style={{ width: `${pct}%` }} />
      </div>

      {/* 幻灯片舞台 */}
      <div className="deck-stage">{SLIDES[i].render()}</div>

      {/* 底部圆点导航 */}
      <div className="deck-nav">
        <div className="deck-dots">
          {SLIDES.map((s, idx) => (
            <button
              key={s.id}
              className={idx === i ? "on" : ""}
              title={`第 ${idx + 1} 页`}
              onClick={() => go(idx)}
            />
          ))}
        </div>
      </div>
    </div>
  );
}
