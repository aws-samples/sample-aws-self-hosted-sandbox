import { callApi, toResponse } from "../../_lib/client";

// GET /api/sandboxes/{id} → 单沙盒详情
export async function GET(
  _req: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  const { id } = await params;
  return toResponse(await callApi(`/sandboxes/${id}`));
}

// DELETE /api/sandboxes/{id} → 销毁
export async function DELETE(
  _req: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  const { id } = await params;
  return toResponse(await callApi(`/sandboxes/${id}`, { method: "DELETE" }));
}
