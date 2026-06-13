"""
KataDriver — 通过 K8s Python client 管理 Kata microVM Pod。
集群内运行时自动使用 in-cluster config;集群外使用 kubeconfig。
"""
from __future__ import annotations

import os
import subprocess

from sandbox_api.driver import Capabilities, SandboxSpec, UnsupportedOperation

# K8s client — 延迟初始化(避免导入时崩溃)
_k8s_core = None
_k8s_net  = None

def _init_k8s():
    global _k8s_core, _k8s_net
    if _k8s_core is not None:
        return
    try:
        from kubernetes import client, config as k8s_config
        try:
            k8s_config.load_incluster_config()   # 集群内
        except Exception:
            k8s_config.load_kube_config()        # 本地 kubeconfig
        _k8s_core = client.CoreV1Api()
        _k8s_net  = client.NetworkingV1Api()
    except ImportError:
        pass  # 回退到 kubectl subprocess

NAMESPACE      = os.environ.get("K8S_NAMESPACE", "default")
RUNTIME_CLASS  = os.environ.get("KATA_RUNTIME_CLASS", "kata-qemu")
DOMAIN         = os.environ.get("SANDBOX_DOMAIN", "sbx.example.com")
IMAGE_DEFAULT  = os.environ.get("SANDBOX_IMAGE", "")
LITELLM_URL    = os.environ.get("LITELLM_URL", "http://litellm.litellm.svc.cluster.local:4000")
APP_LABEL      = "claude-sbx-ctrl"


