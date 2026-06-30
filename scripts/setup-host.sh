#!/bin/bash
# setup-host.sh —— 在 .metal 主机上准备 Firecracker microVM + Claude Code(arm64 / x86_64)
# 幂等:可重复运行。对应 POC 文档第 3 节。
# 在主机上以 root 或 sudo 运行:  sudo bash setup-host.sh
#
# 架构:默认探测宿主架构;Intel x86 节点可显式 ARCH=x86_64 bash setup-host.sh。
#       Firecracker 发行包与 CI vmlinux 的架构后缀正好与 uname -m 一致(aarch64 / x86_64)。
#
# 实测踩坑修正(2026-06-12):
#  1. .metal 的 cloud-init 偶尔没装上 docker → 本脚本主动安装并启动 docker(不再假设已就绪)。
#  2. Firecracker CI 默认 vmlinux 没编 FUSE(`# CONFIG_FUSE_FS is not set`)→ JuiceFS/任何
#     S3 FUSE 在 guest 内挂不上。本脚本默认调用 build-fuse-kernel.sh 编一个带 FUSE 的内核,
#     vmconfig 指向 vmlinux-fuse。(设 SKIP_FUSE_KERNEL=1 可跳过,回退到无 FUSE 的 CI 内核)
#  3. rootfs 里预装 juicefs 客户端,方便 guest 内挂 JuiceFS。
set -euxo pipefail

# 架构:默认探测宿主架构;可用 ARCH 环境变量覆盖(aarch64 / x86_64)
ARCH="${ARCH:-$(uname -m)}"
case "$ARCH" in
  aarch64|arm64) ARCH=aarch64 ;;
  x86_64|amd64)  ARCH=x86_64 ;;
  *) echo "ERROR: 不支持的架构 ARCH=$ARCH(仅支持 aarch64 / x86_64)" >&2; exit 1 ;;
esac
WORKDIR=/opt/sbx
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$WORKDIR"
cd "$WORKDIR"

# ---------- 0. 前提校验 ----------
ls -l /dev/kvm   # 必须存在,否则不是 .metal

# ---------- 0.5 确保 docker 就绪(踩坑修正:cloud-init 不一定装上) ----------
if ! command -v docker >/dev/null 2>&1; then
  dnf install -y docker
fi
systemctl enable --now docker
# 等 docker daemon 真正可用
for i in $(seq 1 15); do docker info >/dev/null 2>&1 && break; sleep 2; done
docker version >/dev/null

