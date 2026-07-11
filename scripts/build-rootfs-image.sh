#!/usr/bin/env bash
# 构建【命名 rootfs 模板】—— 泛化自 build-min-rootfs.sh。
# 每个模板 = 一个可启动 arm64 rootfs.tar.gz,含 sbxinit(PID1) + vsock-exec-agent(exec 主通道),
# 在其上叠加该"镜像"的应用层。产出 rootfs-{name}.tar.gz 上传 S3,节点拉取造 /opt/sbx/rootfs-{name}.ext4。
# create 时按沙盒的 image 字段选模板(见 node-agent op_create 的 rootfs_template)。
#
# 用法: bash scripts/build-rootfs-image.sh <name> <s3-bucket>
#   name = min      → 等价于 build-min-rootfs.sh(基础模板)
#   name = web      → 自带 demo 首页 + 开机自起 :80(端口暴露打开即见站点)
#   其它 name        → 目前回退到 min 内容(可在下方 case 里加预设)
set -euo pipefail

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
docker build --platform linux/arm64 -t "sbx-rootfs:$NAME" "$WORK"
CID=$(docker create --platform linux/arm64 "sbx-rootfs:$NAME" sleep infinity)
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
  min|"")
    : # 基础模板,无额外内容
    ;;
  *)
    echo "==> WARN: 未知预设 '$NAME',仅产出基础(min 等价)内容;可在脚本 case 里加预设"
    ;;
esac

# ---------- 3. 打包 + 上传 ----------
TARBALL="$WORK/rootfs-${NAME}.tar.gz"
tar -C "$WORK/rootfs" -czf "$TARBALL" .
echo "==> rootfs tarball: $(du -h "$TARBALL" | cut -f1)"
# min 兼容旧路径 min-rootfs.tar.gz;其余用 rootfs-{name}.tar.gz
if [ "$NAME" = "min" ]; then
  S3_URI="s3://${S3_BUCKET}/rootfs/min-rootfs.tar.gz"
else
  S3_URI="s3://${S3_BUCKET}/rootfs/rootfs-${NAME}.tar.gz"
fi
aws s3 cp "$TARBALL" "$S3_URI" --region "$REGION"
echo "==> uploaded: $S3_URI"
