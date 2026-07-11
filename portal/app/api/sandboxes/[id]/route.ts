import { callApi, toResponse } from "../../_lib/client";

// GET /api/sandboxes/{id} → 单沙盒详情
export async function GET(
  _req: Request,
  { params }: { params: { id: string } },
) {
  return toResponse(await callApi(`/sandboxes/${params.id}`));
}

// DELETE /api/sandboxes/{id} → 销毁
export async function DELETE(
  _req: Request,
  { params }: { params: { id: string } },
) {
  return toResponse(
    await callApi(`/sandboxes/${params.id}`, { method: "DELETE" }),
  );
}
