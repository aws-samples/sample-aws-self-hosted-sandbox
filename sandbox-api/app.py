#!/usr/bin/env python3
"""
最小沙盒控制平面 API(demo)—— 把 "创建/销毁/列出沙盒" 封装成 REST API。
沙盒 = 一个带 runtimeClassName=kata-qemu 的 K8s Pod(跑进 microVM)+ Service + Ingress。
后端用 kubectl(零依赖,纯标准库)。生产应换成 K8s client SDK 或 kubernetes-sigs/agent-sandbox CRD。

接口:
  POST   /sandboxes              创建沙盒 -> {id, url, status}
  GET    /sandboxes              列出所有沙盒
  GET    /sandboxes/{id}         查单个沙盒
  GET    /sandboxes/{id}/locate  定位沙盒背后的 VMM(节点/进程/后端/socket)
  DELETE /sandboxes/{id}         销毁沙盒
  POST   /sandboxes/{id}/exec    在沙盒内执行命令(演示) body: {"cmd": "..."}

注:无 suspend/resume —— Kata 的 VMM(qmp.sock)被 kata-runtime 独占,外部不能直连做快照;
   Kata-on-K8s 无 turnkey 快照(见 ../Kata快照定位机制.md)。快照能力见 fc_snapshot_api.py(裸 Firecracker)。

跑: python3 app.py   (默认 :8000,用本地 kubeconfig 连 EKS)
"""
import json
import os
import subprocess
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# ---- 配置 ----
IMAGE = os.environ.get("SANDBOX_IMAGE", "<account-id>.dkr.ecr.<region>.amazonaws.com/claude-sbx:poc")
RUNTIME_CLASS = "kata-qemu"          # 用哪个 Kata 后端(发动机);clh 注册后可换 kata-clh
DOMAIN = "sbx.example.com"           # 通配符子域名根
NAMESPACE = "default"
APP_LABEL = "claude-sbx-api"         # 跟手动创建的沙盒区分开


REGION = "us-east-1"


