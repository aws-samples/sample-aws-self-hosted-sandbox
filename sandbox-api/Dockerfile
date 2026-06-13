FROM python:3.12-slim

WORKDIR /app

# 依赖
RUN pip install --no-cache-dir boto3 kubernetes

# 复制源码(以 sandbox_api 包名暴露)
COPY . /app/sandbox_api/

# 入口
CMD ["python3", "-m", "sandbox_api.app"]
