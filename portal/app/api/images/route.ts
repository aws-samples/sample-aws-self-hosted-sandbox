import { callApi, toResponse } from "../_lib/client";

// GET /api/images → 代理 GET /admin/images(可用镜像/rootfs 模板列表,供创建表单下拉)
export async function GET() {
  return toResponse(await callApi("/admin/images"));
}
