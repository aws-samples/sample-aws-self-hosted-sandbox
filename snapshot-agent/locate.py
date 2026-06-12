#!/usr/bin/env python3
"""
locate.py —— Kata 快照 node-agent 的"定位"环节最小 demo。
输入一个 Pod 名,输出:它在哪台节点、背后的 VMM 进程 PID、控制 socket、后端类型、快照接口。
这是生产 node-agent 的第一步(也是最易被低估的一步)。

两段逻辑:
  [控制平面侧] kubectl 拿 nodeName + podUID  → 决定去哪台节点
  [节点侧]     由 sandbox ID 定位 VMM 进程/socket/后端  (本 demo 在节点上执行的那段 shell)

用法:
  python3 locate.py <pod-name> [-n namespace]
依赖:本机能 kubectl 连集群;节点能用 SSM(脚本自动用 aws ssm 在节点上执行定位)。
"""
import argparse, json, subprocess, sys, time


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)


def kubectl_json(args):
    r = sh(f"kubectl {args} -o json")
    if r.returncode != 0:
        sys.exit(f"kubectl 失败: {r.stderr.strip()}")
    return json.loads(r.stdout)


# ---- 节点侧定位脚本(会被发到节点上执行) ----
# 入参 $1 = podUID。输出 JSON 行,便于控制平面解析。
NODE_PROBE = r'''
PODUID="$1"
# 1) 由 podUID 找 sandbox(pause)容器,再拿 kata shim 的 -id(= sandbox ID)
#    crictl 的 pod 标签里带 uid;不同版本字段略异,这里多路兜底。
SBID=$(sudo crictl pods -o json 2>/dev/null | python3 -c '
import sys,json
uid=sys.argv[1]
try:
    d=json.load(sys.stdin)
except Exception:
    sys.exit(0)
for p in d.get("items",[]):
    labels=p.get("labels",{})
    if labels.get("io.kubernetes.pod.uid")==uid:
        print(p["id"]); break
' "$PODUID" 2>/dev/null)

# 兜底:若 crictl 没匹配到,直接从 kata shim 进程里取(集群只有少量 kata pod 时够用)
if [ -z "$SBID" ]; then
  SBID=$(sudo pgrep -af containerd-shim-kata-v2 2>/dev/null | grep -oE '[-]id [a-f0-9]{64}' | awk '{print $2}' | head -1)
fi

# 2) 由 sandbox ID 定位 VMM 进程 + 后端类型
VMM_LINE=$(sudo pgrep -af 'qemu-system|cloud-hypervisor|firecracker' 2>/dev/null | grep "$SBID" | head -1)
VMM_PID=$(echo "$VMM_LINE" | awk '{print $1}')
case "$VMM_LINE" in
  *qemu-system*)        BACKEND=qemu; IFACE="QMP socket (savevm/migrate)";;
  *cloud-hypervisor*)   BACKEND=cloud-hypervisor; IFACE="CH HTTP API (/api/v1/vm.snapshot)";;
  *firecracker*)        BACKEND=firecracker; IFACE="Firecracker REST (/snapshot/create)";;
  *)                    BACKEND=unknown; IFACE="?";;
esac

# 3) 该 VM 的运行时 socket 目录
VMDIR="/run/vc/vm/$SBID"
SOCKETS=$(sudo ls "$VMDIR" 2>/dev/null | tr '\n' ',' )

python3 -c '
import json,sys
print(json.dumps({
  "sandbox_id": sys.argv[1],
  "vmm_pid": sys.argv[2],
  "backend": sys.argv[3],
  "snapshot_interface": sys.argv[4],
  "vm_runtime_dir": sys.argv[5],
  "sockets": sys.argv[6],
}))
' "$SBID" "$VMM_PID" "$BACKEND" "$IFACE" "$VMDIR" "$SOCKETS"
'''


def ssm_run(instance_id, script, arg):
    """在节点上用 SSM 跑定位脚本,返回最后一行 JSON。
    用 --cli-input-json + 临时文件传参,彻底避开 shell/JSON 双重转义。"""
    import base64, tempfile, os
    full = f"{script}\n"   # 不要 set -e:grep 无匹配会返回非零,会误中断
    b64 = base64.b64encode(full.encode()).decode()
    one_liner = f"echo {b64} | base64 -d > /tmp/_probe.sh && bash /tmp/_probe.sh {arg}"
    payload = {
        "InstanceIds": [instance_id],
        "DocumentName": "AWS-RunShellScript",
        "Parameters": {"commands": [one_liner]},
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(payload, f)
        pf = f.name
    try:
        r = subprocess.run(
            ["aws", "ssm", "send-command", "--region", "us-east-1",
             "--cli-input-json", f"file://{pf}",
             "--query", "Command.CommandId", "--output", "text"],
            capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(f"SSM 下发失败: {r.stderr.strip()}")
        cid = r.stdout.strip()
        for _ in range(20):
            time.sleep(3)
            g = subprocess.run(
                ["aws", "ssm", "get-command-invocation", "--region", "us-east-1",
                 "--command-id", cid, "--instance-id", instance_id,
                 "--query", "{s:Status,o:StandardOutputContent}", "--output", "json"],
                capture_output=True, text=True)
            try:
                res = json.loads(g.stdout)
            except Exception:
                continue
            if res.get("s") in ("Success", "Failed"):
                return res.get("o", "")
        return ""
    finally:
        os.unlink(pf)


def node_instance_id(node_name):
    """K8s nodeName(= 私有 DNS)→ EC2 实例 ID。"""
    r = sh(f'aws ec2 describe-instances --region us-east-1 '
           f'--filters "Name=private-dns-name,Values={node_name}" '
           f'"Name=instance-state-name,Values=running" '
           f'--query "Reservations[].Instances[].InstanceId" --output text')
    return r.stdout.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pod")
    ap.add_argument("-n", "--namespace", default="default")
    args = ap.parse_args()

    # [控制平面侧] 拿 nodeName + podUID + runtimeClass
    pod = kubectl_json(f"-n {args.namespace} get pod {args.pod}")
    node = pod["spec"].get("nodeName")
    uid = pod["metadata"]["uid"]
    rc = pod["spec"].get("runtimeClassName")
    print(f"[控制平面] Pod={args.pod}  node={node}  uid={uid}  runtimeClass={rc}")
    if not (rc and rc.startswith("kata")):
        print(f"  ⚠️ 这个 Pod 不是 Kata(runtimeClass={rc}),没有独立 VMM 可定位。")
        return

    inst = node_instance_id(node)
    print(f"[控制平面] node {node} → EC2 {inst},下发节点侧定位...")

    # [节点侧] 由 podUID 定位 VMM
    out = ssm_run(inst, NODE_PROBE, uid)
    last = [l for l in out.strip().splitlines() if l.strip().startswith("{")]
    if not last:
        print("  ❌ 节点侧未返回结果,原始输出:\n" + out)
        return
    info = json.loads(last[-1])
    print("\n===== 定位结果 =====")
    print(f"  sandbox_id        : {info['sandbox_id']}")
    print(f"  VMM 进程 PID      : {info['vmm_pid']}  (在 {node})")
    print(f"  后端类型          : {info['backend']}")
    print(f"  快照接口          : {info['snapshot_interface']}")
    print(f"  VM 运行时目录     : {info['vm_runtime_dir']}")
    print(f"  socket 文件       : {info['sockets']}")
    print("\n→ 拿到这些,node-agent 下一步就能按'后端类型'调对应快照接口(本 demo 止于定位)。")


if __name__ == "__main__":
    main()
