#!/usr/bin/env bash
# 在 Spot 快照测试节点上准备 ZFS pool 与 FSx for Lustre 挂载(测试用,非生产流程)。
# 环境变量: ZFS_VOLUME_ID FSX_DNS FSX_MOUNT_NAME [ZFS_COMPRESSION=lz4] [ZFS_ARC_MAX_GIB=8]
set -euo pipefail
: "${ZFS_VOLUME_ID:?}" "${FSX_DNS:?}" "${FSX_MOUNT_NAME:?}"
ZFS_COMPRESSION="${ZFS_COMPRESSION:-lz4}"
ARC_GIB="${ZFS_ARC_MAX_GIB:-8}"

# ---- ZFS ----
modprobe zfs
echo $((ARC_GIB * 1024 * 1024 * 1024)) > /sys/module/zfs/parameters/zfs_arc_max
SERIAL="${ZFS_VOLUME_ID/-/}"   # EBS NVMe 序列号形如 vol0123...
DEV=""
for d in /dev/nvme*n1; do
  if [[ "$(cat /sys/block/${d##*/}/device/serial 2>/dev/null | tr -d ' ')" == "$SERIAL" ]]; then DEV="$d"; fi
done
[[ -n "$DEV" ]] || { echo "zfs volume $ZFS_VOLUME_ID not attached"; exit 1; }
if ! zpool list sbxpool >/dev/null 2>&1; then
  zpool import -f sbxpool 2>/dev/null || zpool create -f -o ashift=12 \
    -O compression="$ZFS_COMPRESSION" -O recordsize=1M -O atime=off -O xattr=sa \
    -O mountpoint=none sbxpool "$DEV"
fi
zfs create -p -o mountpoint=none sbxpool/sbx 2>/dev/null || true
zfs set compression="$ZFS_COMPRESSION" sbxpool/sbx
echo "zfs: dev=$DEV $(zfs get -H -o value compression,recordsize sbxpool/sbx | tr '\n' ' ') arc_max=$(cat /sys/module/zfs/parameters/zfs_arc_max)"

# ---- FSx for Lustre ----
if ! mountpoint -q /mnt/fsx; then
  dnf install -y -q lustre-client >/dev/null
  mkdir -p /mnt/fsx
  mount -t lustre -o relatime,flock "${FSX_DNS}@tcp:/${FSX_MOUNT_NAME}" /mnt/fsx
fi
mkdir -p /mnt/fsx/sbx
lfs setstripe -c -1 -S 16M /mnt/fsx/sbx   # 每个快照文件跨全部 OST 条带
echo "fsx: $(lfs df -h /mnt/fsx | grep -c OST) OSTs; stripe=$(lfs getstripe -d /mnt/fsx/sbx | tr '\n' ' ')"