class KataDriver:

    def capabilities(self) -> Capabilities:
        return Capabilities(suspend_resume=False, warm_pool=True, migrate=False)

    # ------------------------------------------------------------------
    # create
    # ------------------------------------------------------------------

    def create(self, sandbox_id: str, spec: SandboxSpec) -> dict:
        _init_k8s()
        manifest = self._render_manifest(sandbox_id, spec)
        if _k8s_core is not None:
            # Python k8s client 방식 (kubectl 불필요)
            import yaml as _yaml
            docs = list(_yaml.safe_load_all(manifest))
            for doc in docs:
                if not doc:
                    continue
                kind = doc.get("kind", "")
                try:
                    if kind == "Pod":
                        _k8s_core.create_namespaced_pod(NAMESPACE, doc)
                    elif kind == "Service":
                        _k8s_core.create_namespaced_service(NAMESPACE, doc)
                    elif kind == "Ingress":
                        _k8s_net.create_namespaced_ingress(NAMESPACE, doc)
                except Exception as e:
                    if "already exists" not in str(e):
                        raise
        else:
            rc, out, err = _kubectl(["apply", "-f", "-"], stdin=manifest)
            if rc != 0:
                raise RuntimeError(f"kubectl apply failed: {err}")
        return {"pod_name": f"sbx-{sandbox_id}"}

    # ------------------------------------------------------------------
    # destroy
    # ------------------------------------------------------------------

    def destroy(self, sandbox_id: str, record: dict) -> None:
        _init_k8s()
        label = f"sandboxId={sandbox_id}"
        if _k8s_core is not None:
            from kubernetes.client.exceptions import ApiException
            for fn in [
                lambda: _k8s_core.delete_collection_namespaced_pod(NAMESPACE, label_selector=label),
                lambda: _k8s_core.delete_collection_namespaced_service(NAMESPACE, label_selector=label),
                lambda: _k8s_net.delete_collection_namespaced_ingress(NAMESPACE, label_selector=label),
            ]:
                try:
                    fn()
                except ApiException:
                    pass
        else:
            _kubectl(["delete", "pod,svc,ingress", "-l", label, "--ignore-not-found"])

    # ------------------------------------------------------------------
    # suspend / resume — v1 不支持
    # ------------------------------------------------------------------

    def suspend(self, sandbox_id: str, record: dict) -> dict:
        raise UnsupportedOperation(
            "kata driver does not support suspend in v1 "
            "(Kata QMP socket is kata-runtime private; no turnkey snapshot API)"
        )

    def resume(self, sandbox_id: str, record: dict) -> dict:
        raise UnsupportedOperation("kata driver does not support resume in v1")

    # ------------------------------------------------------------------
    # exec
    # ------------------------------------------------------------------

    def exec(self, sandbox_id: str, record: dict, cmd: str) -> tuple[int, str, str]:
        _init_k8s()
        if _k8s_core is not None:
            from kubernetes.stream import stream
            resp = stream(
                _k8s_core.connect_get_namespaced_pod_exec,
                f"sbx-{sandbox_id}", NAMESPACE,
                command=["sh", "-c", cmd],
                stderr=True, stdin=False, stdout=True, tty=False,
                _preload_content=False,
            )
            stdout, stderr = "", ""
            while resp.is_open():
                resp.update(timeout=1)
                if resp.peek_stdout():
                    stdout += resp.read_stdout()
                if resp.peek_stderr():
                    stderr += resp.read_stderr()
            rc = resp.returncode if resp.returncode is not None else 0
            return rc, stdout, stderr
        else:
            rc, out, err = _kubectl(["exec", f"sbx-{sandbox_id}", "--", "sh", "-c", cmd])
            return rc, out, err

    # ------------------------------------------------------------------
    # get_runtime_state
    # ------------------------------------------------------------------

    def get_runtime_state(self, sandbox_id: str, record: dict) -> str:
        _init_k8s()
        if _k8s_core is not None:
            try:
                pod = _k8s_core.read_namespaced_pod(f"sbx-{sandbox_id}", NAMESPACE)
                phase_map = {"Running": "running", "Pending": "creating",
                             "Succeeded": "stopped", "Failed": "failed"}
                return phase_map.get(pod.status.phase or "", "unknown")
            except Exception:
                return "unknown"
        else:
            rc, out, _ = _kubectl(["get", "pod", f"sbx-{sandbox_id}",
                                    "-o", "jsonpath={.status.phase}"])
            if rc != 0:
                return "unknown"
            phase_map = {"Running": "running", "Pending": "creating",
                         "Succeeded": "stopped", "Failed": "failed"}
            return phase_map.get(out.strip(), "unknown")

    # ------------------------------------------------------------------
    # Manifest 渲染
    # ------------------------------------------------------------------

    def _render_manifest(self, sid: str, spec: SandboxSpec) -> str:
        image = spec.image or IMAGE_DEFAULT

        # env:走 LiteLLM,不注入任何 AWS 凭据
        env_block = [
            f"    - {{ name: ANTHROPIC_BASE_URL, value: \"{LITELLM_URL}\" }}",
            f"    - {{ name: ANTHROPIC_MODEL, value: \"{spec.env.get('ANTHROPIC_MODEL', 'claude-opus-4-8')}\" }}",
        ]
        # 每租户 virtual key(由控制面在创建沙盒时从 Secrets Manager 取)
        if "LLM_TOKEN" in spec.env:
            env_block.append(
                f"    - {{ name: ANTHROPIC_AUTH_TOKEN,"
                f" value: \"{spec.env['LLM_TOKEN']}\" }}"
            )
        for k, v in spec.env.items():
            if k not in ("ANTHROPIC_MODEL", "LLM_TOKEN"):
                env_block.append(f"    - {{ name: {k}, value: \"{v}\" }}")

        # 暴露端口
        ports_block = "\n".join(
            f"    - {{ containerPort: {s.port} }}" for s in spec.services
        ) or "    - { containerPort: 8080 }"

        pod = f"""
apiVersion: v1
kind: Pod
metadata:
  name: sbx-{sid}
  labels: {{ app: {APP_LABEL}, sandboxId: "{sid}" }}
spec:
  runtimeClassName: {RUNTIME_CLASS}
  nodeSelector: {{ sandbox: "true" }}
  tolerations:
    - {{ key: kata-dedicated, operator: Exists, effect: NoSchedule }}
  containers:
  - name: agent
    image: {image}
    command: ["sleep", "infinity"]
    ports:
{ports_block}
    resources:
      requests: {{ cpu: "{spec.cpu}", memory: "{spec.mem_mib}Mi" }}
      limits:   {{ cpu: "{spec.cpu * 2}", memory: "{spec.mem_mib * 2}Mi" }}
    env:
{chr(10).join(env_block)}
"""
        # Service
        svc = f"""
apiVersion: v1
kind: Service
metadata:
  name: sbx-{sid}
  labels: {{ app: {APP_LABEL} }}
spec:
  type: ClusterIP
  selector: {{ sandboxId: "{sid}" }}
  ports: [{{ port: 8080, targetPort: 8080 }}]
"""
        # Ingress(每个 service 一条 rule)
        if spec.services:
            rules = "\n".join(self._ingress_rule(sid, s) for s in spec.services)
        else:
            rules = self._ingress_rule(sid, None)

        ingress = f"""
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: sbx-{sid}
  labels: {{ app: {APP_LABEL} }}
spec:
  ingressClassName: nginx
  rules:
{rules}
"""
        return pod + "---\n" + svc + "---\n" + ingress

    def _ingress_rule(self, sid: str, svc) -> str:
        port = svc.port if svc else 8080
        host = f"{port}-{sid}.{DOMAIN}"
        return f"""  - host: {host}
    http:
      paths:
      - path: /
        pathType: Prefix
        backend: {{ service: {{ name: sbx-{sid}, port: {{ number: {port} }} }} }}"""


# ------------------------------------------------------------------
# kubectl helper
# ------------------------------------------------------------------

def _kubectl(args: list[str], stdin: str | None = None) -> tuple[int, str, str]:
    p = subprocess.run(
        ["kubectl", "-n", NAMESPACE, *args],
        input=stdin, capture_output=True, text=True, timeout=120,
    )
    return p.returncode, p.stdout.strip(), p.stderr.strip()
