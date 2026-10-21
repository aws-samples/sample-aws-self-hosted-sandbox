#!/usr/bin/env bash
# 独立 EC2(嵌套虚拟化)上排查 KVM_EXIT_FAIL_ENTRY:在 nested-cap-probe.sh 的阶梯加压基础上,
# 支持重载 kvm_intel 参数(preemption_timer / dump_invalid_vmcs)、vCPU 绑核,以及快照"年龄"恢复测试。
# 经 SSM 以 root 执行:
#   failentry-probe.sh setup                        # 下载 firecracker / guest 内核 / rootfs(只需一次)
#   failentry-probe.sh ladder                       # 阶梯加压,环境变量见下
#   failentry-probe.sh snap-create                  # 起 SNAP_N 个 VM,Full 快照后杀掉
#   failentry-probe.sh snap-restore                 # 从快照恢复,观察 HOLD_S 秒
# 环境变量:TAG STEPS HOLD_S MEM_MIB KVM_PARAMS(如 "preemption_timer=0 dump_invalid_vmcs=1")
#          PIN(1 = 绑核) PIN_GROUP(每组 CPU 数,默认 1) REPIN REPIN_ROUNDS SNAP_N ROOTFS_S3 FC_VER
# 结果写 /opt/probe/results/<TAG>.jsonl,失败时的 VMCS dump 写 /opt/probe/results/<TAG>.dmesg
set -uo pipefail
W=/opt/probe; R=$W/results; mkdir -p $R; cd $W
FC_VER="${FC_VER:-v1.16.1}"; TAG="${TAG:-run}"; MEM_MIB="${MEM_MIB:-128}"; HOLD_S="${HOLD_S:-120}"
OUT=$R/$TAG.jsonl
log() { echo "$*" | tee -a "$OUT"; }

kvm_reload() {
  pgrep -x firecracker >/dev/null && { pkill -9 -x firecracker; sleep 2; }
  modprobe -r kvm_intel && modprobe kvm_intel ${KVM_PARAMS:-} || { echo "kvm_intel reload failed"; exit 1; }
  chmod 666 /dev/kvm
}

kvm_params_json() {
  local p out=""
  for p in preemption_timer enable_apicv pml dump_invalid_vmcs nested ept; do
    [ -r /sys/module/kvm_intel/parameters/$p ] && out="$out\"$p\":\"$(cat /sys/module/kvm_intel/parameters/$p)\","
  done
  echo "{${out%,}}"
}

vm_cfg() {
  cat <<CFG
{"boot-source":{"kernel_image_path":"$W/vmlinux","boot_args":"console=ttyS0 reboot=k panic=1 pci=off init=/sbin/sbxinit"},
 "drives":[{"drive_id":"rootfs","path_on_host":"$W/rootfs.ext4","is_root_device":true,"is_read_only":true}],
 "machine-config":{"vcpu_count":1,"mem_size_mib":$MEM_MIB}}
CFG
}

# PIN=1 时第 i 个 VM 绑到第 i%组数 组 CPU(每组 PIN_GROUP 个连续 CPU,默认 1 = 单核绑定)
pin_prefix() {
  [ "${PIN:-0}" = 1 ] || return 0
  local g=${PIN_GROUP:-1} groups=$(( $(nproc) / ${PIN_GROUP:-1} )) first
  first=$(( ($1 % groups) * g )); echo "taskset -c $first-$(( first + g - 1 ))"
}

count_step() {
  local n=$1
  local alive failentry booted
  alive=$(pgrep -x firecracker | wc -l)
  failentry=$(grep -l KVM_EXIT_FAIL_ENTRY $W/vms/*/vm.log 2>/dev/null | wc -l)
  booted=$(grep -l 'microVM booted\|heartbeat\|Artificially kick' $W/vms/*/vm.log 2>/dev/null | wc -l)
  log "{\"event\":\"step\",\"tag\":\"$TAG\",\"launched\":$n,\"alive\":$alive,\"booted\":$booted,\"fail_entry\":$failentry,\"load1\":$(cut -d' ' -f1 /proc/loadavg),\"mem_avail_mib\":$(( $(grep MemAvailable /proc/meminfo | awk '{print $2}') / 1024 )),\"ts\":$(date +%s)}"
  FAILS=$failentry
}