# ---------- 1. 安装 Firecracker($ARCH) ----------
if ! command -v firecracker >/dev/null 2>&1; then
  VER=$(curl -s https://api.github.com/repos/firecracker-microvm/firecracker/releases/latest | grep tag_name | cut -d'"' -f4)
  curl -L "https://github.com/firecracker-microvm/firecracker/releases/download/${VER}/firecracker-${VER}-${ARCH}.tgz" -o fc.tgz
  tar -xzf fc.tgz
  cp release-${VER}-${ARCH}/firecracker-${VER}-${ARCH} /usr/local/bin/firecracker
  chmod +x /usr/local/bin/firecracker
fi
firecracker --version

# ---------- 2. guest 内核 ----------
# 默认:编一个带 FUSE/overlay/inotify 的内核(JuiceFS 等 FUSE 文件系统在 guest 内必需)。
# 这是实测坐实的必做项(CI 内核无 FUSE)。产出 /opt/sbx/vmlinux-fuse。
if [ "${SKIP_FUSE_KERNEL:-0}" != "1" ]; then
  if [ ! -f "$WORKDIR/vmlinux-fuse" ]; then
    echo "=== 编译带 FUSE 的 guest 内核(首次约几分钟,64核native) ==="
    # 找 build-fuse-kernel.sh:优先同目录,其次 /opt(部署时可能单独推送到这里)
    KBUILD=""
    for cand in "$SCRIPT_DIR/build-fuse-kernel.sh" /opt/build-fuse-kernel.sh; do
      [ -f "$cand" ] && { KBUILD="$cand"; break; }
    done
    if [ -z "$KBUILD" ]; then
      echo "ERROR: 找不到 build-fuse-kernel.sh(同目录或 /opt 均无)。" >&2
      echo "       请把 build-fuse-kernel.sh 放到 $SCRIPT_DIR 或 /opt,或设 SKIP_FUSE_KERNEL=1 回退 CI 内核。" >&2
      exit 1
    fi
    bash "$KBUILD"
  fi
  KERNEL="$WORKDIR/vmlinux-fuse"
else
  # 回退:无 FUSE 的 CI 内核(仅本地 ext4 workspace 场景够用,不能挂 JuiceFS)
  if [ ! -f "$WORKDIR/vmlinux" ]; then
    KURL="https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.10/${ARCH}/vmlinux-5.10.223"
    curl -fL "$KURL" -o "$WORKDIR/vmlinux"
  fi
  KERNEL="$WORKDIR/vmlinux"
  echo "⚠️ SKIP_FUSE_KERNEL=1:用无 FUSE 的 CI 内核,JuiceFS/s3fs/mountpoint 在 guest 内将挂不上"
fi
ls -lh "$KERNEL"

# ---------- 3. 构建带 Claude Code + JuiceFS 的 rootfs(随宿主架构 $ARCH) ----------

# 生成 sbxinit（guest PID 1：网络 + 可选 JuiceFS 挂载）
cat > "$WORKDIR/sbxinit" <<'SBXINIT'
#!/bin/bash
set -e

# 网络初始化
ip link set lo up
ip addr add 172.16.0.2/30 dev eth0
ip link set eth0 up
ip route add default via 172.16.0.1
echo "nameserver 8.8.8.8" > /etc/resolv.conf

# JuiceFS workspace 挂载（方案 B）
# JFS_REDIS / JFS_BUCKET / JFS_NAME / AWS_REGION 由 node-agent 通过 boot_args 注入
if [ -n "${JFS_REDIS:-}" ] && [ -n "${JFS_BUCKET:-}" ]; then
  JFS_NAME="${JFS_NAME:-sbxfs}"
  AWS_REGION="${AWS_REGION:-us-east-1}"

  # 首次：格式化文件系统（已格式化时幂等跳过）
  juicefs format \
    --storage s3 \
    --bucket "https://${JFS_BUCKET}.s3.${AWS_REGION}.amazonaws.com" \
    "${JFS_REDIS}" "${JFS_NAME}" 2>/dev/null || true

  # 挂载 /workspace（writeback + 本地缓存加速小文件写）
  mkdir -p /workspace /var/jfscache
  juicefs mount "${JFS_REDIS}" /workspace \
    --writeback \
    --cache-dir /var/jfscache \
    --cache-size 10240 \
    --buffer-size 1024 \
    -d 2>/dev/null || echo "[sbxinit] JuiceFS mount failed, continuing without workspace"
fi

echo "[sbxinit] ready"
exec /bin/bash
SBXINIT
chmod +x "$WORKDIR/sbxinit"

cat > "$WORKDIR/Dockerfile.sbx" <<'DOCKER'
FROM node:22-bookworm
RUN apt-get update && apt-get install -y \
    git build-essential python3 curl ca-certificates \
    iproute2 iputils-ping fuse3 inotify-tools strace \
 && rm -rf /var/lib/apt/lists/*
RUN npm install -g @anthropic-ai/claude-code
# JuiceFS 客户端(供 guest 内挂 /workspace);失败不阻断镜像构建
RUN curl -sSL https://d.juicefs.com/install | sh - || echo "juicefs install skipped"
# sbxinit：网络初始化 + 可选 JuiceFS 挂载（JFS_REDIS/JFS_BUCKET 由 boot_args 注入）
COPY sbxinit /sbin/sbxinit
RUN chmod +x /sbin/sbxinit
DOCKER

docker build -f "$WORKDIR/Dockerfile.sbx" -t claude-sbx:poc "$WORKDIR"

# 导出为 ext4(8 GiB);幂等:已存在则重建内容
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
    "kernel_image_path": "$KERNEL",
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
echo "准备完成。内核: $KERNEL"
echo "启动 microVM(交互式):"
echo "  sudo firecracker --no-api --config-file $WORKDIR/vmconfig.json"
echo ""
echo "进入 guest 后跑 Claude Code(走 Bedrock):"
echo "  export CLAUDE_CODE_USE_BEDROCK=1 AWS_REGION=us-east-1"
echo "  export ANTHROPIC_MODEL=us.anthropic.claude-opus-4-8"
echo "  # 方式A: export AWS_BEARER_TOKEN_BEDROCK=<key>"
echo "  # 方式B(本主机已挂IAM角色,凭据从元数据取后注入 guest env)"
echo "  claude --version"
echo ""
echo "若用 FUSE 内核,可在 guest 内挂 JuiceFS:"
echo "  juicefs format --storage s3 --bucket https://<bucket>.s3.<region>.amazonaws.com redis://172.16.0.1:6379/1 sbxfs"
echo "  juicefs mount redis://172.16.0.1:6379/1 /workspace --writeback --cache-dir /jfscache --cache-size 10240 --buffer-size 1024 -d"
echo "========================================================"
