FROM python:3.12-slim

# aws cli(S3 同步快照用)
RUN apt-get update && apt-get install -y --no-install-recommends \
    awscli iproute2 iptables \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir boto3

WORKDIR /app
COPY main.py /app/

CMD ["python3", "/app/main.py"]
