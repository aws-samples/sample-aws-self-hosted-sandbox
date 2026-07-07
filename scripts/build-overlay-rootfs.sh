#!/usr/bin/env bash
# 构建 overlay + JuiceFS 版最小 rootfs（方案 B：系统写入持久化到 S3）
#
# 与 build-min-rootfs.sh 的区别:
#   - rootfs 内装 juicefs 客户端 + fuse
#   - sbxinit 改为: 配网 → juicefs mount /persist(连共享 Redis + S3) →
#     overlay 合并(lower=只读基础根 / upper=/persist/upper) → pivot_root → sshd + 心跳
#   - guest 通过内核 cmdline 读 JFS_REDIS / JFS_BUCKET / JFS_NAME / AWS 凭据(node-agent 注入)
#
# 产出 overlay-rootfs.tar.gz 上传 S3,供节点拉取。
# 用法: bash scripts/build-overlay-rootfs.sh <s3-bucket>
set -euo pipefail

S3_BUCKET="${1:?usage: build-overlay-rootfs.sh <s3-bucket>}"
REGION="${AWS_REGION:-us-east-1}"
WORK=$(mktemp -d)
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PUBKEY_FILE="${ROOT}/.sbxkeys/sbx_exec.pub"
[ -f "$PUBKEY_FILE" ] || { echo "missing $PUBKEY_FILE (run ssh-keygen first)"; exit 1; }
PUBKEY=$(cat "$PUBKEY_FILE")

echo "==> build dir: $WORK"

# ---------- 1. sbxinit (guest PID 1, overlay + JuiceFS 根) ----------
cat > "$WORK/sbxinit" <<INIT
#!/bin/sh
# PID1: overlay 根(lower=只读基础rootfs / upper=JuiceFS→S3) + sshd + 心跳
mount -t proc proc /proc 2>/dev/null
mount -t sysfs sys /sys 2>/dev/null
mount -t tmpfs tmpfs /run 2>/dev/null
mount -t devtmpfs dev /dev 2>/dev/null
mkdir -p /dev/pts && mount -t devpts devpts /dev/pts 2>/dev/null

log() { echo "[sbxinit] \$1" > /dev/console; }

# --- 配网(JuiceFS 要连 Redis,必须先有网) ---
ip link set lo up 2>/dev/null
SBX_IP=\$(cat /proc/cmdline | tr ' ' '\n' | sed -n 's/^SBX_IP=//p')
SBX_GW=\$(cat /proc/cmdline | tr ' ' '\n' | sed -n 's/^SBX_GW=//p')
ip link set eth0 up 2>/dev/null
if [ -n "\$SBX_IP" ]; then
  ip addr add \${SBX_IP}/30 dev eth0 2>/dev/null
  [ -n "\$SBX_GW" ] && ip route add default via \$SBX_GW 2>/dev/null
  log "net \$SBX_IP gw \$SBX_GW"
fi
echo "nameserver 169.254.169.253" > /etc/resolv.conf 2>/dev/null
echo "nameserver 8.8.8.8" >> /etc/resolv.conf 2>/dev/null

# --- 从 cmdline 读 JuiceFS 参数 + AWS 凭据 ---
JFS_REDIS=\$(cat /proc/cmdline | tr ' ' '\n' | sed -n 's/^JFS_REDIS=//p')
JFS_BUCKET=\$(cat /proc/cmdline | tr ' ' '\n' | sed -n 's/^JFS_BUCKET=//p')
JFS_NAME=\$(cat /proc/cmdline | tr ' ' '\n' | sed -n 's/^JFS_NAME=//p')
export AWS_ACCESS_KEY_ID=\$(cat /proc/cmdline | tr ' ' '\n' | sed -n 's/^AWS_AK=//p')
export AWS_SECRET_ACCESS_KEY=\$(cat /proc/cmdline | tr ' ' '\n' | sed -n 's/^AWS_SK=//p')
export AWS_REGION=us-east-1

OVERLAY_OK=0
if [ -n "\$JFS_REDIS" ] && [ -n "\$JFS_BUCKET" ] && [ -n "\$JFS_NAME" ]; then
  log "juicefs mount: name=\$JFS_NAME redis=\$JFS_REDIS"
  mkdir -p /persist
  # format 幂等(已 format 会跳过);再 mount
  juicefs format --storage s3 --bucket "\$JFS_BUCKET" "\$JFS_REDIS" "\$JFS_NAME" > /dev/console 2>&1
  juicefs mount "\$JFS_REDIS" /persist -d --no-usage-report > /dev/console 2>&1
  # 等挂载就绪
  i=0; while [ \$i -lt 20 ]; do mountpoint -q /persist && break; i=\$((i+1)); sleep 0.5; done
  if mountpoint -q /persist; then
    log "juicefs mounted at /persist"
    mkdir -p /persist/upper /persist/work /merged
    # overlay: lower=当前只读根(整个 /), upper=JuiceFS
    mount -t overlay overlay -o lowerdir=/,upperdir=/persist/upper,workdir=/persist/work /merged 2>/dev/console
    if mountpoint -q /merged; then
      log "overlay root ready, pivoting"
      OVERLAY_OK=1
    else
      log "overlay mount FAILED, fallback to base rootfs"
    fi
  else
    log "juicefs mount FAILED, fallback to base rootfs"
  fi
