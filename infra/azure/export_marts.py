"""把云侧 DuckDB 的 marts 导成 Parquet 快照，交给 Azure 一侧装载（GitHub Actions 和本机手动补跑用同一个脚本）。

输入：S3 数据湖里 ETL 写的 warehouse/quantai.duckdb（云侧，不含持仓）。
输出：<out>/<表>.parquet（白名单 9 张）和 <out>/_manifest.csv（table_name,row_count）。
Data Factory 的存储事件触发器看到 _manifest.csv 才开始装载，所以上传时它必须最后传。

头寸边界，与 scripts/s3_publish.py 同一套规则：
1. fact_positions（s3_publish.POSITION_FILES）不在白名单里，永不导出；
2. dim_symbol.is_currently_held 会暴露持有哪些标的。云侧 DuckDB 里它应该全是 false，只要有一行 true 就中止：
   说明拿错了库，比如本机那份带持仓的。
导出后再体检输出目录：只能有白名单里的表，一张不多、一张不少。

时间戳写成 INT96：Data Factory 的复制活动读 Parquet 时，TIMESTAMP_MICROS 按 Int64 读、INT96 才按 DateTime 读
（Learn「Parquet format」的类型对照表）。DuckDB 自己的 COPY 写的是微秒 INT64，第一次装载 fact_news 就因此失败。
所以经 Arrow 用 pyarrow 写，时间戳列用 INT96，微秒精度不丢；其余类型和 DuckDB 直接写出的一样。

用法：
    python infra/azure/export_marts.py --duckdb quantai.duckdb --out out/marts/<快照>
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def _s3_publish():
    spec = importlib.util.spec_from_file_location("s3_publish", ROOT / "scripts" / "s3_publish.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


S3P = _s3_publish()

POSITION_MARTS = {name.removesuffix(".csv") for name in S3P.POSITION_FILES}
HELD_TABLE, HELD_COLUMN = S3P.HELD_FLAG[0].removesuffix(".csv"), S3P.HELD_FLAG[1]
MANIFEST = "_manifest.csv"
# 显式白名单：新加的 marts 模型不会自动去 Azure（测试要求每个 marts 模型都被明确归类）。
MARTS_EXPORT_TABLES = (
    "dim_date",
    "dim_symbol",
    "fact_backtest_equity",
    "fact_backtest_results",
    "fact_event_odds",
    "fact_news",
    "fact_prices",
    "fact_signals",
    "fact_trades",
)


def check_whitelist() -> None:
    leaked = sorted(POSITION_MARTS.intersection(MARTS_EXPORT_TABLES))
    if leaked:
        raise SystemExit(f"ABORT: marts 导出白名单含头寸表 {leaked}")


def export_marts(db_path: Path, dest: Path) -> dict[str, int]:
    """白名单里的 marts 表 -> dest/<表>.parquet，返回每张表的行数（空表也写，带表结构）。"""
    import duckdb
    import pyarrow.parquet as pq

    check_whitelist()
    dest.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        held = con.execute(f"SELECT count(*) FROM marts.{HELD_TABLE} WHERE {HELD_COLUMN}").fetchone()[0]
        if held:
            raise SystemExit(f"ABORT: marts.{HELD_TABLE} 有 {held} 行 {HELD_COLUMN} = true，这不是云侧（不含持仓）的库")
        for table in MARTS_EXPORT_TABLES:
            data = con.execute(f"SELECT * FROM marts.{table}").to_arrow_table()
            pq.write_table(data, dest / f"{table}.parquet", compression="snappy", use_deprecated_int96_timestamps=True)
            counts[table] = data.num_rows
    finally:
        con.close()
    audit(dest)
    write_manifest(dest, counts)
    return counts


def audit(dest: Path) -> None:
    """上传前体检：输出目录里的 Parquet 必须正好是白名单，出现头寸表或白名单外的表就中止。"""
    names = {p.stem for p in dest.glob("*.parquet")}
    leaked = sorted(names & POSITION_MARTS)
    if leaked:
        raise SystemExit(f"ABORT: 导出目录含头寸表 {leaked}")
    unknown = sorted(names - set(MARTS_EXPORT_TABLES))
    missing = sorted(set(MARTS_EXPORT_TABLES) - names)
    if unknown or missing:
        raise SystemExit(f"ABORT: 导出目录与白名单不符：多出 {unknown}，缺少 {missing}")
    print(f"[audit] marts 体检通过：{len(names)} 张白名单表，无头寸表")


def write_manifest(dest: Path, counts: dict[str, int]) -> Path:
    path = dest / MANIFEST
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["table_name", "row_count"])
        for table in MARTS_EXPORT_TABLES:
            w.writerow([table, counts[table]])
    return path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--duckdb", type=Path, required=True, help="云侧 DuckDB（S3 的 warehouse/quantai.duckdb）")
    p.add_argument("--out", type=Path, required=True, help="输出目录，建议 out/marts/<快照>")
    args = p.parse_args(argv)
    counts = export_marts(args.duckdb, args.out)
    for table, n in counts.items():
        print(f"[export] {table:<22} {n:>7} rows")
    print(f"[export] 完成：{len(counts)} 张表，共 {sum(counts.values())} 行；清单 {MANIFEST} 最后上传")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
