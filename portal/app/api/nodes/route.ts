import { callApi, toResponse } from "../_lib/client";

// GET /api/nodes → 代理 GET /admin/nodes(活节点水位)
export async function GET() {
  return toResponse(await callApi("/admin/nodes"));
}
