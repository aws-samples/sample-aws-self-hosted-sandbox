# Isolated Spot recovery test node groups

This stack adds two temporary node groups to the existing EKS cluster:

- one Spot Firecracker data node with a retained gp3 state EBS;
- one same-AZ On-Demand standby with no dedicated state EBS.
- one least-privilege AWS FIS experiment template that targets exactly one
  running Spot instance carrying this run's `SpotRecoveryTest` tag and sends a
  real two-minute interruption notice.

All instances, launch templates, ENIs, and volumes are tagged with
`SpotRecoveryTest=<test_id>`. The stack intentionally reuses the existing
sandbox node IAM role, security group, subnet, and bootstrap user data.

Start the FIS experiment only after the test sandbox and marker are ready:

```bash
aws fis start-experiment \
  --region us-east-1 \
  --experiment-template-id "$(terraform output -raw fis_experiment_template_id)"
```

Example:

```bash
terraform init
terraform apply \
  -var='source_node_group_name=sandbox_amd64-43f191fbd4487a297c26e6db4d' \
  -var='test_id=sr-20260901-a' \
  -var='active_instance_type=r8i.8xlarge' \
  -var='state_ebs_iops=8000' \
  -var='state_ebs_throughput=2000' \
  -var='ebs_bandwidth_weighting=ebs-1'
```

`ebs-1` is experimental in this EKS managed-node-group stack. In the
2026-09-02 live test, the launch template accepted the setting but the EC2
instance still reported `default`. Verify the launched instance before
claiming the weighting is active. Use `../ebs-benchmark` for the authoritative
raw-EC2 comparison.

After testing, destroy the node groups and launch templates, then explicitly
delete retained test state volumes:

```bash
terraform destroy \
  -var='source_node_group_name=sandbox_amd64-43f191fbd4487a297c26e6db4d' \
  -var='test_id=sr-20260901-a'

aws ec2 describe-volumes --region us-east-1 \
  --filters Name=tag:SpotRecoveryTest,Values=sr-20260901-a \
  --query 'Volumes[?State==`available`].VolumeId' --output text
```

Never delete volumes without checking both the test tag and attachment state.
