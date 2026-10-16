#!/usr/bin/env bash
# 在 Amazon Linux 2023 (kernel 6.1) 上从源码编译安装 OpenZFS。
# AL2023 没有官方 ZFS 包,EL9 的 kmod RPM 与 AL2023 内核 ABI 不匹配,只能源码/DKMS。
# 用法: ZFS_VERSION=2.3.9 bash install-zfs-al2023.sh
set -euo pipefail
ZFS_VERSION="${ZFS_VERSION:-2.3.9}"
if modinfo zfs >/dev/null 2>&1 && zfs version 2>/dev/null | grep -q "zfs-${ZFS_VERSION}"; then
  modprobe zfs; zfs version; exit 0
fi
T0=$(date +%s)
dnf install -y -q "kernel-devel-$(uname -r)" gcc make autoconf automake libtool \
  libtirpc-devel libblkid-devel libuuid-devel libudev-devel openssl-devel zlib-devel \
  libaio-devel libattr-devel elfutils-libelf-devel python3 python3-devel ncompress \
  libffi-devel >/dev/null
T1=$(date +%s)
# 不能在 /usr/src 下解包:--prefix=/usr 时内核头文件安装到 /usr/src/zfs-<ver>,
# 与源码目录相同会让 make install 报 "are the same file"。
BUILD=/root/zfs-build; mkdir -p "$BUILD"; cd "$BUILD"
curl -sfL -o "zfs-${ZFS_VERSION}.tar.gz" \
  "https://github.com/openzfs/zfs/releases/download/zfs-${ZFS_VERSION}/zfs-${ZFS_VERSION}.tar.gz"
tar xzf "zfs-${ZFS_VERSION}.tar.gz"
cd "zfs-${ZFS_VERSION}"
./configure --prefix=/usr --sysconfdir=/etc --localstatedir=/var \
  --with-linux="/usr/src/kernels/$(uname -r)" >/tmp/zfs-configure.log 2>&1
make -s -j"$(nproc)" >/tmp/zfs-make.log 2>&1
make -s install >/tmp/zfs-install.log 2>&1
ldconfig
depmod -a
modprobe zfs
T2=$(date +%s)
zfs version
echo "deps_s=$((T1-T0)) build_install_s=$((T2-T1))"
