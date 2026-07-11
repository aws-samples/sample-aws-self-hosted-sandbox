import { callApi, toResponse } from "../_lib/client";

// GET /api/events?id=&limit= → 代理 GET /admin/events(事件时间线)
export async function GET(req: Request) {
  const { searchParams } = new URL(req.url);
  const id = searchParams.get("id");
  const limit = searchParams.get("limit") || "100";
  const q = new URLSearchParams({ limit });
  if (id) q.set("id", id);
  return toResponse(await callApi(`/admin/events?${q.toString()}`));
}
