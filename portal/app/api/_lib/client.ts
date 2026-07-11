// BFF 侧 sandbox-api 客户端。
// 只在服务端(Route Handler)运行 —— Bearer Token 只留在本机 Node 进程,浏览器永不接触。
// 每次调用统一计时,把 { status, elapsed_ms, body } 一并回传,直接满足
// portal "展示 API response + 耗时" 的诉求。

import type { ApiCallResult } from "@/lib/types";

const API_URL = process.env.SANDBOX_API_URL || "http://localhost:18000";
const API_KEY = process.env.SANDBOX_API_KEY || "";

interface CallOpts {
  method?: string;
  body?: unknown;
  // 上游超时(ms)。create/resume 可能较慢,给足余量。
  timeoutMs?: number;
}

/**
 * 调 sandbox-api 并返回标准化结果(永不抛异常,连接失败也归一化为 status=0)。
 */
export async function callApi<T = unknown>(
  path: string,
  opts: CallOpts = {},
): Promise<ApiCallResult<T>> {
  const method = opts.method || "GET";
  const url = `${API_URL}${path}`;
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (API_KEY) headers["Authorization"] = `Bearer ${API_KEY}`;

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), opts.timeoutMs ?? 30_000);

  const started = performance.now();
  try {
    const res = await fetch(url, {
      method,
      headers,
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
      signal: controller.signal,
      cache: "no-store",
    });
    const elapsed_ms = Math.round(performance.now() - started);

    // 上游统一返回 JSON;个别情况下(如空 body)容错解析。
    const text = await res.text();
    let parsed: T | null = null;
    try {
      parsed = text ? (JSON.parse(text) as T) : null;
    } catch {
      parsed = text as unknown as T;
    }

    return {
      ok: res.ok,
      status: res.status,
      elapsed_ms,
      method,
      path,
      body: parsed,
    };
  } catch (e) {
    const elapsed_ms = Math.round(performance.now() - started);
    const isAbort = e instanceof Error && e.name === "AbortError";
    return {
      ok: false,
      status: 0,
      elapsed_ms,
      method,
      path,
      body: null,
      error: isAbort
        ? `上游超时(>${opts.timeoutMs ?? 30_000}ms)`
        : `无法连接 sandbox-api(${API_URL}):${e instanceof Error ? e.message : String(e)}。` +
          `请确认已执行 kubectl port-forward 并在 .env.local 配好 SANDBOX_API_URL / SANDBOX_API_KEY。`,
    };
  } finally {
    clearTimeout(timer);
  }
}

/** 把 ApiCallResult 转成 Next 的 Response(保留 elapsed_ms 等元数据供前端展示)。 */
export function toResponse(result: ApiCallResult): Response {
  // 连接失败(status=0)对外用 502,其余透传上游 status。
  const httpStatus = result.status === 0 ? 502 : result.status;
  return Response.json(result, { status: httpStatus });
}

export { API_URL, API_KEY };