def kubectl(args, stdin=None, timeout=120):
    """调 kubectl,返回 (rc, stdout, stderr)。"""
    p = subprocess.run(["kubectl", "-n", NAMESPACE, *args],
                       input=stdin, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


# 节点侧定位脚本(经 SSM 在沙盒所在节点执行):podUID → sandbox ID → VMM 进程/后端/socket。
# 与 snapshot-agent/locate.py 同源。注意:定位 ≠ 能直连 VMM 做快照(QMP 被 Kata 独占,见 Kata快照定位机制.md)。
_NODE_PROBE = r'''
PODUID="$1"
SBID=$(sudo crictl pods -o json 2>/dev/null | python3 -c '
import sys,json
uid=sys.argv[1]
try: d=json.load(sys.stdin)
except Exception: sys.exit(0)
for p in d.get("items",[]):
    if p.get("labels",{}).get("io.kubernetes.pod.uid")==uid:
        print(p["id"]); break
' "$PODUID" 2>/dev/null)
[ -z "$SBID" ] && SBID=$(sudo pgrep -af containerd-shim-kata-v2 2>/dev/null | grep -oE '[-]id [a-f0-9]{64}' | awk '{print $2}' | head -1)
VMM_LINE=$(sudo pgrep -af 'qemu-system|cloud-hypervisor|firecracker' 2>/dev/null | grep "$SBID" | head -1)
VMM_PID=$(echo "$VMM_LINE" | awk '{print $1}')
case "$VMM_LINE" in
  *qemu-system*)      BACKEND=qemu; IFACE="QMP socket (Kata 独占,不可外部直连)";;
  *cloud-hypervisor*) BACKEND=cloud-hypervisor; IFACE="CH HTTP API";;
  *firecracker*)      BACKEND=firecracker; IFACE="Firecracker REST";;
  *)                  BACKEND=unknown; IFACE="?";;
esac
VMDIR="/run/vc/vm/$SBID"
SOCKETS=$(sudo ls "$VMDIR" 2>/dev/null | tr '\n' ',')
python3 -c '
import json,sys
print(json.dumps({"sandbox_id":sys.argv[1],"vmm_pid":sys.argv[2],"backend":sys.argv[3],
"snapshot_interface":sys.argv[4],"vm_runtime_dir":sys.argv[5],"sockets":sys.argv[6]}))
' "$SBID" "$VMM_PID" "$BACKEND" "$IFACE" "$VMDIR" "$SOCKETS"
'''


def _ssm_run(instance_id, podUID, timeout_polls=20):
    """在节点上经 SSM 跑定位脚本,返回最后一行 JSON dict(失败返回 {})。"""
    import base64, tempfile, os
    b64 = base64.b64encode((_NODE_PROBE + "\n").encode()).decode()
    one = f"echo {b64} | base64 -d > /tmp/_probe.sh && bash /tmp/_probe.sh {podUID}"
    payload = {"InstanceIds": [instance_id], "DocumentName": "AWS-RunShellScript",
               "Parameters": {"commands": [one]}}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(payload, f); pf = f.name
    try:
        r = subprocess.run(["aws", "ssm", "send-command", "--region", REGION,
                            "--cli-input-json", f"file://{pf}",
                            "--query", "Command.CommandId", "--output", "text"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return {"error": r.stderr.strip()}
        cid = r.stdout.strip()
        for _ in range(timeout_polls):
            time.sleep(3)
            g = subprocess.run(["aws", "ssm", "get-command-invocation", "--region", REGION,
                               "--command-id", cid, "--instance-id", instance_id,
                               "--query", "{s:Status,o:StandardOutputContent}", "--output", "json"],
                              capture_output=True, text=True)
            try: res = json.loads(g.stdout)
            except Exception: continue
            if res.get("s") in ("Success", "Failed"):
                lines = [l for l in res.get("o", "").splitlines() if l.strip().startswith("{")]
                return json.loads(lines[-1]) if lines else {"error": "node probe no output"}
        return {"error": "ssm timeout"}
    finally:
        os.unlink(pf)


def locate_sandbox(sid):
    """由沙盒 id 定位到:节点 + VMM 进程 + 后端 + socket。继承自 snapshot-agent/locate.py。"""
    rc, out, err = kubectl(["get", "pod", f"sbx-{sid}",
                            "-o", "jsonpath={.spec.nodeName}|{.metadata.uid}|{.spec.runtimeClassName}"])
    if rc != 0:
        return {"error": err or "pod not found"}
    node, uid, runtime_class = (out.split("|") + ["", "", ""])[:3]
    info = {"id": sid, "node": node, "podUID": uid, "runtimeClass": runtime_class}
    if not runtime_class.startswith("kata"):
        info["note"] = f"非 Kata(runtimeClass={runtime_class}),无独立 VMM"
        return info
    # node(私有 DNS) → EC2 实例 ID
    r = subprocess.run(["aws", "ec2", "describe-instances", "--region", REGION,
                        "--filters", f"Name=private-dns-name,Values={node}",
                        "Name=instance-state-name,Values=running",
                        "--query", "Reservations[].Instances[].InstanceId", "--output", "text"],
                       capture_output=True, text=True)
    inst = r.stdout.strip()
    info["ec2_instance"] = inst
    if inst:
        info.update(_ssm_run(inst, uid))
    return info


def sandbox_manifest(sid):
    """一个沙盒 = Pod(kata microVM) + ClusterIP Service + Ingress(子域名 Host 路由)。"""
    host = f"8080-{sid}.{DOMAIN}"
    return f"""
apiVersion: v1
kind: Pod
metadata:
  name: sbx-{sid}
  labels: {{ app: {APP_LABEL}, sandboxId: "{sid}" }}
spec:
  runtimeClassName: {RUNTIME_CLASS}
  nodeSelector: {{ sandbox: "true" }}
  containers:
  - name: agent
    image: {IMAGE}
    # 主进程起 dev server 占位(代表沙盒内服务);真实场景是 Claude Code agent
    command: ["sh","-c","echo \\"sandbox {sid} ready\\" > /tmp/index.html && cd /tmp && python3 -m http.server 8080"]
    ports:
    - {{ containerPort: 8080 }}
    resources:
      requests: {{ cpu: "1", memory: "2Gi" }}
      limits: {{ cpu: "2", memory: "4Gi" }}
    env:
    - {{ name: CLAUDE_CODE_USE_BEDROCK, value: "1" }}
    - {{ name: AWS_REGION, value: "us-east-1" }}
    - {{ name: ANTHROPIC_MODEL, value: "us.anthropic.claude-opus-4-8" }}
---
apiVersion: v1
kind: Service
metadata:
  name: sbx-{sid}
  labels: {{ app: {APP_LABEL} }}
spec:
  type: ClusterIP
  selector: {{ sandboxId: "{sid}" }}
  ports: [{{ port: 8080, targetPort: 8080 }}]
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: sbx-{sid}
  labels: {{ app: {APP_LABEL} }}
spec:
  ingressClassName: nginx
  rules:
  - host: {host}
    http:
      paths:
      - path: /
        pathType: Prefix
        backend: {{ service: {{ name: sbx-{sid}, port: {{ number: 8080 }} }} }}
"""


def list_sandboxes():
    rc, out, err = kubectl(["get", "pods", "-l", f"app={APP_LABEL}",
                            "-o", "json"])
    if rc != 0:
        return []
    items = json.loads(out).get("items", [])
    result = []
    for p in items:
        sid = p["metadata"]["labels"].get("sandboxId", "?")
        phase = p["status"].get("phase", "Unknown")
        ready = any(c.get("ready") for c in p["status"].get("containerStatuses", []))
        result.append({
            "id": sid,
            "status": phase,
            "ready": ready,
            "url": f"http://8080-{sid}.{DOMAIN}/",
            "node": p["spec"].get("nodeName"),
        })
    return result


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass  # 静音默认日志

    def _read_body(self):
        # 容错:非法 Content-Length 当作 0;请求体非法 JSON 抛 ValueError,由 handler 兜底转 400/500。
        try:
            n = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            n = 0
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_GET(self):
        try:
            self._do_GET()
        except Exception as e:
            self._send(500, {"error": str(e)})

    def do_POST(self):
        try:
            self._do_POST()
        except Exception as e:
            self._send(500, {"error": str(e)})

    def do_DELETE(self):
        try:
            self._do_DELETE()
        except Exception as e:
            self._send(500, {"error": str(e)})

    def _do_GET(self):
        path = urlparse(self.path).path
        parts = path.strip("/").split("/")
        if path == "/sandboxes":
            self._send(200, {"sandboxes": list_sandboxes()})
        elif len(parts) == 3 and parts[0] == "sandboxes" and parts[2] == "locate":
            # 定位:沙盒 → 节点 + VMM 进程 + 后端 + socket(继承自 locate.py)
            self._send(200, locate_sandbox(parts[1]))
        elif path.startswith("/sandboxes/"):
            sid = parts[1]
            sb = [s for s in list_sandboxes() if s["id"] == sid]
            self._send(200 if sb else 404, sb[0] if sb else {"error": "not found"})
        elif path == "/":
            self._send(200, {"service": "sandbox-api", "endpoints": [
                "POST /sandboxes", "GET /sandboxes",
                "GET /sandboxes/{id}", "GET /sandboxes/{id}/locate",
                "DELETE /sandboxes/{id}", "POST /sandboxes/{id}/exec"]})
        else:
            self._send(404, {"error": "not found"})

    def _do_POST(self):
        path = urlparse(self.path).path
        if path == "/sandboxes":
            sid = uuid.uuid4().hex[:8]                  # 唯一 ID(避免同秒并发碰撞)
            rc, out, err = kubectl(["apply", "-f", "-"], stdin=sandbox_manifest(sid))
            if rc != 0:
                return self._send(500, {"error": err})
            self._send(201, {
                "id": sid,
                "status": "creating",
                "url": f"http://8080-{sid}.{DOMAIN}/",
                "note": "Pod(kata microVM)+Service+Ingress 已创建;几秒后 ready",
            })
        elif path.startswith("/sandboxes/") and path.endswith("/exec"):
            sid = path.split("/")[2]
            cmd = self._read_body().get("cmd", "echo no-cmd")
            rc, out, err = kubectl(["exec", f"sbx-{sid}", "--", "sh", "-c", cmd])
            self._send(200 if rc == 0 else 500,
                       {"id": sid, "cmd": cmd, "stdout": out, "stderr": err, "rc": rc})
        else:
            self._send(404, {"error": "not found"})

    def _do_DELETE(self):
        path = urlparse(self.path).path
        if path.startswith("/sandboxes/"):
            sid = path.split("/")[2]
            rc, out, err = kubectl(["delete", "pod,svc,ingress", "-l", f"sandboxId={sid}"])
            self._send(200 if rc == 0 else 500,
                       {"id": sid, "deleted": rc == 0, "detail": out or err})
        else:
            self._send(404, {"error": "not found"})


if __name__ == "__main__":
    print("沙盒 API 在 http://127.0.0.1:8000 (Ctrl-C 退出)")
    ThreadingHTTPServer(("127.0.0.1", 8000), Handler).serve_forever()
