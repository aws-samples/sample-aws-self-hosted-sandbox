#!/bin/bash
# build-fuse-kernel.sh —— 在 .metal 主机上编译一个带 FUSE 的 guest 内核(arm64 或 x86_64)
#
# 为什么需要:Firecracker CI 提供的默认 vmlinux 没编 FUSE(实测 `# CONFIG_FUSE_FS is not set`),
# 导致 JuiceFS / s3fs / mountpoint-s3 等任何 FUSE 文件系统在 microVM guest 内挂不上
# (fusermount: fuse device not found)。这是文档 R3 标注的内核 config 风险点。
#
# 本脚本编译 6.1.x + CONFIG_FUSE_FS=y + overlay + inotify 的 guest 内核,
# 产出 /opt/sbx/vmlinux-fuse,供 Firecracker boot-source 使用。
# 实测:c6g.metal 64 核 native 编译仅几分钟。
#
# 架构:由 ARCH 环境变量控制(aarch64=Graviton[默认] / x86_64=Intel)。
#   - aarch64: make ARCH=arm64,产物 arch/arm64/boot/Image
#   - x86_64 : make ARCH=x86, 产物 arch/x86/boot/bzImage
#
# 用法(在 .metal 主机,需 root):
#   sudo bash build-fuse-kernel.sh                 # 默认探测宿主架构
#   sudo ARCH=x86_64 bash build-fuse-kernel.sh     # Intel x86 节点
set -euxo pipefail

# 架构:默认探测宿主架构;可用 ARCH 环境变量覆盖(aarch64 / x86_64)
ARCH="${ARCH:-$(uname -m)}"
case "$ARCH" in
  aarch64|arm64)
    ARCH=aarch64
    KARCH=arm64                              # Linux kernel 的 ARCH= 取值
    KIMAGE=arch/arm64/boot/Image             # arm64 用 Image
    KCONFIG_ARCH=aarch64                     # Firecracker guest_configs 文件名里的架构
    ;;
  x86_64|amd64)
    ARCH=x86_64
    KARCH=x86                                # Linux kernel 的 ARCH= 取值
    # Firecracker x86 加载未压缩 ELF vmlinux(源码根目录),不是 bzImage。
    # 依据官方 docs/rootfs-and-kernel-setup.md:"kernel image under ./vmlinux (for x86)
    # or ./arch/arm64/boot/Image (for aarch64)"。
    KIMAGE=vmlinux
    KCONFIG_ARCH=x86_64
    ;;
  *)
    echo "ERROR: 不支持的架构 ARCH=$ARCH(仅支持 aarch64 / x86_64)" >&2
    exit 1
    ;;
esac

KVER="${KVER:-5.10.259}"          # 与现有 guest CI 内核(5.10.223)同系列的最新 longterm。
                                  # 注:kernel.org 只保留每系列最新点版本,旧版会 404;实测 us-east-1 从
                                  # cdn.kernel.org 下 v6.x 点版本会 404,v5.x 正常,故默认锁 5.10。可用 KVER 覆盖。
KMM="$(echo "$KVER" | cut -d. -f1,2)"  # 主.次(如 5.10),用于取对应的 microvm config
KMAJ="$(echo "$KVER" | cut -d. -f1)"   # 主版本(如 5),用于 kernel.org 路径 v${KMAJ}.x
OUT="${OUT:-/opt/sbx/vmlinux-fuse}"
mkdir -p "$(dirname "$OUT")"

# 1) 工具链
dnf install -y gcc make flex bison elfutils-libelf-devel openssl-devel bc perl tar xz wget

# 2) 内核源码(路径按主版本 v${KMAJ}.x;curl 比 wget 对 CDN 更稳,失败即报错)
cd /opt
[ -d "linux-$KVER" ] || { curl -fsSL "https://cdn.kernel.org/pub/linux/kernel/v${KMAJ}.x/linux-$KVER.tar.xz" -o "linux-$KVER.tar.xz"; tar -xf "linux-$KVER.tar.xz"; }
cd "linux-$KVER"

# 3) 以 Firecracker 推荐的 microvm config 为基础(按 主.次 版本取;取不到则退无版本号,再退 defconfig)
curl -fsSL "https://raw.githubusercontent.com/firecracker-microvm/firecracker/main/resources/guest_configs/microvm-kernel-ci-${KCONFIG_ARCH}-${KMM}.config" -o .config 2>/dev/null \
  || curl -fsSL "https://raw.githubusercontent.com/firecracker-microvm/firecracker/main/resources/guest_configs/microvm-kernel-ci-${KCONFIG_ARCH}.config" -o .config 2>/dev/null \
  || make ARCH="$KARCH" defconfig

# 4) 打开 FUSE / overlay / inotify(R3 三项)
./scripts/config --enable CONFIG_FUSE_FS
./scripts/config --enable CONFIG_VIRTIO_FS 2>/dev/null || true
./scripts/config --enable CONFIG_OVERLAY_FS
./scripts/config --enable CONFIG_INOTIFY_USER
make ARCH="$KARCH" olddefconfig

echo "=== config 确认(应均为 =y) ==="
grep -E "CONFIG_FUSE_FS=|CONFIG_OVERLAY_FS=|CONFIG_INOTIFY_USER=" .config

# 5) 编译 guest 内核镜像(arm64 用 Image / x86 用未压缩 ELF vmlinux,见 $KIMAGE)
KTARGET=$(basename "$KIMAGE")               # Image(arm64) 或 vmlinux(x86)
make ARCH="$KARCH" "$KTARGET" -j"$(nproc)"
cp "$KIMAGE" "$OUT"
ls -lh "$OUT"
file "$OUT"
echo "=== 完成:$OUT (FUSE-enabled ${ARCH} guest kernel) ==="
echo "在 Firecracker vmconfig 的 boot-source.kernel_image_path 指向它即可。"
