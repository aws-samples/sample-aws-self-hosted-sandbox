"use client";

import { useCallback, useEffect, useRef, useState } from "react";

// 简单轮询 hook(POC 用,不引入 WebSocket)。
// 返回 { data, error, loading, refresh }。intervalMs=0 关闭自动轮询。
export function usePolling<T>(url: string, intervalMs = 5000) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const fetchOnce = useCallback(async () => {
    try {
      const res = await fetch(url, { cache: "no-store" });
      const json = await res.json();
      // BFF 用 ApiCallResult 包裹上游响应;连接失败时 status=0 + error。
      if (json && typeof json === "object" && "ok" in json) {
        if (!json.ok) {
          setError(json.error || `上游返回 ${json.status}`);
          setData(null);
        } else {
          setError(null);
          setData(json.body as T);
        }
      } else {
        setData(json as T);
        setError(null);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [url]);

  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      if (cancelled) return;
      await fetchOnce();
      if (intervalMs > 0 && !cancelled) {
        timer.current = setTimeout(tick, intervalMs);
      }
    };
    tick();
    return () => {
      cancelled = true;
      if (timer.current) clearTimeout(timer.current);
    };
  }, [fetchOnce, intervalMs]);

  return { data, error, loading, refresh: fetchOnce };
}
