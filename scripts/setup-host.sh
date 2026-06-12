#!/bin/bash
# setup-host.sh —— 在 Graviton .metal 主机上准备 Firecracker microVM + Claude Code(Phase 1, 验 H1)
# 幂等:可重复运行。对应 POC 文档第 3 节 1.3–1.7。
# 在主机上以 root 或 sudo 运行:  sudo bash setup-host.sh
set -euxo pipefail

ARCH=aarch64
WORKDIR=/opt/sbx
mkdir -p "$WORKDIR"
cd "$WORKDIR"

# ---------- 0. 前提校验 ----------
ls -l /dev/kvm   # 必须存在,否则不是 .metal

# ---------- 1. 安装 Firecracker(aarch64) ----------
if ! command -v firecracker >/dev/null 2>&1; then
  VER=$(curl -s https://api.github.com/repos/firecracker-microvm/firecracker/releases/latest | grep tag_name | cut -d'"' -f4)
  curl -L "https://github.com/firecracker-microvm/firecracker/releases/download/${VER}/firecracker-${VER}-${ARCH}.tgz" -o fc.tgz
  tar -xzf fc.tgz
  cp release-${VER}-${ARCH}/firecracker-${VER}-${ARCH} /usr/local/bin/firecracker
  chmod +x /usr/local/bin/firecracker
fi
firecracker --version

# ---------- 2. 取 guest 内核(aarch64,用 Firecracker CI 提供的 vmlinux) ----------
# 注:CI 桶按 arch 提供测试内核。生产应自编内核固化 config(FUSE/overlay/cgroup/inotify)。
if [ ! -f "$WORKDIR/vmlinux" ]; then
  # Firecracker CI 公共 S3 桶(无需凭据)
  KURL="https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.10/aarch64/vmlinux-5.10.223"
  curl -fL "$KURL" -o "$WORKDIR/vmlinux" || {
    echo "内核下载失败 —— 请到 Firecracker getting-started 文档确认当前 CI 内核路径,或自编内核"; exit 1; }
fi
ls -lh "$WORKDIR/vmlinux"

# ---------- 3. 构建带 Claude Code 的 arm64 rootfs ----------
cat > "$WORKDIR/Dockerfile.sbx" <<'DOCKER'
FROM node:22-bookworm
RUN apt-get update && apt-get install -y \
    git build-essential python3 curl ca-certificates \
    iproute2 iputils-ping fuse3 inotify-tools strace \
 && rm -rf /var/lib/apt/lists/*
RUN npm install -g @anthropic-ai/claude-code
RUN printf '#!/bin/bash\nip link set lo up\nip addr add 172.16.0.2/30 dev eth0\nip link set eth0 up\nip route add default via 172.16.0.1\necho "nameserver 8.8.8.8" > /etc/resolv.conf\nexec /bin/bash\n' > /sbin/sbxinit \
 && chmod +x /sbin/sbxinit
DOCKER

docker build -f "$WORKDIR/Dockerfile.sbx" -t claude-sbx:poc "$WORKDIR"

# 导出为 ext4(8 GiB)
if [ ! -f "$WORKDIR/rootfs.ext4" ]; then
  dd if=/dev/zero of="$WORKDIR/rootfs.ext4" bs=1M count=8192
  mkfs.ext4 "$WORKDIR/rootfs.ext4"
fi
MNT=$(mktemp -d)
mount "$WORKDIR/rootfs.ext4" "$MNT"
CID=$(docker create claude-sbx:poc)
docker export "$CID" | tar -C "$MNT" -xf -
docker rm "$CID"
umount "$MNT"
rmdir "$MNT"
echo "rootfs.ext4 构建完成"

# ---------- 4. 配置 TAP 网络 + NAT ----------
ip tuntap add tap0 mode tap 2>/dev/null || true
ip addr add 172.16.0.1/30 dev tap0 2>/dev/null || true
ip link set tap0 up
HOST_IF=$(ip route | awk '/default/{print $5; exit}')
sysctl -w net.ipv4.ip_forward=1
iptables -t nat -C POSTROUTING -o "$HOST_IF" -j MASQUERADE 2>/dev/null || \
  iptables -t nat -A POSTROUTING -o "$HOST_IF" -j MASQUERADE
iptables -C FORWARD -i tap0 -o "$HOST_IF" -j ACCEPT 2>/dev/null || \
  iptables -A FORWARD -i tap0 -o "$HOST_IF" -j ACCEPT
iptables -C FORWARD -i "$HOST_IF" -o tap0 -m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || \
  iptables -A FORWARD -i "$HOST_IF" -o tap0 -m state --state RELATED,ESTABLISHED -j ACCEPT

# ---------- 5. 写 microVM 配置(2 vCPU / 4 GiB) ----------
cat > "$WORKDIR/vmconfig.json" <<EOF
{
  "boot-source": {
    "kernel_image_path": "$WORKDIR/vmlinux",
    "boot_args": "console=ttyS0 reboot=k panic=1 pci=off init=/sbin/sbxinit"
  },
  "drives": [{
    "drive_id": "rootfs", "path_on_host": "$WORKDIR/rootfs.ext4",
    "is_root_device": true, "is_read_only": false
  }],
  "network-interfaces": [{ "iface_id": "eth0", "host_dev_name": "tap0" }],
  "machine-config": { "vcpu_count": 2, "mem_size_mib": 4096 }
}
EOF

echo "========================================================"
echo "准备完成。启动 microVM(交互式):"
echo "  sudo firecracker --no-api --config-file $WORKDIR/vmconfig.json"
echo ""
echo "进入 guest 后跑 Claude Code(走 Bedrock):"
echo "  export CLAUDE_CODE_USE_BEDROCK=1 AWS_REGION=us-east-1"
echo "  export ANTHROPIC_MODEL=us.anthropic.claude-opus-4-8"
echo "  # 方式A: export AWS_BEARER_TOKEN_BEDROCK=<key>"
echo "  # 方式B(本主机已挂IAM角色,但凭据在宿主,需通过元数据/代理传入 guest)"
echo "  claude --version"
echo "========================================================"
