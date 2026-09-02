# EBS bandwidth benchmark

Creates two short-lived, otherwise identical EC2 instances:

- `default`: normal network/EBS bandwidth weighting.
- `ebs1`: `ebs-1` bandwidth weighting.

Each instance has two gp3 data volumes. Its bootstrap script runs a 45-second
1 MiB sequential-write fio test against one volume, then a second test against
a two-volume RAID0 array. Results are written to:

```text
/var/lib/ebs-bandwidth-benchmark/result.json
```

Set `associate_public_ip_address = true` only when the selected subnet routes
through an Internet Gateway but has neither NAT nor the required SSM VPC
endpoints. The address is ephemeral and disappears with the instance.

The instances use an SSM instance profile, so the result can be read without
SSH:

```bash
aws ssm send-command \
  --region us-east-1 \
  --instance-ids i-0123456789abcdef0 \
  --document-name AWS-RunShellScript \
  --parameters '{"commands":["cat /var/lib/ebs-bandwidth-benchmark/result.json"]}'
```

All instance-created EBS volumes use `delete_on_termination = true`. Always
destroy the stack after collecting results, and verify there are no resources
left with the `EbsBenchmarkTest` tag.
