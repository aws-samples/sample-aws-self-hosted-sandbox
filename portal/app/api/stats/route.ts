import { callApi, toResponse } from "../_lib/client";

// GET /api/stats → 代理 GET /admin/stats(汇总卡片数据)
export async function GET() {
  return toResponse(await callApi("/admin/stats"));
}
