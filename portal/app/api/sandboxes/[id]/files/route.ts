import { callApi, toResponse } from "../../../_lib/client";

// GET  /api/sandboxes/{id}/files?path=  → 下载(返回 content_b64)
// PUT  /api/sandboxes/{id}/files?path=  → 上传(body: {content_b64})
// 均代理到控制面同名 endpoint(base64 over exec 落 guest 文件系统)。

export async function GET(
  req: Request,
  { params }: { params: { id: string } },
) {
  const path = new URL(req.url).searchParams.get("path") || "";
  return toResponse(
    await callApi(`/sandboxes/${params.id}/files?path=${encodeURIComponent(path)}`, {
      timeoutMs: 60_000,
    }),
  );
}

export async function PUT(
  req: Request,
  { params }: { params: { id: string } },
) {
  const path = new URL(req.url).searchParams.get("path") || "";
  const body = await req.json().catch(() => ({}));
  return toResponse(
    await callApi(`/sandboxes/${params.id}/files?path=${encodeURIComponent(path)}`, {
      method: "PUT",
      body,
      timeoutMs: 60_000,
    }),
  );
}
