"""DuckDB 与 Azure SQL 的 marts 逐列、逐格对账。

比对逻辑和 Snowflake 那边是同一份：直接用 infra/snowflake/reconcile.py 的 compare_table 和 norm
（列指纹加整行排序后逐格比较；整数精确，浮点相对误差 1e-9），这里只换数据来源。
范围是导出白名单（export_marts.MARTS_EXPORT_TABLES）；头寸表不在 Azure 上，另外核对 Azure 的 marts 里没有它。
DuckDB 要用这份快照出自的那个云侧文件（S3 的 warehouse/quantai.duckdb）。

用法：
    python infra/azure/reconcile.py --duckdb <云侧 quantai.duckdb>
退出码：0 全部一致，1 有差异。
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import azsql  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


EXPORT = _load("export_marts", HERE / "export_marts.py")
CORE = _load("snowflake_reconcile", HERE.parent / "snowflake" / "reconcile.py")


def duckdb_marts(path: Path) -> dict:
    import duckdb

    con = duckdb.connect(str(path), read_only=True)
    try:
        out = {}
        for n in EXPORT.MARTS_EXPORT_TABLES:
            cur = con.execute(f"SELECT * FROM marts.{n}")
            out[n] = ([c[0] for c in cur.description], cur.fetchall())
        return out
    finally:
        con.close()


def azure_marts() -> tuple[dict, list[str]]:
    con = azsql.connect()
    try:
        cur = con.cursor()
        cur.execute("SELECT name FROM sys.tables WHERE schema_id = SCHEMA_ID('marts') ORDER BY name")
        present = [r[0] for r in cur.fetchall()]
        out = {}
        for n in EXPORT.MARTS_EXPORT_TABLES:
            if n in present:
                cur.execute(f"SELECT * FROM marts.[{n}]")
                out[n] = ([c[0] for c in cur.description], [tuple(r) for r in cur.fetchall()])
        return out, present
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--duckdb", type=Path, required=True, help="这份快照出自的云侧 DuckDB 文件")
    args = p.parse_args(argv)

    duck = duckdb_marts(args.duckdb)
    try:
        azure, present = azure_marts()
    except Exception as exc:  # noqa: BLE001 - 报错信息打码后再抛
        raise SystemExit(f"[reconcile] Azure SQL 读取失败：{azsql.mask(exc)}") from None

    failed = 0
    leaked = sorted(EXPORT.POSITION_MARTS & set(present))
    if leaked:
        failed += 1
        print(f"[boundary] Azure 的 marts 里出现了头寸表 {leaked}")
    missing = sorted(set(duck) - set(azure))
    if missing:
        failed += 1
        print(f"[table set] Azure 缺少 {missing}")
    total_rows = total_cols = 0
    for name in sorted(set(duck) & set(azure)):
        r = CORE.compare_table(name, *duck[name], *azure[name])
        total_rows += r["rows"][0]
        total_cols += r["fingerprints_equal"][1]
        fp_eq, fp_n = r["fingerprints_equal"]
        status = "OK  " if not r["problems"] else "DIFF"
        print(f"[{status}] {name:<24} rows {r['rows'][0]:>7} / {r['rows'][1]:<7} "
              f"column fingerprints {fp_eq}/{fp_n}  max float rel diff {r['max_rel_diff']:.1e}")
        for prob in r["problems"]:
            print(f"        {prob}")
        failed += bool(r["problems"])
    print(f"[reconcile] {len(set(duck) & set(azure))} tables, {total_rows} rows, {total_cols} columns; "
          f"{'all equal' if not failed else f'{failed} with differences'}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
