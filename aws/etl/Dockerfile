# QuantAI 夜间 ETL 的 Lambda 容器镜像。
#
# 为什么是容器不是 zip：pandas + pyarrow + scipy + duckdb + dbt-duckdb 解压后
# 远超 zip 层的 250MB 上限。代价是放弃 SnapStart（只支持 zip 打包）。
FROM public.ecr.aws/lambda/python:3.11

# --only-binary=:all: 是硬门槛：基础镜像里没有编译器，任何包一旦回落到源码构建
# 都会静默失败在 meson/gcc 上。宁可让 pip 当场报"没有 wheel"，也不要装编译器。
COPY aws/etl/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --only-binary=:all: -r /tmp/requirements.txt \
 && rm -rf /root/.cache

# 业务代码。scripts/warehouse.py 用 __file__.parent.parent 定位仓库根，
# 所以这几个目录必须保持相对布局。
COPY quantai/   ${LAMBDA_TASK_ROOT}/quantai/
COPY scripts/   ${LAMBDA_TASK_ROOT}/scripts/
COPY warehouse/ ${LAMBDA_TASK_ROOT}/warehouse/
COPY configs/   ${LAMBDA_TASK_ROOT}/configs/
COPY aws/etl/handler.py ${LAMBDA_TASK_ROOT}/handler.py

# Lambda 没有 POSIX 共享内存，multiprocessing 的信号量锁会 PermissionError。
# 放在 site-packages 里，解释器启动即生效——dbt 是子进程，从 handler 里 patch 不到。
COPY aws/etl/sitecustomize.py /var/lang/lib/python3.11/site-packages/sitecustomize.py

# 只读文件系统：所有写盘路径在 handler 里重定向到 /tmp
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

CMD ["handler.handler"]
