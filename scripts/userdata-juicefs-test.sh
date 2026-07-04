#!/bin/bash
# UserData 脚本 —— 节点启动时一次性安装 Firecracker + Redis + JuiceFS rootfs
# 用于方案 B（JuiceFS workspace）的集成测试
# 在 kubelet 启动前完成所有安装，不会触发 EKS 健康检查替换

set -euo pipefail
exec > /var/log/userdata-juicefs.log 2>&1

echo "[userdata] START $(date)"

# ---------- 1. 基础工具 ----------
dnf install -y docker redis6 fuse3 2>/dev/null || true
systemctl enable --now docker
systemctl enable --now redis6
sleep 2
redis6-cli ping && echo "[userdata] Redis OK"

# ---------- 2. Firecracker ----------
# 架构:默认探测宿主架构;可用 ARCH 环境变量覆盖(aarch64 / x86_64)
ARCH="${ARCH:-$(uname -m)}"
case "$ARCH" in
  aarch64|arm64) ARCH=aarch64 ;;
  x86_64|amd64)  ARCH=x86_64 ;;
  *) echo "[userdata] 不支持的架构 ARCH=$ARCH"; exit 1 ;;
esac
VER=$(curl -sf https://api.github.com/repos/firecracker-microvm/firecracker/releases/latest \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['tag_name'])")
curl -sfL "https://github.com/firecracker-microvm/firecracker/releases/download/${VER}/firecracker-${VER}-${ARCH}.tgz" \
  -o /tmp/fc.tgz
tar -xzf /tmp/fc.tgz -C /tmp
mv "/tmp/release-${VER}-${ARCH}/firecracker-${VER}-${ARCH}" /usr/local/bin/firecracker
chmod +x /usr/local/bin/firecracker
firecracker --version && echo "[userdata] Firecracker OK"

# ---------- 3. FUSE kernel（JuiceFS 必需）----------
mkdir -p /opt/sbx /var/lib/sbx
# 用 CI kernel 先跑通流程；如需 JuiceFS 则需 FUSE kernel（见 build-fuse-kernel.sh）
# POC 测试：用 CI kernel + JuiceFS 客户端在 host 侧挂，验证方案 B 快照逻辑
curl -sfL "https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.10/${ARCH}/vmlinux-5.10.223" \
  -o /opt/sbx/vmlinux
ls -lh /opt/sbx/vmlinux && echo "[userdata] Kernel OK"

# ---------- 4. JuiceFS 客户端（host 侧）----------
curl -sSL https://d.juicefs.com/install | sh - || echo "[userdata] JuiceFS install failed, continuing"
juicefs version 2>/dev/null && echo "[userdata] JuiceFS client OK" || echo "[userdata] JuiceFS client not available"

# ---------- 5. sbxinit（含 JuiceFS 自动挂载）----------
cat > /tmp/sbxinit << 'SBXINIT'
#!/bin/bash
set -e
ip link set lo up
ip addr add 172.18.1.2/30 dev eth0 2>/dev/null || true
ip link set eth0 up 2>/dev/null || true
ip route add default via 172.18.1.1 2>/dev/null || true
echo "nameserver 8.8.8.8" > /etc/resolv.conf

# JuiceFS workspace 挂载（boot_args 注入 JFS_REDIS/JFS_BUCKET）
if [ -n "${JFS_REDIS:-}" ] && [ -n "${JFS_BUCKET:-}" ]; then
  JFS_NAME="${JFS_NAME:-sbxfs}"
  AWS_REGION="${AWS_REGION:-us-east-1}"
  juicefs format --storage s3 \
    --bucket "https://${JFS_BUCKET}.s3.${AWS_REGION}.amazonaws.com" \
    "${JFS_REDIS}" "${JFS_NAME}" 2>/dev/null || true
  mkdir -p /workspace /var/jfscache
  juicefs mount "${JFS_REDIS}" /workspace \
    --writeback --cache-dir /var/jfscache \
    --cache-size 10240 --buffer-size 1024 -d 2>/dev/null \
    || echo "[sbxinit] JuiceFS mount failed, no /workspace"
fi

echo "[sbxinit] ready"
exec /bin/bash
SBXINIT
chmod +x /tmp/sbxinit

# ---------- 6. 构建含 JuiceFS 的 rootfs ----------
cat > /tmp/Dockerfile.jfs << 'DOCKER'
FROM node:22-bookworm
RUN apt-get update && apt-get install -y \
    git build-essential python3 curl ca-certificates \
    iproute2 iputils-ping fuse3 inotify-tools \
 && rm -rf /var/lib/apt/lists/*
RUN npm install -g @anthropic-ai/claude-code 2>/dev/null || true
RUN curl -sSL https://d.juicefs.com/install | sh - || echo "juicefs skipped"
COPY sbxinit /sbin/sbxinit
RUN chmod +x /sbin/sbxinit
DOCKER

cp /tmp/sbxinit /tmp/sbxinit_ctx
cd /tmp && docker build -f /tmp/Dockerfile.jfs --build-arg BUILDKIT_INLINE_CACHE=1 -t claude-sbx:jfs . 2>&1 | tail -5

# rootfs export
mkdir -p /tmp/rootfs_jfs
dd if=/dev/zero of=/opt/sbx/rootfs.ext4 bs=1M count=6144 status=none
mkfs.ext4 /opt/sbx/rootfs.ext4 -q
mount /opt/sbx/rootfs.ext4 /tmp/rootfs_jfs
CID=$(docker create claude-sbx:jfs)
docker export "$CID" | tar -C /tmp/rootfs_jfs -xf -
docker rm "$CID"
umount /tmp/rootfs_jfs
ls -lh /opt/sbx/rootfs.ext4 && echo "[userdata] rootfs.ext4 OK"

# ---------- 7. NAT 设置（node-agent 需要）----------
sysctl -w net.ipv4.ip_forward=1
HOST_IF=$(ip route | awk '/default/{print $5; exit}')
iptables -t nat -C POSTROUTING -o "$HOST_IF" -j MASQUERADE 2>/dev/null || \
  iptables -t nat -A POSTROUTING -o "$HOST_IF" -j MASQUERADE

echo "[userdata] COMPLETE $(date)"
