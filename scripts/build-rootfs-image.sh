#!/usr/bin/env bash
# 构建【命名 rootfs 模板】—— 泛化自 build-min-rootfs.sh。
# 每个模板 = 一个可启动 rootfs.tar.gz,含 sbxinit(PID1) + vsock-exec-agent(exec 主通道),
# 在其上叠加该"镜像"的应用层。产出 rootfs-{name}.tar.gz 上传 S3,节点拉取造 /opt/sbx/rootfs-{name}.ext4。
# create 时按沙盒的 image 字段选模板(见 node-agent op_create 的 rootfs_template)。
#
# 用法: bash scripts/build-rootfs-image.sh <name> <s3-bucket>
#   name = min          → 等价于 build-min-rootfs.sh(基础模板)
#   name = web          → 自带 demo 首页 + 开机自起 :80(端口暴露打开即见站点)
#   name = claude-code  → 预打包运行环境:Node.js LTS + @anthropic-ai/claude-code CLI
#   name = openclaw     → 预打包运行环境:Node.js LTS + openclaw CLI
#   其它 name            → 目前回退到 min 内容(可在下方 case 里加预设)
#
# 环境变量:
#   PLATFORM=linux/amd64          目标架构(必须与 node_arch 一致;默认 linux/arm64)
#                                 🔴 claude-code / openclaw 必须在【同架构原生机器】上跑:
#                                 Apple Silicon 上 PLATFORM=linux/amd64 会在 npm install -g
#                                 中途 qemu 段错误(2026-08-13 实测 exit 139)。min/web 不装
#                                 npm 包,跨架构没问题。变通=一次性起同架构 EC2 构建。
#   NODE_VERSION=22.23.2          预打包运行环境用的 Node.js 版本(claude-code/openclaw 都要 >=22.22.3)
#   CLAUDE_CODE_VERSION=2.1.231   @anthropic-ai/claude-code 版本(留 latest 也可)
#   OPENCLAW_VERSION=2026.7.1-2   openclaw 版本
#
# 上传后要让节点真的造这个模板,还需在 phase3 传:
#   -var="rootfs_images=web,claude-code,openclaw"
# 🔴 且此时 phase3 的 sandbox_warm_pool_size 必须为 0 —— 预热池节点不等 cloud-init 跑完就停机,
#    命名模板会全缺,而 node-agent 对缺失模板静默回退 min(2026-08-13 实测)。
set -euo pipefail

PLATFORM="${PLATFORM:-linux/arm64}"
# 预打包运行环境的版本(可用环境变量覆盖)。Node 需 >=22.22.3(两个 CLI 的 engines 要求)。
NODE_VERSION="${NODE_VERSION:-22.23.2}"
CLAUDE_CODE_VERSION="${CLAUDE_CODE_VERSION:-2.1.231}"
OPENCLAW_VERSION="${OPENCLAW_VERSION:-2026.7.1-2}"
ROOTFS_PREFIX="${ROOTFS_PREFIX:-rootfs}"
NAME="${1:?usage: build-rootfs-image.sh <name> <s3-bucket>}"
S3_BUCKET="${2:?usage: build-rootfs-image.sh <name> <s3-bucket>}"
REGION="${AWS_REGION:-us-east-1}"
WORK=$(mktemp -d)
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PUBKEY_FILE="${ROOT}/.sbxkeys/sbx_exec.pub"
[ -f "$PUBKEY_FILE" ] || { echo "missing $PUBKEY_FILE (run ssh-keygen first)"; exit 1; }
PUBKEY=$(cat "$PUBKEY_FILE")

echo "==> build rootfs template '$NAME' in $WORK"

