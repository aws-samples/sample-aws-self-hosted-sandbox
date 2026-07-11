import { callApi, toResponse } from "../_lib/client";

// GET /api/cluster → 代理 GET /admin/cluster(NLB hostname 等,供拼接端口暴露 URL)
export async function GET() {
  return toResponse(await callApi("/admin/cluster"));
}