fi

# --- sshd 授权(不论是否 overlay,exec 通道要可用) ---
setup_ssh() {
  mkdir -p /root/.ssh /run/sshd
  chmod 700 /root /root/.ssh
  echo "$PUBKEY" > /root/.ssh/authorized_keys
  chmod 600 /root/.ssh/authorized_keys
  sed -i 's/^#\?StrictModes.*/StrictModes no/' /etc/ssh/sshd_config 2>/dev/null || echo "StrictModes no" >> /etc/ssh/sshd_config
  [ -f /etc/ssh/ssh_host_ed25519_key ] || ssh-keygen -A 2>/dev/null
  /usr/sbin/sshd 2>/dev/null && log "sshd started" || log "sshd FAILED"
}

heartbeat() {
  i=0
  while true; do i=\$((i+1)); echo "\$i" > /run/heartbeat; echo "[heartbeat] \$i" > /dev/console; sleep 2; done
}

if [ "\$OVERLAY_OK" = "1" ]; then
  # 切到 overlay 根:把关键挂载移过去,pivot_root
  mkdir -p /merged/proc /merged/sys /merged/dev /merged/run /merged/persist
  mount --move /proc /merged/proc 2>/dev/null
  mount --move /sys  /merged/sys  2>/dev/null
  mount --move /dev  /merged/dev  2>/dev/null
  mount --move /run  /merged/run  2>/dev/null
  mount --move /persist /merged/persist 2>/dev/null
  cd /merged
  mkdir -p old_root
  pivot_root . old_root 2>/dev/console && log "pivot_root OK" || log "pivot_root FAILED"
  # pivot 后在新根内继续
  setup_ssh
  log "overlay microVM booted"
  heartbeat
else
  # 回退:直接用基础 rootfs(不持久化系统写入,但保证能起来做验证)
  setup_ssh
  log "base microVM booted (NO overlay/persist)"
  heartbeat
fi
INIT
chmod +x "$WORK/sbxinit"

# ---------- 2. docker 造 rootfs: python + iproute2 + openssh-server + juicefs + fuse ----------
cat > "$WORK/Dockerfile" <<'DOCKER'
FROM public.ecr.aws/docker/library/python:3.12-slim
RUN sed -i 's|deb.debian.org|cdn-aws.deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
 && apt-get update && apt-get install -y --no-install-recommends \
    iproute2 openssh-server iputils-ping fuse3 ca-certificates curl \
 && rm -rf /var/lib/apt/lists/* \
 && sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config \
 && sed -i 's/^#\?PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config
# JuiceFS 客户端(arm64)
RUN curl -sSL https://d.juicefs.com/install | sh - 2>/dev/null \
 || (curl -sSLo /tmp/jfs.tar.gz https://github.com/juicedata/juicefs/releases/download/v1.3.1/juicefs-1.3.1-linux-arm64.tar.gz \
     && tar -xzf /tmp/jfs.tar.gz -C /usr/local/bin juicefs && chmod +x /usr/local/bin/juicefs)
DOCKER
docker build --platform linux/arm64 -t sbx-overlay-rootfs:b "$WORK"
CID=$(docker create --platform linux/arm64 sbx-overlay-rootfs:b sleep infinity)
mkdir -p "$WORK/rootfs"
docker export "$CID" | tar -C "$WORK/rootfs" -xf -
docker rm "$CID" >/dev/null

cp "$WORK/sbxinit" "$WORK/rootfs/sbin/sbxinit"
chmod +x "$WORK/rootfs/sbin/sbxinit"

# ---------- 3. 打包 + 上传 ----------
TARBALL="$WORK/overlay-rootfs.tar.gz"
tar -C "$WORK/rootfs" -czf "$TARBALL" .
echo "==> rootfs tarball: $(du -h "$TARBALL" | cut -f1)"
S3_URI="s3://${S3_BUCKET}/rootfs/overlay-rootfs.tar.gz"
aws s3 cp "$TARBALL" "$S3_URI" --region "$REGION"
echo "==> uploaded: $S3_URI"
