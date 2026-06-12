#!/bin/bash
# build-fuse-kernel.sh —— 在 Graviton .metal 主机上编译一个带 FUSE 的 arm64 guest 内核
#
# 为什么需要:Firecracker CI 提供的默认 vmlinux 没编 FUSE(实测 `# CONFIG_FUSE_FS is not set`),
# 导致 JuiceFS / s3fs / mountpoint-s3 等任何 FUSE 文件系统在 microVM guest 内挂不上
# (fusermount: fuse device not found)。这是文档 R3 标注的内核 config 风险点。
#
# 本脚本编译 6.1.x + CONFIG_FUSE_FS=y + overlay + inotify 的 arm64 Image,
# 产出 /opt/sbx/vmlinux-fuse,供 Firecracker boot-source 使用。
# 实测:c6g.metal 64 核 native 编译仅几分钟。
#
# 用法(在 .metal 主机,需 root):  sudo bash build-fuse-kernel.sh
set -euxo pipefail

KVER="${KVER:-6.1.128}"           # 与 AL2023 宿主内核同系列;可用环境变量覆盖
OUT="${OUT:-/opt/sbx/vmlinux-fuse}"
mkdir -p "$(dirname "$OUT")"

# 1) 工具链
dnf install -y gcc make flex bison elfutils-libelf-devel openssl-devel bc perl tar xz wget

# 2) 内核源码
cd /opt
[ -d "linux-$KVER" ] || { wget -q "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-$KVER.tar.xz"; tar -xf "linux-$KVER.tar.xz"; }
cd "linux-$KVER"

# 3) 以 Firecracker 推荐的 arm64 microvm config 为基础(取不到则 defconfig)
wget -q "https://raw.githubusercontent.com/firecracker-microvm/firecracker/main/resources/guest_configs/microvm-kernel-ci-aarch64-6.1.config" -O .config 2>/dev/null \
  || wget -q "https://raw.githubusercontent.com/firecracker-microvm/firecracker/main/resources/guest_configs/microvm-kernel-ci-aarch64.config" -O .config 2>/dev/null \
  || make ARCH=arm64 defconfig

# 4) 打开 FUSE / overlay / inotify(R3 三项)
./scripts/config --enable CONFIG_FUSE_FS
./scripts/config --enable CONFIG_VIRTIO_FS 2>/dev/null || true
./scripts/config --enable CONFIG_OVERLAY_FS
./scripts/config --enable CONFIG_INOTIFY_USER
make ARCH=arm64 olddefconfig

echo "=== config 确认(应均为 =y) ==="
grep -E "CONFIG_FUSE_FS=|CONFIG_OVERLAY_FS=|CONFIG_INOTIFY_USER=" .config

# 5) 编译 arm64 Image(注意:arm64 用 Image,不是 x86 的 vmlinux)
make ARCH=arm64 Image -j"$(nproc)"
cp arch/arm64/boot/Image "$OUT"
ls -lh "$OUT"
file "$OUT"
echo "=== 完成:$OUT (FUSE-enabled guest kernel) ==="
echo "在 Firecracker vmconfig 的 boot-source.kernel_image_path 指向它即可。"