# ---------- 1. sbxinit (guest PID 1) ----------
# 通用部分与 min 一致;额外:若 /web/index.html 存在则开机自起 :80(web 预设用)。
# min 模板没有 /web → 该分支不触发,行为与原 min-rootfs 完全一致。
cat > "$WORK/sbxinit" <<INIT
#!/bin/sh
mount -t proc proc /proc 2>/dev/null
mount -t sysfs sys /sys 2>/dev/null
mount -t tmpfs tmpfs /run 2>/dev/null
mount -t devtmpfs dev /dev 2>/dev/null
mkdir -p /dev/pts && mount -t devpts devpts /dev/pts 2>/dev/null

# PATH 显式导出:kernel 传给 PID1 的环境几乎是空的,而 vsock-exec-agent 用 subprocess
# 继承本进程环境 → 不导出的话 exec 里找不到 /usr/local/bin 下的 node/npm/claude/openclaw
# (web 预设当年就是踩这个坑才改用 python 绝对路径)。
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# 预打包运行环境可在镜像层放 /etc/sbx-env(KEY=VALUE 每行一条),这里导出,
# 之后启动的 vsock-exec-agent 会继承 → exec 出来的命令直接拿到这些变量。
if [ -f /etc/sbx-env ]; then set -a; . /etc/sbx-env; set +a; fi

ip link set lo up 2>/dev/null

SBX_IP=\$(cat /proc/cmdline | tr ' ' '\n' | sed -n 's/^SBX_IP=//p')
SBX_GW=\$(cat /proc/cmdline | tr ' ' '\n' | sed -n 's/^SBX_GW=//p')
ip link set eth0 up 2>/dev/null
if [ -n "\$SBX_IP" ]; then
  ip addr add \${SBX_IP}/30 dev eth0 2>/dev/null
  [ -n "\$SBX_GW" ] && ip route add default via \$SBX_GW 2>/dev/null
  echo "[sbxinit] net configured: \$SBX_IP gw \$SBX_GW" > /dev/console
fi
echo "nameserver 8.8.8.8" > /etc/resolv.conf 2>/dev/null

mkdir -p /root/.ssh /run/sshd
chmod 700 /root /root/.ssh
echo "$PUBKEY" > /root/.ssh/authorized_keys
chmod 700 /root/.ssh; chmod 600 /root/.ssh/authorized_keys
chown -R root:root /root/.ssh 2>/dev/null
sed -i 's/^#\?StrictModes.*/StrictModes no/' /etc/ssh/sshd_config 2>/dev/null || echo "StrictModes no" >> /etc/ssh/sshd_config
[ -f /etc/ssh/ssh_host_ed25519_key ] || ssh-keygen -A 2>/dev/null
/usr/sbin/sshd 2>/dev/null && echo "[sbxinit] sshd started" > /dev/console || echo "[sbxinit] sshd FAILED" > /dev/console

python3 /sbin/vsock-exec-agent.py > /dev/console 2>&1 &
echo "[sbxinit] vsock-exec-agent started (pid \$!)" > /dev/console

# web 预设:自带站点则开机自起 :80(min 模板无 /web,不触发)。
# 用 python 绝对路径 —— sbxinit 由 /bin/sh 执行,PATH 里未必含 /usr/local/bin。
if [ -f /web/index.html ]; then
  PY=/usr/local/bin/python3
  [ -x "\$PY" ] || PY=python3
  (cd /web && setsid \$PY -m http.server 80 >/tmp/web.log 2>&1 &)
  echo "[sbxinit] demo web on :80 (from /web)" > /dev/console
fi

echo "[sbxinit] microVM booted ($NAME)" > /dev/console

i=0
while true; do
  i=\$((i+1))
  echo "[heartbeat] count=\$i" > /dev/console
  echo "\$i" > /run/heartbeat
  sleep 2
done
INIT
chmod +x "$WORK/sbxinit"

