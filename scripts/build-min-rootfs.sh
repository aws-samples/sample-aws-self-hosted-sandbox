#!/usr/bin/env bash
# 构建最小可启动 arm64 rootfs
#   里程碑 A: 内存快照 + 跨机恢复(心跳验证)
#   里程碑 B: exec —— vsock 主通道(guest 起 vsock-exec-agent) + SSH 兜底
#            (SSH: init 读内核 cmdline 的 SBX_IP 配网 + 起 sshd + 授权公钥)
# 产出 rootfs.tar.gz 上传 S3,供节点拉取。
#
# 用法: bash scripts/build-min-rootfs.sh <s3-bucket>
set -euo pipefail

S3_BUCKET="${1:?usage: build-min-rootfs.sh <s3-bucket>}"
REGION="${AWS_REGION:-us-east-1}"
WORK=$(mktemp -d)
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PUBKEY_FILE="${ROOT}/.sbxkeys/sbx_exec.pub"
[ -f "$PUBKEY_FILE" ] || { echo "missing $PUBKEY_FILE (run ssh-keygen first)"; exit 1; }
PUBKEY=$(cat "$PUBKEY_FILE")

echo "==> build dir: $WORK"

# ---------- 1. sbxinit (guest PID 1) ----------
cat > "$WORK/sbxinit" <<INIT
#!/bin/sh
# PID1 for Firecracker microVM (milestone A heartbeat + B sshd)
mount -t proc proc /proc 2>/dev/null
mount -t sysfs sys /sys 2>/dev/null
mount -t tmpfs tmpfs /run 2>/dev/null
mount -t devtmpfs dev /dev 2>/dev/null
mkdir -p /dev/pts && mount -t devpts devpts /dev/pts 2>/dev/null

ip link set lo up 2>/dev/null

# 从内核 cmdline 读 SBX_IP/SBX_GW(node-agent 注入: 172.18.{tap_idx}.2 / .1)
SBX_IP=\$(cat /proc/cmdline | tr ' ' '\n' | sed -n 's/^SBX_IP=//p')
SBX_GW=\$(cat /proc/cmdline | tr ' ' '\n' | sed -n 's/^SBX_GW=//p')
ip link set eth0 up 2>/dev/null
if [ -n "\$SBX_IP" ]; then
  ip addr add \${SBX_IP}/30 dev eth0 2>/dev/null
  [ -n "\$SBX_GW" ] && ip route add default via \$SBX_GW 2>/dev/null
  echo "[sbxinit] net configured: \$SBX_IP gw \$SBX_GW" > /dev/console
fi
echo "nameserver 8.8.8.8" > /etc/resolv.conf 2>/dev/null

# sshd(里程碑 B exec): 授权公钥 + 起 sshd
mkdir -p /root/.ssh /run/sshd
chmod 700 /root /root/.ssh
echo "$PUBKEY" > /root/.ssh/authorized_keys
chmod 700 /root/.ssh; chmod 600 /root/.ssh/authorized_keys
chown -R root:root /root/.ssh 2>/dev/null
# 关闭 StrictModes,避免 /root 属主/权限细节导致 publickey 被拒
sed -i 's/^#\?StrictModes.*/StrictModes no/' /etc/ssh/sshd_config 2>/dev/null || echo "StrictModes no" >> /etc/ssh/sshd_config
# 首次生成 host key
[ -f /etc/ssh/ssh_host_ed25519_key ] || ssh-keygen -A 2>/dev/null
/usr/sbin/sshd 2>/dev/null && echo "[sbxinit] sshd started" > /dev/console || echo "[sbxinit] sshd FAILED" > /dev/console

# vsock exec agent(exec 主通道): 监听 AF_VSOCK:2222,不依赖 guest 网络。
# 后台常驻;stdout/stderr 转串口便于排查。
python3 /sbin/vsock-exec-agent.py > /dev/console 2>&1 &
echo "[sbxinit] vsock-exec-agent started (pid \$!)" > /dev/console

echo "[sbxinit] microVM booted" > /dev/console

# 心跳: 递增计数写串口 + tmpfs。内存级 resume 后从断点续增。
i=0
while true; do
  i=\$((i+1))
  echo "[heartbeat] count=\$i" > /dev/console
  echo "\$i" > /run/heartbeat
  sleep 2
done
INIT
chmod +x "$WORK/sbxinit"

# ---------- 2. docker 造 rootfs: python(vsock agent) + iproute2 + openssh-server(SSH 兜底) ----------
cat > "$WORK/Dockerfile" <<'DOCKER'
FROM public.ecr.aws/docker/library/python:3.12-slim
RUN sed -i 's|deb.debian.org|cdn-aws.deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
 && apt-get update && apt-get install -y --no-install-recommends \
    iproute2 openssh-server iputils-ping \
 && rm -rf /var/lib/apt/lists/* \
 && sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config \
 && sed -i 's/^#\?PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config
DOCKER
docker build --platform linux/arm64 -t sbx-rootfs:b "$WORK"
CID=$(docker create --platform linux/arm64 sbx-rootfs:b sleep infinity)
mkdir -p "$WORK/rootfs"
docker export "$CID" | tar -C "$WORK/rootfs" -xf -
docker rm "$CID" >/dev/null

cp "$WORK/sbxinit" "$WORK/rootfs/sbin/sbxinit"
chmod +x "$WORK/rootfs/sbin/sbxinit"

# vsock exec agent(guest 端 exec 主通道),由 sbxinit 后台启动
cp "${ROOT}/scripts/vsock-exec-agent.py" "$WORK/rootfs/sbin/vsock-exec-agent.py"
chmod +x "$WORK/rootfs/sbin/vsock-exec-agent.py"

# ---------- 3. 打包 + 上传 ----------
TARBALL="$WORK/rootfs.tar.gz"
tar -C "$WORK/rootfs" -czf "$TARBALL" .
echo "==> rootfs tarball: $(du -h "$TARBALL" | cut -f1)"
S3_URI="s3://${S3_BUCKET}/rootfs/min-rootfs.tar.gz"
aws s3 cp "$TARBALL" "$S3_URI" --region "$REGION"
echo "==> uploaded: $S3_URI"
