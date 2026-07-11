import { callApi, toResponse } from "../../../_lib/client";

// POST /api/sandboxes/{id}/{action} → 代理 suspend / resume / exec
// exec 需要 body { cmd }; suspend/resume 无 body。
const ALLOWED = new Set(["suspend", "resume", "exec"]);

export async function POST(
  req: Request,
  { params }: { params: { id: string; action: string } },
) {
  const { id, action } = params;
  if (!ALLOWED.has(action)) {
    return Response.json(
      { ok: false, status: 400, error: `unsupported action: ${action}` },
      { status: 400 },
    );
  }
  const body = action === "exec" ? await req.json().catch(() => ({})) : undefined;
  // suspend/resume 是慢操作:suspend 要打全量快照落 EBS、resume 要合并快照恢复,
  // 真机可达数十秒。给足 90s 超时,避免 BFF 提前超时返回 null(后端其实仍在跑)。
  // exec 一般亚秒级,保持 30s。
  const timeoutMs = action === "exec" ? 30_000 : 90_000;
  return toResponse(
    await callApi(`/sandboxes/${id}/${action}`, { method: "POST", body, timeoutMs }),
  );
}