# ---------- 2. docker 造 rootfs 基底(通用:python + iproute2 + sshd)----------
cat > "$WORK/Dockerfile" <<'DOCKER'
FROM public.ecr.aws/docker/library/python:3.12-slim
RUN sed -i 's|deb.debian.org|cdn-aws.deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
 && apt-get update && apt-get install -y --no-install-recommends \
    iproute2 openssh-server iputils-ping \
 && rm -rf /var/lib/apt/lists/* \
 && sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config \
 && sed -i 's/^#\?PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config
DOCKER

# ---------- 2.1 预打包运行环境:在镜像里装 Node.js + CLI ----------
# 放在 Dockerfile 里而不是在导出的 rootfs 目录里做:docker build --platform 能跨架构
# (走 qemu),而宿主上跑 npm 装出来的原生依赖架构会错。
# Debian 仓库的 nodejs 太老(claude-code/openclaw 的 engines 都要 node >=22.22.3),
# 所以直接取 nodejs.org 官方 tarball 解到 /usr/local。
node_layer() {
  cat <<EOF

# Node.js ${NODE_VERSION}(官方 tarball;dpkg 架构 → nodejs.org 的 x64/arm64 命名)
RUN set -eux; \\
    apt-get update && apt-get install -y --no-install-recommends \\
      ca-certificates curl xz-utils git; \\
    rm -rf /var/lib/apt/lists/*; \\
    case "\$(dpkg --print-architecture)" in \\
      amd64) NARCH=x64 ;; \\
      arm64) NARCH=arm64 ;; \\
      *) echo "unsupported arch: \$(dpkg --print-architecture)" >&2; exit 1 ;; \\
    esac; \\
    curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-\$NARCH.tar.xz" \\
      -o /tmp/node.tar.xz; \\
    tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 --no-same-owner; \\
    rm -f /tmp/node.tar.xz; \\
    node --version && npm --version
EOF
}

case "$NAME" in
  claude-code)
    node_layer >> "$WORK/Dockerfile"
    cat >> "$WORK/Dockerfile" <<EOF
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} \\
 && npm cache clean --force \\
 && claude --version
EOF
    ;;
  openclaw)
    node_layer >> "$WORK/Dockerfile"
    cat >> "$WORK/Dockerfile" <<EOF
RUN npm install -g openclaw@${OPENCLAW_VERSION} \\
 && npm cache clean --force \\
 && openclaw --version
EOF
    ;;
esac

docker build --platform "$PLATFORM" -t "sbx-rootfs:$NAME" "$WORK"
CID=$(docker create --platform "$PLATFORM" "sbx-rootfs:$NAME" sleep infinity)
mkdir -p "$WORK/rootfs"
docker export "$CID" | tar -C "$WORK/rootfs" -xf -
docker rm "$CID" >/dev/null

cp "$WORK/sbxinit" "$WORK/rootfs/sbin/sbxinit"
chmod +x "$WORK/rootfs/sbin/sbxinit"
cp "${ROOT}/scripts/vsock-exec-agent.py" "$WORK/rootfs/sbin/vsock-exec-agent.py"
chmod +x "$WORK/rootfs/sbin/vsock-exec-agent.py"

# ---------- 2.5 按 name 叠加应用层预设 ----------
case "$NAME" in
  web)
    # 自带一个好看的 demo 首页;sbxinit 会开机自起 :80。
    mkdir -p "$WORK/rootfs/web"
    cat > "$WORK/rootfs/web/index.html" <<'HTML'
<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Firecracker Sandbox — Live</title>
<style>
  *{box-sizing:border-box} body{margin:0;min-height:100vh;display:grid;place-items:center;
    font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#e6e9ef;
    background:radial-gradient(1200px 800px at 50% -10%,#1b1f3a,#0b0d10)}
  .card{background:#14171c;border:1px solid #262b33;border-radius:18px;padding:44px 52px;
    box-shadow:0 20px 60px rgba(0,0,0,.4);text-align:center;max-width:560px}
  .dot{width:12px;height:12px;border-radius:50%;background:#2ecc71;display:inline-block;
    margin-right:8px;box-shadow:0 0 12px #2ecc71}
  h1{margin:6px 0 4px;font-size:26px} .sub{color:#8b93a1;margin-bottom:24px}
  .badge{display:inline-block;background:#171a33;border:1px solid #2c2f52;color:#7c9cff;
    border-radius:20px;padding:6px 14px;font-size:13px;margin:4px;font-family:ui-monospace,monospace}
  .foot{margin-top:26px;color:#5c6472;font-size:13px}
</style></head><body>
  <div class="card">
    <div><span class="dot"></span><b>Served from inside a Firecracker microVM</b></div>
    <h1>🔥 Sandbox Web is Live</h1>
    <div class="sub">This page is served on port 80 from within an isolated microVM,<br>
      reached through the sandbox port-exposure proxy.</div>
    <div>
      <span class="badge">real guest kernel</span>
      <span class="badge">CoW rootfs</span>
      <span class="badge">/s/&lt;id&gt;/80/</span>
      <span class="badge">image = web</span>
    </div>
    <div class="foot">AWS Self-Hosted Sandbox Platform · Firecracker + node-agent</div>
  </div>
</body></html>
HTML
    ;;
  claude-code)
    # Node + Claude Code CLI 已在 Dockerfile 层装好(claude 在 /usr/local/bin)。
    # 这里只放 guest 侧运行时配置:sbxinit 会 source /etc/sbx-env 并导出。
    cat > "$WORK/rootfs/etc/sbx-env" <<'ENV'
# 由 /sbin/sbxinit 在启动时 source(set -a 导出),vsock exec 的命令会继承。
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
# ⚠️ ANTHROPIC_BASE_URL 必须填【guest 可达】的地址:guest 只有 tap 网段 + 8.8.8.8 DNS,
#    解析不了集群内 DNS(litellm.litellm),所以不能在镜像里写死集群 Service 名。
#    正确做法是控制面/node-agent 在 create 时按所在节点写入,例如:
#      ANTHROPIC_BASE_URL=http://<node-ip>:4000
#    未配置时 claude 会直接打 Anthropic 公网 API(沙盒内没有 key → 需自带 key)。
ENV
    ;;
  openclaw)
    # Node + OpenClaw CLI 已在 Dockerfile 层装好(openclaw 在 /usr/local/bin)。
    cat > "$WORK/rootfs/etc/sbx-env" <<'ENV'
# 由 /sbin/sbxinit 在启动时 source(set -a 导出),vsock exec 的命令会继承。
# OpenClaw 是多渠道 AI 网关,凭据/渠道配置不应烤进镜像 —— 由 create 时注入或 exec 时传入。
ENV
    ;;
  min|"")
    : # 基础模板,无额外内容
    ;;
  *)
    echo "==> WARN: 未知预设 '$NAME',仅产出基础(min 等价)内容;可在脚本 case 里加预设"
    ;;
esac

# 镜像标记:guest 内 `cat /etc/sbx-image` 即可确认自己跑的是哪个模板(e2e 断言用)。
echo "$NAME" > "$WORK/rootfs/etc/sbx-image"

# ---------- 3. 打包 + 上传 ----------
TARBALL="$WORK/rootfs-${NAME}.tar.gz"
tar -C "$WORK/rootfs" -czf "$TARBALL" .
echo "==> rootfs tarball: $(du -h "$TARBALL" | cut -f1)"
# min 兼容旧路径 min-rootfs.tar.gz;其余用 rootfs-{name}.tar.gz
if [ "$NAME" = "min" ]; then
  S3_URI="s3://${S3_BUCKET}/${ROOTFS_PREFIX}/min-rootfs.tar.gz"
else
  S3_URI="s3://${S3_BUCKET}/${ROOTFS_PREFIX}/rootfs-${NAME}.tar.gz"
fi
aws s3 cp "$TARBALL" "$S3_URI" --region "$REGION"
echo "==> uploaded: $S3_URI"
