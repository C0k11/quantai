# QuantAI 计算 API 的 Lambda 容器镜像。
# 与 ETL 镜像刻意分开：这里不需要 dbt/duckdb/yfinance/pyarrow，镜像小、冷启动快。
FROM public.ecr.aws/lambda/python:3.11

COPY aws/api/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --only-binary=:all: -r /tmp/requirements.txt \
 && rm -rf /root/.cache

COPY quantai/ ${LAMBDA_TASK_ROOT}/quantai/
COPY configs/ ${LAMBDA_TASK_ROOT}/configs/
COPY aws/api/handler.py ${LAMBDA_TASK_ROOT}/handler.py

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

CMD ["handler.handler"]
