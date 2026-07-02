#!/bin/bash
# 在 EKS metal 节点上安装 Firecracker 素材(B2 修法2:pre_bootstrap 未生效时手动补)
# 经 SSM 下发。装:firecracker 二进制 + guest kernel + 从 S3 拉 rootfs 造 ext4。
set -uxo pipefail
exec >> /var/log/fc-setup.log 2>&1
echo "[fc-setup] START $(date)"

ROOTFS_S3="${ROOTFS_S3:-s3://my-sandbox-snapshots-551344820358/rootfs/min-rootfs.tar.gz}"
REGION="${REGION:-us-east-1}"
ARCH=aarch64

mkdir -p /opt/sbx /var/lib/sbx

# 1. Firecracker 二进制
if [ ! -x /usr/local/bin/firecracker ]; then
  VER=v1.16.0
  curl -sfL "https://github.com/firecracker-microvm/firecracker/releases/download/${VER}/firecracker-${VER}-${ARCH}.tgz" -o /tmp/fc.tgz
  tar -xzf /tmp/fc.tgz -C /tmp
  mv "/tmp/release-${VER}-${ARCH}/firecracker-${VER}-${ARCH}" /usr/local/bin/firecracker
  chmod +x /usr/local/bin/firecracker
fi
/usr/local/bin/firecracker --version | head -1

# 2. guest kernel (CI vmlinux)
if [ ! -f /opt/sbx/vmlinux ]; then
  curl -sfL "https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.10/aarch64/vmlinux-5.10.223" -o /opt/sbx/vmlinux
fi
ls -lh /opt/sbx/vmlinux

# 3. rootfs: 从 S3 拉 tar.gz → 造 2GB ext4
if [ ! -f /opt/sbx/rootfs.ext4 ]; then
  aws s3 cp "$ROOTFS_S3" /tmp/rootfs.tar.gz --region "$REGION"
  dd if=/dev/zero of=/opt/sbx/rootfs.ext4 bs=1M count=2048 status=none
  mkfs.ext4 /opt/sbx/rootfs.ext4 -q
  mkdir -p /tmp/rootfs_mount
  mount /opt/sbx/rootfs.ext4 /tmp/rootfs_mount
  tar -xzf /tmp/rootfs.tar.gz -C /tmp/rootfs_mount
  umount /tmp/rootfs_mount
fi
ls -lh /opt/sbx/rootfs.ext4

# 4. ip_forward(node-agent 配 NAT 需要)
sysctl -w net.ipv4.ip_forward=1 || true

echo "[fc-setup] DONE $(date)"
