set -euo pipefail

exec > >(tee -a /var/log/ebs-bandwidth-benchmark.log | logger -t ebs-benchmark -s 2>/dev/console) 2>&1

RESULT_DIR=/var/lib/ebs-bandwidth-benchmark
RESULT_PATH="$RESULT_DIR/result.json"
mkdir -p "$RESULT_DIR"

fail() {
  local message="$1"
  jq -n \
    --arg status "failed" \
    --arg weighting "$BENCH_WEIGHTING" \
    --arg instance_type "$BENCH_INSTANCE_TYPE" \
    --arg error "$message" \
    '{status:$status, weighting:$weighting, instance_type:$instance_type, error:$error}' \
    >"$RESULT_PATH"
  echo "EBS_BENCHMARK_FAILED: $message"
  exit 1
}

dnf install -y fio mdadm jq nvme-cli || fail "package installation failed"

root_source=$(findmnt -n -o SOURCE /)
root_parent=$(lsblk -no PKNAME "$root_source" | head -1)
if [[ -z "$root_parent" ]]; then
  root_parent=$(basename "$root_source")
fi
root_device="/dev/$root_parent"

data_devices=()
for _ in $(seq 1 120); do
  mapfile -t data_devices < <(
    lsblk -dpno NAME,TYPE |
      awk '$2 == "disk" {print $1}' |
      grep -v -F "$root_device" |
      sort
  )
  if [[ ${#data_devices[@]} -ge 2 ]]; then
    break
  fi
  sleep 2
done

if [[ ${#data_devices[@]} -ne 2 ]]; then
  fail "expected exactly two data disks, found ${#data_devices[@]}: ${data_devices[*]:-none}"
fi

device_one="${data_devices[0]}"
device_two="${data_devices[1]}"

fio_common=(
  --rw=write
  --bs=1M
  --direct=1
  --ioengine=libaio
  --iodepth=64
  --numjobs=1
  --time_based=1
  --runtime=45
  --size=80G
  --group_reporting=1
  --eta=never
  --output-format=json
)

fio \
  --name=single-gp3 \
  --filename="$device_one" \
  "${fio_common[@]}" \
  --output="$RESULT_DIR/single.json" ||
  fail "single-volume fio failed"

mdadm --zero-superblock --force "$device_one" "$device_two" 2>/dev/null || true
mdadm \
  --create /dev/md/ebsbench \
  --force \
  --run \
  --level=0 \
  --raid-devices=2 \
  --chunk=256 \
  "$device_one" \
  "$device_two" ||
  fail "RAID0 creation failed"
udevadm settle

fio \
  --name=raid0-two-gp3 \
  --filename=/dev/md/ebsbench \
  "${fio_common[@]}" \
  --output="$RESULT_DIR/raid0.json" ||
  fail "RAID0 fio failed"

single_bps=$(jq -r '.jobs[0].write.bw_bytes' "$RESULT_DIR/single.json")
single_iops=$(jq -r '.jobs[0].write.iops' "$RESULT_DIR/single.json")
raid_bps=$(jq -r '.jobs[0].write.bw_bytes' "$RESULT_DIR/raid0.json")
raid_iops=$(jq -r '.jobs[0].write.iops' "$RESULT_DIR/raid0.json")

jq -n \
  --arg status "passed" \
  --arg weighting "$BENCH_WEIGHTING" \
  --arg instance_type "$BENCH_INSTANCE_TYPE" \
  --arg device_one "$device_one" \
  --arg device_two "$device_two" \
  --argjson single_bps "$single_bps" \
  --argjson single_iops "$single_iops" \
  --argjson raid_bps "$raid_bps" \
  --argjson raid_iops "$raid_iops" \
  '{
    status:$status,
    weighting:$weighting,
    instance_type:$instance_type,
    devices:[$device_one,$device_two],
    runtime_s:45,
    block_size:"1MiB",
    single_gp3:{
      bytes_per_second:$single_bps,
      mebibytes_per_second:($single_bps / 1048576),
      iops:$single_iops
    },
    raid0_two_gp3:{
      bytes_per_second:$raid_bps,
      mebibytes_per_second:($raid_bps / 1048576),
      iops:$raid_iops
    },
    raid_gain_percent:(
      if $single_bps > 0
      then (($raid_bps - $single_bps) * 100 / $single_bps)
      else null
      end
    )
  }' >"$RESULT_PATH"

echo "EBS_BENCHMARK_RESULT_BEGIN"
cat "$RESULT_PATH"
echo "EBS_BENCHMARK_RESULT_END"
