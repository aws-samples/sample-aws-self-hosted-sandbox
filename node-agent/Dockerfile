FROM public.ecr.aws/docker/library/python:3.12-slim

# aws cli(S3 同步快照用) + openssh-client(里程碑 B: SSH exec 到 guest)
RUN sed -i 's|deb.debian.org|cdn-aws.deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
    awscli iproute2 iptables openssh-client \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir boto3

# 里程碑 B: 内置 exec 私钥(POC;生产应用 K8s Secret 挂载而非打进镜像)
COPY sbx_exec_key /root/.ssh/id_ed25519
RUN chmod 600 /root/.ssh/id_ed25519

WORKDIR /app
COPY main.py /app/

CMD ["python3", "/app/main.py"]
