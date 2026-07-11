import { callApi, toResponse, API_URL } from "../_lib/client";

// GET /api/cluster → 代理 GET /admin/cluster(NLB hostname 等,供拼接端口暴露 URL)
//
// 端口暴露 URL(/s/{id}/{port}/)由控制面提供,不在 Portal(:3000)上。
// 若集群没配 NLB(proxy_base 为空),浏览器无法用相对路径命中控制面 → 回退到
// SANDBOX_API_URL(本地 port-forward 的 http://localhost:18000,浏览器可直达),
// 让"打开终端 / Demo Web"链接指向正确的控制面。
export async function GET() {
  const result = await callApi<{ proxy_base?: string }>("/admin/cluster");
  if (result.ok && result.body && !result.body.proxy_base) {
    result.body.proxy_base = API_URL; // 回退到控制面地址
  }
  return toResponse(result);
}
