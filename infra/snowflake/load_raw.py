"""把 S3 数据湖 raw/ 下的 Parquet 快照装进 Snowflake 的 RAW schema（Snowflake 这一侧的 EL，T 是同一个 dbt 项目）。

流程：
1. 建 RAW schema 和全部 raw 表。DDL 与 DuckDB 共用 quantai/warehouse/etl.py 的同一份（DuckDB 的类型名 Snowflake 都认）。
2. 建 Parquet 文件格式（USE_LOGICAL_TYPE，DATE 和 TIMESTAMP 才按逻辑类型读）和指向 raw/ 的外部 stage
   （storage integration QUANTAI_S3_RAW，只读，由 infra/terraform/snowflake.tf 的 IAM 角色授权）。
3. 先 LIST raw/：白名单里每张表都要有文件，且全部出自同一次 ETL（写入时间相差不超过 10 分钟；
   ETL 的函数超时是 300 秒，一次运行写不出更大的跨度）。缺文件或跨度过大就拒绝装载。
4. 白名单里的 9 张表在**同一个事务**里 DELETE 再 COPY INTO：任何一张失败就整体回滚，RAW 保持上一份快照，
   不会一半新一半旧。FORCE = TRUE：Snowflake 记得装过的文件，快照没变时会跳过，DELETE 之后表就空了。
5. 头寸两张表只建空表，从不装载；装完核对它们在 Snowflake 上仍是 0 行。

用法：
    python infra/snowflake/load_raw.py --env-file .env.snowflake.local
需要桶名（建 stage 与报错打码都要用）：QUANTAI_S3_BUCKET 环境变量，或本机 .aws-bucket-name.local；找不到就停。
"""
from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

import sfconn  # noqa: E402

INTEGRATION = "QUANTAI_S3_RAW"
STAGE = "raw.lake"
FILE_FORMAT = "raw.parquet_ff"
MAX_SNAPSHOT_SPREAD = dt.timedelta(minutes=10)


def _s3_publish():
    spec = importlib.util.spec_from_file_location("s3_publish", ROOT / "scripts" / "s3_publish.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


S3P = _s3_publish()


def tables_to_load() -> tuple[str, ...]:
    """与 ETL 导出同一份白名单：导出什么就装什么，头寸表两边都不在内。"""
    return S3P.RAW_EXPORT_TABLES


def copy_sql(table: str) -> str:
    return (
        f"COPY INTO raw.{table} FROM @{STAGE} FILES = ('{table}.parquet') "
        f"FILE_FORMAT = (FORMAT_NAME = '{FILE_FORMAT}') "
        "MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE FORCE = TRUE ON_ERROR = ABORT_STATEMENT"
    )


def check_snapshot(listing: list[tuple[str, dt.datetime]], tables=None) -> dt.datetime:
    """listing 是 (文件名, 写入时间)。每张表都要有文件，且写入时间跨度不超过 MAX_SNAPSHOT_SPREAD。返回快照时间。"""
    tables = tuple(tables if tables is not None else tables_to_load())
    stamps = {name: ts for name, ts in listing}
    missing = [t for t in tables if f"{t}.parquet" not in stamps]
    if missing:
        raise SystemExit(f"ABORT: raw/ 缺少 {missing}，不是完整快照")
    times = [stamps[f"{t}.parquet"] for t in tables]
    spread = max(times) - min(times)
    if spread > MAX_SNAPSHOT_SPREAD:
        raise SystemExit(f"ABORT: raw/ 的文件写入时间相差 {spread}，不是同一次 ETL 的快照")
    return max(times)


def _listing(cur) -> list[tuple[str, dt.datetime]]:
    cur.execute(f"LIST @{STAGE}")
    # 行是 (name, size, md5, last_modified)；name 是完整 URL，只取文件名，不打印。
    return [(r[0].rsplit("/", 1)[-1], email.utils.parsedate_to_datetime(r[3])) for r in cur.fetchall()]


def load(cur, bucket: str) -> dict[str, int]:
    from quantai.warehouse.etl import init_raw_tables

    cur.execute("CREATE SCHEMA IF NOT EXISTS raw")
    init_raw_tables(cur)  # 同一份 DDL；游标和 DuckDB 连接一样有 execute()
    cur.execute(f"CREATE FILE FORMAT IF NOT EXISTS {FILE_FORMAT} TYPE = PARQUET USE_LOGICAL_TYPE = TRUE")
    cur.execute("SHOW STAGES LIKE 'LAKE' IN SCHEMA raw")
    if not cur.fetchall():
        cur.execute(
            f"CREATE STAGE {STAGE} URL = 's3://{bucket}/{S3P.RAW_PREFIX}/' "
            f"STORAGE_INTEGRATION = {INTEGRATION} FILE_FORMAT = {FILE_FORMAT}"
        )
    snapshot = check_snapshot(_listing(cur))
    print(f"[load] 快照写于 {snapshot:%Y-%m-%d %H:%M:%S}Z，{len(tables_to_load())} 个文件齐全")

    cur.execute("BEGIN")
    try:
        for table in tables_to_load():
            cur.execute(f"DELETE FROM raw.{table}")
            cur.execute(copy_sql(table))
        cur.execute("COMMIT")
    except Exception:
        cur.execute("ROLLBACK")
        raise

    counts: dict[str, int] = {}
    for table in tables_to_load():
        cur.execute(f"SELECT COUNT(*) FROM raw.{table}")
        counts[table] = cur.fetchone()[0]
        print(f"[load] raw.{table:<16} {counts[table]:>8} rows")
    for table in sorted(S3P.POSITION_TABLES):
        cur.execute(f"SELECT COUNT(*) FROM raw.{table}")
        n = cur.fetchone()[0]
        if n:
            raise SystemExit(f"ABORT: raw.{table} 在 Snowflake 上有 {n} 行，头寸不该出现在云上")
        print(f"[load] raw.{table:<16} {n:>8} rows（头寸表，只建空表）")
    return counts


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env-file", type=Path, help="KEY=VALUE 文件，如 .env.snowflake.local")
    args = p.parse_args(argv)
    if args.env_file:
        sfconn.read_env_file(args.env_file)
    bucket = S3P.resolve_bucket()  # 找不到就停：报错打码要靠它
    try:
        con = sfconn.connect("quantai-load-raw")
        try:
            counts = load(con.cursor(), bucket)
        finally:
            con.close()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - 报错信息打码后再抛
        raise SystemExit(f"[load] 失败：{sfconn.mask(exc, bucket)}") from None
    print(f"[load] 完成：{len(counts)} 张表，共 {sum(counts.values())} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
