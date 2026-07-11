import { callApi, toResponse } from "../_lib/client";

// GET /api/sandboxes → 全租户总览(代理 GET /admin/sandboxes)
export async function GET() {
  return toResponse(await callApi("/admin/sandboxes"));
}

// POST /api/sandboxes → 创建沙盒(代理 POST /sandboxes)
export async function POST(req: Request) {
  const body = await req.json().catch(() => ({}));
  return toResponse(
    await callApi("/sandboxes", { method: "POST", body, timeoutMs: 60_000 }),
  );
}
