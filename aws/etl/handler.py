"""Lambda 入口：夜间仓库刷新（行情/新闻 -> DuckDB -> dbt -> CSV -> S3）。

与本地 `scripts/warehouse.py --full` 同一套代码，差别只有三点：

1. **头寸边界**：云侧强制 `--no-positions`，`raw.positions` 根本不产生；
   上传前再跑一次 `s3_publish.audit()` 做纵深防御（结构上不该有，真有就中止）。
2. **只读文件系统**：Lambda 只有 `/tmp` 可写，所以 DuckDB、dbt target/logs、
   CSV 导出全部重定向到 `/tmp`。
3. **配置来自 S3**：自选股清单从 `s3://<bucket>/config/watchlist.yaml` 拉取，
   换标的不用重新构建镜像。

环境变量：
    QUANTAI_S3_BUCKET   必填，数据湖桶名
    QUANTAI_S3_PREFIX   可选，默认 exports
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

import boto3

# 同一个执行环境会被复用；模块级只在冷启动执行一次，据此区分冷/热。
_COLD_START = True

TASK_ROOT = Path(os.environ.get("LAMBDA_TASK_ROOT", Path(__file__).parent))
sys.path.insert(0, str(TASK_ROOT))
sys.path.insert(0, str(TASK_ROOT / "scripts"))

TMP = Path("/tmp")
DB_PATH = TMP / "quantai.duckdb"
EXPORT_DIR = TMP / "exports"
WATCHLIST = TMP / "watchlist.yaml"

# 云侧自有的仓库文件。**不是**本地那个 data/warehouse/quantai.duckdb——本地库含
# raw.positions，永不上传；这一份由 Lambda 自己用 --no-positions 建，结构上无头寸。
# 需要它有状态是因为 fact_news 靠去重累积：每次从零建库只会留下当次抓到的几百条。
DB_KEY = "warehouse/quantai.duckdb"

_s3 = boto3.client("s3")


class _FeedFailureCounter:
    """数 news 模块报了多少条 ERROR。

    Phase 0 实测：Yahoo RSS 会间歇 400/502，代码优雅降级继续跑——本地能看到红字，
    云上定时任务却会**静默少数据**。所以把它变成一个可告警的指标，而不是日志里的一行。
    不改 quantai.data.news：挂一个 loguru sink 就够了。
    """

    def __init__(self) -> None:
        self.count = 0
        self._sink_id: int | None = None

    def __enter__(self) -> "_FeedFailureCounter":
        from loguru import logger

        def _sink(message) -> None:  # noqa: ANN001 - loguru record
            rec = message.record
            if rec["level"].name == "ERROR" and "news" in rec["name"]:
                self.count += 1

        self._sink_id = logger.add(_sink, level="ERROR")
        return self

    def __exit__(self, *exc) -> None:
        from loguru import logger

        if self._sink_id is not None:
            logger.remove(self._sink_id)


def _emit_metrics(function_name: str, metrics: dict[str, tuple[float, str]]) -> None:
    """CloudWatch Embedded Metric Format：指标嵌在本来就要写的日志行里。

    这样请求路径上**没有额外的 PutMetricData 调用**（零延迟开销），执行角色也
    **不需要 cloudwatch:PutMetricData 权限**——CloudWatch 从日志里自己抽。
    """
    payload = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": "QuantAI/ETL",
                "Dimensions": [["FunctionName"]],
                "Metrics": [{"Name": k, "Unit": u} for k, (_, u) in metrics.items()],
            }],
        },
        "FunctionName": function_name,
        **{k: v for k, (v, _) in metrics.items()},
    }
    print(json.dumps(payload))


def _bucket() -> str:
    b = os.environ.get("QUANTAI_S3_BUCKET")
    if not b:
        raise RuntimeError("QUANTAI_S3_BUCKET 未设置")
    return b


def _restore_db(bucket: str) -> bool:
    """把云侧仓库从 S3 拉回 /tmp。首次运行没有它，从零建库即可。"""
    try:
        _s3.download_file(bucket, DB_KEY, str(DB_PATH))
        return True
    except _s3.exceptions.ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
            print(f"[etl-lambda] {DB_KEY} 不存在，本次从零建库")
            return False
        raise


def _prepare_env(bucket: str) -> None:
    """把所有会写盘的路径挪到 /tmp，并从 S3 取自选股清单。"""
    _s3.download_file(bucket, "config/watchlist.yaml", str(WATCHLIST))
    os.environ.update({
        "QUANTAI_DB_PATH": str(DB_PATH),          # dbt profile 读这个
        "QUANTAI_EXPORT_DIR": str(EXPORT_DIR),    # warehouse.py 的导出目录
        "DBT_TARGET_PATH": str(TMP / "dbt_target"),
        "DBT_LOG_PATH": str(TMP / "dbt_logs"),
        "QUANTAI__portfolio__watchlist_file": str(WATCHLIST),
        "HOME": "/tmp",                           # 有些库会往 ~ 写缓存
    })


def _cleanup() -> None:
    """Lambda 会复用执行环境，/tmp 不清理会跨调用累积。"""
    for p in (DB_PATH, WATCHLIST):
        p.unlink(missing_ok=True)
    for d in (EXPORT_DIR, TMP / "dbt_target", TMP / "dbt_logs"):
        shutil.rmtree(d, ignore_errors=True)


def handler(event, context):  # noqa: ANN001 - Lambda 签名
    global _COLD_START
    cold = _COLD_START
    _COLD_START = False

    t0 = time.perf_counter()
    bucket = _bucket()
    prefix = os.environ.get("QUANTAI_S3_PREFIX", "exports")
    _cleanup()
    _prepare_env(bucket)
    warm = _restore_db(bucket)

    import s3_publish
    import warehouse as warehouse_cli

    with _FeedFailureCounter() as feeds:
        rc = warehouse_cli.main(["--full", "--no-positions", "--db", str(DB_PATH)])
    if rc != 0:
        raise RuntimeError(f"warehouse --full 失败 rc={rc}")
    etl_sec = time.perf_counter() - t0

    # 纵深防御：云侧 ETL 结构上不产生头寸，真有就是回归，中止而不是上传
    s3_publish.audit(EXPORT_DIR)

    uploaded = 0
    for f in sorted(EXPORT_DIR.glob("*.csv")):
        if f.name in s3_publish.POSITION_FILES:
            raise RuntimeError(f"边界违规：{f.name} 不该出现在云侧导出里")
        _s3.upload_file(str(f), bucket, f"{prefix}/{f.name}")
        uploaded += 1

    # 回传云侧仓库，下次运行接着累积（新闻靠去重增量，从零建库会丢历史）
    _s3.upload_file(str(DB_PATH), bucket, DB_KEY)

    total_sec = time.perf_counter() - t0
    db_mb = round(DB_PATH.stat().st_size / 1_048_576, 1)
    result = {
        "uploaded": uploaded,
        "warm_start": warm,
        "cold_start": cold,
        "db_mb": db_mb,
        "news_feed_failures": feeds.count,
        "etl_seconds": round(etl_sec, 2),
        "total_seconds": round(total_sec, 2),
        "positions_loaded": False,
    }
    _emit_metrics(context.function_name if context else "local", {
        "EtlDurationMs": (round(etl_sec * 1000), "Milliseconds"),
        "TotalDurationMs": (round(total_sec * 1000), "Milliseconds"),
        "ExportedFiles": (uploaded, "Count"),
        "NewsFeedFailures": (feeds.count, "Count"),
        "WarehouseMB": (db_mb, "Megabytes"),
        "ColdStart": (1 if cold else 0, "Count"),
    })
    print(f"[etl-lambda] {result}")
    _cleanup()
    return result