save_dmesg() {
  dmesg | grep -n -E 'VMCS|vmcs|VM-entry|PinBased|ProcBased|EntryControls|ExitControls|TSC|reason|FAIL|kvm' | tail -400 > $R/$TAG.dmesg
  grep -h -A3 KVM_EXIT_FAIL_ENTRY $W/vms/*/vm.log 2>/dev/null | head -40 > $R/$TAG.vmlog
}

case "${1:-}" in
setup)
  dnf install -y -q e2fsprogs tar gzip jq >/dev/null 2>&1
  curl -sfL "https://github.com/firecracker-microvm/firecracker/releases/download/${FC_VER}/firecracker-${FC_VER}-x86_64.tgz" | tar xz
  install -m755 release-${FC_VER}-x86_64/firecracker-${FC_VER}-x86_64 /usr/local/bin/firecracker
  curl -sfL -o vmlinux https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.10/x86_64/vmlinux-5.10.223
  aws s3 cp "${ROOTFS_S3:?}" rootfs.tar.gz --only-show-errors
  rm -f rootfs.ext4; truncate -s 1G rootfs.ext4 && mkfs.ext4 -q rootfs.ext4 && mkdir -p mnt && mount rootfs.ext4 mnt \
    && tar xzf rootfs.tar.gz -C mnt && umount mnt
  echo "{\"event\":\"setup\",\"kernel\":\"$(uname -r)\",\"vcpus\":$(nproc),\"virt\":\"$(systemd-detect-virt)\",\"cpu\":\"$(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2 | xargs)\",\"microcode\":\"$(grep -m1 microcode /proc/cpuinfo | awk '{print $3}')\"}"
  ;;
ladder)
  ulimit -n 1048576
  kvm_reload; dmesg -C
  rm -rf $W/vms; mkdir -p $W/vms; : > "$OUT"
  log "{\"event\":\"start\",\"tag\":\"$TAG\",\"kernel\":\"$(uname -r)\",\"vcpus\":$(nproc),\"mem_mib\":$MEM_MIB,\"pin\":\"${PIN:-0}\",\"pin_group\":\"${PIN_GROUP:-1}\",\"hold_s\":$HOLD_S,\"kvm_intel\":$(kvm_params_json)}"
  n=0; consecutive=0
  for target in ${STEPS//,/ }; do
    while [ $n -lt $target ]; do
      n=$((n+1)); d=$W/vms/$n; mkdir -p $d; vm_cfg > $d/cfg.json
      $(pin_prefix $n) setsid /usr/local/bin/firecracker --no-api --config-file $d/cfg.json > $d/vm.log 2>&1 < /dev/null &
      [ $((n % 16)) -eq 0 ] && sleep 1
    done
    sleep "$HOLD_S"; count_step $n
    if [ "$FAILS" -gt 0 ]; then consecutive=$((consecutive+1)); else consecutive=0; fi
    [ $consecutive -ge 2 ] && break
  done
  # REPIN=1:全部 VM 改绑到下一个 CPU(模拟偶发的负载再均衡),重复 REPIN_ROUNDS 轮
  if [ "${REPIN:-0}" = 1 ]; then
    for r in $(seq 1 ${REPIN_ROUNDS:-1}); do
      for p in $(pgrep -x firecracker); do
        c=$(taskset -cp $p | awk -F': ' '{print $2}' | cut -d- -f1 | cut -d, -f1)
        taskset -a -cp $(( (c + 1) % $(nproc) )) $p > /dev/null
      done
      sleep "$HOLD_S"; count_step "$n"
    done
  fi
  save_dmesg; pkill -9 -x firecracker; log "{\"event\":\"done\",\"tag\":\"$TAG\"}"
  ;;
snap-create)
  kvm_reload; dmesg -C
  rm -rf $W/vms $W/snaps; mkdir -p $W/vms $W/snaps; : > "$OUT"
  N=${SNAP_N:-8}
  for i in $(seq 1 $N); do
    d=$W/vms/$i; mkdir -p $d; vm_cfg > $d/cfg.json; rm -f $d/api.sock
    setsid /usr/local/bin/firecracker --api-sock $d/api.sock --config-file $d/cfg.json > $d/vm.log 2>&1 < /dev/null &
  done
  sleep 30
  ok=0
  for i in $(seq 1 $N); do
    d=$W/vms/$i; s=$W/snaps/$i; mkdir -p $s
    curl -sf --unix-socket $d/api.sock -X PATCH http://l/vm -d '{"state":"Paused"}' \
      && curl -sf --unix-socket $d/api.sock -X PUT http://l/snapshot/create \
           -d "{\"snapshot_type\":\"Full\",\"snapshot_path\":\"$s/vm.snap\",\"mem_file_path\":\"$s/vm.mem\"}" \
      && ok=$((ok+1))
  done
  pkill -9 -x firecracker
  log "{\"event\":\"snap-create\",\"tag\":\"$TAG\",\"n\":$N,\"ok\":$ok,\"ts\":$(date +%s),\"kvm_intel\":$(kvm_params_json)}"
  ;;
snap-restore)
  # 不重载 kvm_intel(保持与创建快照时同一模块实例无关紧要,但允许用 KVM_PARAMS 做对照)
  [ -n "${KVM_PARAMS+x}" ] && kvm_reload
  dmesg -C; : > "$OUT"
  created=$(stat -c %Y $W/snaps/1/vm.snap)
  N=$(ls $W/snaps | wc -l); rm -rf $W/vms; mkdir -p $W/vms
  for i in $(seq 1 $N); do
    d=$W/vms/$i; mkdir -p $d; rm -f $d/api.sock
    setsid /usr/local/bin/firecracker --api-sock $d/api.sock > $d/vm.log 2>&1 < /dev/null &
  done
  sleep 2; loaded=0
  for i in $(seq 1 $N); do
    d=$W/vms/$i; s=$W/snaps/$i
    curl -sf --unix-socket $d/api.sock -X PUT http://l/snapshot/load \
      -d "{\"snapshot_path\":\"$s/vm.snap\",\"mem_backend\":{\"backend_type\":\"File\",\"backend_path\":\"$s/vm.mem\"},\"resume_vm\":true}" \
      && loaded=$((loaded+1))
  done
  sleep "$HOLD_S"; count_step $N
  log "{\"event\":\"snap-restore\",\"tag\":\"$TAG\",\"n\":$N,\"loaded\":$loaded,\"snapshot_age_s\":$(( $(date +%s) - created )),\"kvm_intel\":$(kvm_params_json)}"
  save_dmesg; pkill -9 -x firecracker
  ;;
*) echo "usage: $0 setup|ladder|snap-create|snap-restore"; exit 2 ;;
esac
