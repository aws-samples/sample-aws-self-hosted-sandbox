#!/bin/bash
# verify-x86-feasibility.sh —— 在一台 Intel x86 .metal(如 c5n.metal)上验证
# "Sandbox 改 x86" 的全链路可行性。只读/临时验证,不改动任何持久基础设施。
#
# 背景:代码已把架构参数化(node_arch=amd64),但以下几项是"靠公开信息推断、需真机坐实"的:
#   A. /dev/kvm 在 c5n.metal 上可用(确认是真裸金属)
#   B. Firecracker x86_64 二进制能跑(--version)
#   C. Firecracker CI 的 x86_64 vmlinux-5.10.223 能下载 + 能 boot 一个 microVM
#   D. 自编带 FUSE 的 x86 内核(build-fuse-kernel.sh ARCH=x86_64)产物格式能被 Firecracker 接受
#      —— 这是当初标注的最高风险点(KIMAGE 取 vmlinux 还是 bzImage)
#
# 用法(在 c5n.metal 上,root):
#   sudo bash verify-x86-feasibility.sh            # A~C(快,几分钟)
#   sudo RUN_FUSE_KERNEL=1 bash verify-x86-feasibility.sh   # 含 D(编内核,十几分钟)
#
# 退出码:0=全过;非0=有项失败(看输出 [FAIL])。
set -uo pipefail

PASS=0; FAIL=0
ok(){ echo "[PASS] $*"; PASS=$((PASS+1)); }
ng(){ echo "[FAIL] $*"; FAIL=$((FAIL+1)); }
info(){ echo "[..]  $*"; }

ARCH="$(uname -m)"
info "uname -m = $ARCH"
[ "$ARCH" = "x86_64" ] || ng "本机不是 x86_64(应在 c5n.metal/c5.metal 上跑);继续仅供参考"

# 在 cd 之前解析脚本所在目录(D 步骤要找同目录的 build-fuse-kernel.sh)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

# boot_microvm <kernel_path> <log_file> —— 用最小 ext4 rootfs boot 一个 microVM,
# 看到 X86_BOOT_OK_MARKER 即证明内核 boot 到 userspace 并跑起了 init。
# 用 drives(Firecracker v1.16 要求 drives 必填,不能只给 initrd)+ 静态编译 C init
# (AL2023 无 busybox),不依赖任何动态库。返回 0=boot 成功。
ROOTFS_READY=""
build_min_rootfs() {
  [ -n "$ROOTFS_READY" ] && return 0
  command -v gcc >/dev/null 2>&1 || dnf install -y gcc glibc-static >/dev/null 2>&1 || dnf install -y gcc >/dev/null 2>&1 || true
  cat > "$WORK/init.c" <<'C'
#include <unistd.h>
#include <sys/reboot.h>
#include <linux/reboot.h>
int main(){ const char*m="\nX86_BOOT_OK_MARKER_8888\n"; write(1,m,24); sync(); reboot(LINUX_REBOOT_CMD_POWER_OFF); for(;;); }
C
  gcc -static -O2 -o "$WORK/init" "$WORK/init.c" || return 1
  dd if=/dev/zero of="$WORK/rootfs.ext4" bs=1M count=16 status=none
  mkfs.ext4 -q "$WORK/rootfs.ext4"
  local mnt; mnt=$(mktemp -d)
  mount "$WORK/rootfs.ext4" "$mnt"
  mkdir -p "$mnt/sbin" "$mnt/dev" "$mnt/proc" "$mnt/sys"
  cp "$WORK/init" "$mnt/sbin/init"
  umount "$mnt"; rmdir "$mnt" 2>/dev/null || true
  ROOTFS_READY=1
}
boot_microvm() {
  local kern="$1" logf="$2"
  build_min_rootfs || { echo "rootfs build failed" >"$logf"; return 1; }
  cat > "$WORK/vm.json" <<JSON
{ "boot-source": { "kernel_image_path": "$kern", "boot_args": "console=ttyS0 reboot=k panic=1 pci=off init=/sbin/init" },
  "drives": [{ "drive_id":"rootfs","path_on_host":"$WORK/rootfs.ext4","is_root_device":true,"is_read_only":false }],
  "machine-config": { "vcpu_count":1,"mem_size_mib":256 } }
JSON
  timeout 25 "$FCBIN" --no-api --config-file "$WORK/vm.json" >"$logf" 2>&1 || true
  grep -qE "X86_BOOT_OK_MARKER_8888" "$logf"
}

# ---------- A. /dev/kvm ----------
if [ -e /dev/kvm ]; then
  ok "A. /dev/kvm 存在(确认是裸金属,支持 Firecracker)"
else
  ng "A. /dev/kvm 不存在 —— 不是 .metal 或未开虚拟化,Firecracker/Kata 无法运行。后续多半失败"
fi

