#!/usr/bin/env bash
# 独立 EC2 上探测"同时运行的 Firecracker guest vCPU 上限"(不经过 EKS/node-agent)。
# 每个 VM 1 vCPU / 128 MiB,共享只读 rootfs,无网卡;按阶梯加 VM,每档观察
# HOLD_S 秒,统计存活数与 KVM_EXIT_FAIL_ENTRY 次数,逐档写入 S3。
# 由 EC2 user-data 调用,环境变量:RESULT_S3 STEPS(逗号分隔的目标 VM 数) HOLD_S ROOTFS_S3
set -uo pipefail
RESULT_S3="${RESULT_S3:?}"; STEPS="${STEPS:-32,64,128,192,256,320,384,512}"
HOLD_S="${HOLD_S:-90}"; ROOTFS_S3="${ROOTFS_S3:?}"; FC_VER="${FC_VER:-v1.16.1}"; MEM_MIB="${MEM_MIB:-128}"; TAG="${TAG:-}"
REGION="${AWS_REGION:-us-east-1}"
W=/opt/probe; mkdir -p $W/vms; cd $W
ITYPE=$(curl -s -H "X-aws-ec2-metadata-token: $(curl -s -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 300')" http://169.254.169.254/latest/meta-data/instance-type)
log() { echo "$*" | tee -a $W/result.jsonl; aws s3 cp $W/result.jsonl "$RESULT_S3/$ITYPE$TAG.jsonl" --region $REGION --only-show-errors; }

dnf install -y -q e2fsprogs tar gzip >/dev/null 2>&1
if [ ! -x /usr/local/bin/firecracker ] || [ ! -f $W/rootfs.ext4 ]; then
curl -sfL "https://github.com/firecracker-microvm/firecracker/releases/download/${FC_VER}/firecracker-${FC_VER}-x86_64.tgz" | tar xz
mv release-${FC_VER}-x86_64/firecracker-${FC_VER}-x86_64 /usr/local/bin/firecracker
curl -sfL -o vmlinux https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.10/x86_64/vmlinux-5.10.223
aws s3 cp "$ROOTFS_S3" rootfs.tar.gz --region $REGION --only-show-errors
truncate -s 1G rootfs.ext4 && mkfs.ext4 -q rootfs.ext4 && mkdir -p mnt && mount rootfs.ext4 mnt && tar xzf rootfs.tar.gz -C mnt && umount mnt
fi
rm -rf $W/vms; mkdir -p $W/vms; : > $W/result.jsonl
ulimit -n 1048576
NESTED=$( [ -e /sys/module/kvm_intel/parameters/nested ] && cat /sys/module/kvm_intel/parameters/nested || echo n/a )
log "{\"event\":\"start\",\"instance_type\":\"$ITYPE\",\"host_vcpus\":$(nproc),\"kernel\":\"$(uname -r)\",\"fc\":\"$FC_VER\",\"kvm_intel_nested\":\"$NESTED\",\"virt\":\"$(systemd-detect-virt 2>/dev/null || true)\",\"mem_mib\":$MEM_MIB}"

launch() {
  local i=$1 d=$W/vms/$1
  mkdir -p $d
  cat > $d/cfg.json <<CFG
{"boot-source":{"kernel_image_path":"$W/vmlinux","boot_args":"console=ttyS0 reboot=k panic=1 pci=off init=/sbin/sbxinit"},
 "drives":[{"drive_id":"rootfs","path_on_host":"$W/rootfs.ext4","is_root_device":true,"is_read_only":true}],
 "machine-config":{"vcpu_count":1,"mem_size_mib":$MEM_MIB}}
CFG
  setsid /usr/local/bin/firecracker --no-api --config-file $d/cfg.json > $d/vm.log 2>&1 < /dev/null &
}

n=0; consecutive_fail=0
for target in ${STEPS//,/ }; do
  while [ $n -lt $target ]; do n=$((n+1)); launch $n; [ $((n % 16)) -eq 0 ] && sleep 1; done
  sleep "$HOLD_S"
  alive=$(pgrep -x firecracker | wc -l)
  failentry=$(grep -l KVM_EXIT_FAIL_ENTRY $W/vms/*/vm.log 2>/dev/null | wc -l)
  booted=$(grep -l 'microVM booted\|heartbeat' $W/vms/*/vm.log 2>/dev/null | wc -l)
  log "{\"event\":\"step\",\"instance_type\":\"$ITYPE\",\"launched\":$n,\"alive\":$alive,\"booted\":$booted,\"fail_entry\":$failentry,\"load1\":$(cut -d' ' -f1 /proc/loadavg),\"mem_avail_mib\":$(( $(grep MemAvailable /proc/meminfo | awk '{print $2}') / 1024 ))}"
  if [ "$failentry" -gt 0 ]; then consecutive_fail=$((consecutive_fail+1)); else consecutive_fail=0; fi
  [ $consecutive_fail -ge 2 ] && break
done
log "{\"event\":\"done\",\"instance_type\":\"$ITYPE\"}"
pkill -x firecracker
