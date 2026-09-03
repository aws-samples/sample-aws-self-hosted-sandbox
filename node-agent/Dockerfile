FROM public.ecr.aws/docker/library/python:3.12-slim

# aws cli(S3 同步快照用) + openssh-client(里程碑 B: SSH exec 到 guest)
RUN sed -i 's|deb.debian.org|cdn-aws.deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
    awscli iproute2 iptables openssh-client util-linux \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir boto3 prometheus-client \
    opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp-proto-http

WORKDIR /app
COPY main.py observability.py /app/

CMD ["python3", "/app/main.py"]