# ---------- B. Firecracker x86_64 二进制 ----------
info "B. 下载 Firecracker x86_64 发行包..."
VER=$(curl -sf https://api.github.com/repos/firecracker-microvm/firecracker/releases/latest \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['tag_name'])" 2>/dev/null || echo "v1.16.0")
info "    latest tag = $VER"
if curl -sfL "https://github.com/firecracker-microvm/firecracker/releases/download/${VER}/firecracker-${VER}-x86_64.tgz" -o fc.tgz; then
  tar -xzf fc.tgz
  FCBIN="$(pwd)/release-${VER}-x86_64/firecracker-${VER}-x86_64"
  if [ -x "$FCBIN" ] && "$FCBIN" --version >/dev/null 2>&1; then
    ok "B. Firecracker x86_64 可执行:$($FCBIN --version | head -1)"
  else
    ng "B. Firecracker x86_64 下载到了但无法执行"
    FCBIN=""
  fi
else
  ng "B. 无法下载 firecracker-${VER}-x86_64.tgz(命名假设或网络问题)"
  FCBIN=""
fi

# ---------- C. CI x86_64 内核 + boot microVM ----------
info "C. 下载 Firecracker CI x86_64 vmlinux-5.10.223..."
if curl -sfL "https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.10/x86_64/vmlinux-5.10.223" -o vmlinux-ci; then
  ok "C1. CI x86_64 内核下载成功($(du -h vmlinux-ci | cut -f1))"
  if [ -n "$FCBIN" ] && [ -e /dev/kvm ]; then
    info "C2. 用最小 ext4 rootfs boot 一个 microVM(约 25s)..."
    if boot_microvm "$WORK/vmlinux-ci" /tmp/x86-verify-fc.log; then
      ok "C2. CI x86_64 内核 boot 到 userspace(init 已执行)"
    else
      ng "C2. microVM 未 boot 到 userspace(fc.log 见 /tmp/x86-verify-fc.log)。首行:$(head -1 /tmp/x86-verify-fc.log 2>/dev/null)"
    fi
  else
    info "C2. 跳过 boot 测试(缺 firecracker 或 /dev/kvm)"
  fi
else
  ng "C1. 无法下载 x86_64/vmlinux-5.10.223(路径假设错误?)"
fi

# ---------- D. 自编带 FUSE 的 x86 内核(可选,最高风险项) ----------
if [ "${RUN_FUSE_KERNEL:-0}" = "1" ]; then
  info "D. 编译带 FUSE 的 x86 guest 内核(ARCH=x86_64,十几分钟)..."
  # build-fuse-kernel.sh:优先脚本同目录,其次 /opt,最后从 S3 拉(真机 UserData 场景)
  KB=""
  for cand in "$SCRIPT_DIR/build-fuse-kernel.sh" /opt/x86test/build-fuse-kernel.sh /opt/build-fuse-kernel.sh; do
    [ -f "$cand" ] && { KB="$cand"; break; }
  done
  if [ -f "$KB" ]; then
    if ARCH=x86_64 OUT="$WORK/vmlinux-fuse" bash "$KB" >build.log 2>&1; then
      ok "D1. build-fuse-kernel.sh ARCH=x86_64 编译成功"
      file "$WORK/vmlinux-fuse"
      # 用自编内核 boot,确认 KIMAGE 格式(vmlinux,非 bzImage)被 Firecracker 接受
      if [ -n "$FCBIN" ] && [ -e /dev/kvm ]; then
        info "D2. 用自编 x86 FUSE 内核 boot microVM..."
        if boot_microvm "$WORK/vmlinux-fuse" /tmp/x86-verify-fcf.log; then
          ok "D2. 自编 x86 FUSE 内核 boot 到 userspace —— KIMAGE 格式正确"
        else
          ng "D2. 自编 x86 内核无法 boot —— 检查 build-fuse-kernel.sh 的 KIMAGE。日志 /tmp/x86-verify-fcf.log。首行:$(head -1 /tmp/x86-verify-fcf.log 2>/dev/null)"
        fi
      fi
    else
      ng "D1. 自编 x86 内核失败,看 $WORK/build.log(已复制到 /tmp/x86-verify-build.log)"
      cp build.log /tmp/x86-verify-build.log 2>/dev/null || true
    fi
  else
    ng "D. 找不到 build-fuse-kernel.sh(查找:\$SCRIPT_DIR / /opt/x86test / /opt)"
  fi
else
  info "D. 跳过自编 FUSE 内核(设 RUN_FUSE_KERNEL=1 启用 —— 强烈建议至少跑一次坐实最高风险项)"
fi

echo "========================================"
echo "结果:PASS=$PASS  FAIL=$FAIL"
[ "$FAIL" -eq 0 ] && echo "✅ x86 链路可行性验证通过" || echo "❌ 有失败项,见上方 [FAIL] 与 /tmp/x86-verify-*.log"
exit "$FAIL"
