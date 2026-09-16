"""DuckDB 与 Snowflake 的 marts 逐列对账。

同一份 raw 快照、同一个 dbt 项目、两个引擎：每张 marts 表必须表名一致、列一致、行数一致、取值一致。比两层：
1. 列指纹：每列取值排序后做摘要（数字先取 9 位有效数字），一眼看出哪一列不一致；
2. 逐单元格：两边整行排序对齐后逐格比较。整数（含整值 Decimal）必须完全相等；
   浮点用相对误差 1e-9，另有 1e-12 的绝对误差给接近 0 的值（两个引擎聚合顺序不同，末位可能不同）。
判定以逐单元格为准；列指纹只因数字取整边界不同而不一致时单独标出，不算差异。

用法：
    python infra/snowflake/reconcile.py --duckdb <path/to/quantai.duckdb> --env-file .env.snowflake.local
退出码：0 全部一致，1 有差异。
"""
from __future__ import annotations

import argparse
import datetime as dt
import decimal
import hashlib
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import sfconn  # noqa: E402

REL_TOL = 1e-9
ABS_TOL = 1e-12


def norm(v):
    """两个驱动返回的 Python 值统一成可比较的形式。

    整数和整值 Decimal 保持 int（精确比较），其余数字变 float；日期、时间、布尔、字符串带类型前缀。
    """
    if v is None:
        return None
    if isinstance(v, bool):
        return f"b:{int(v)}"
    if isinstance(v, int):
        return v
    if isinstance(v, decimal.Decimal):
        if v.is_finite() and v == v.to_integral_value():
            return int(v)
        f = float(v)
        return None if math.isnan(f) else f
    if isinstance(v, float):
        return None if math.isnan(v) else v
    if isinstance(v, dt.datetime):
        return "t:" + v.replace(tzinfo=None).isoformat(sep=" ", timespec="microseconds")
    if isinstance(v, dt.date):
        return "d:" + v.isoformat()
    return "s:" + str(v)


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _sig(f: float) -> float:
    return float(f"{f:.9g}") + 0.0  # + 0.0 把 -0.0 归成 0.0


def _key(row: tuple) -> tuple:
    # 整数按精确值排序，浮点按 9 位有效数字排序（末位差异不应改变行序）。
    return tuple(
        (0, 0) if v is None else (1, v) if isinstance(v, int) else (1, _sig(v)) if isinstance(v, float) else (2, v)
        for v in row
    )


def fingerprint(values: list) -> str:
    items = sorted("~" if v is None else repr(_sig(float(v))) if _is_num(v) else v for v in values)
    return hashlib.md5("\x1f".join(items).encode("utf-8")).hexdigest()[:12]


def cells_equal(a, b) -> tuple[bool, float]:
    """返回 (是否一致, 一致但不完全相等时的相对差)。整数对整数精确比较，涉及浮点才用容差。"""
    if isinstance(a, int) and isinstance(b, int):
        return a == b, 0.0
    if _is_num(a) and _is_num(b):
        fa, fb = float(a), float(b)
        if not math.isclose(fa, fb, rel_tol=REL_TOL, abs_tol=ABS_TOL):
            return False, 0.0
        rel = abs(fa - fb) / max(abs(fa), abs(fb)) if fa != fb and fa != 0.0 and fb != 0.0 else 0.0
        return True, rel
    return a == b, 0.0


def compare_table(name: str, duck_cols, duck_rows, snow_cols, snow_rows) -> dict:
    res = {"table": name, "rows": (len(duck_rows), len(snow_rows)), "problems": [],
           "fingerprints_equal": (0, 0), "columns_mismatched": [], "max_rel_diff": 0.0}
    dc = [c.lower() for c in duck_cols]
    sc = [c.lower() for c in snow_cols]
    if set(dc) != set(sc):
        res["problems"].append(
            f"columns differ: only duckdb {sorted(set(dc) - set(sc))}, only snowflake {sorted(set(sc) - set(dc))}")
        return res
    order = [sc.index(c) for c in dc]
    d = [tuple(norm(v) for v in r) for r in duck_rows]
    s = [tuple(norm(r[i]) for i in order) for r in snow_rows]
    if len(d) != len(s):
        res["problems"].append(f"row count {len(d)} vs {len(s)}")
    equal_fp = sum(fingerprint([r[j] for r in d]) == fingerprint([r[j] for r in s]) for j in range(len(dc)))
    res["fingerprints_equal"] = (equal_fp, len(dc))
    d.sort(key=_key)
    s.sort(key=_key)
    bad_cols: set[str] = set()
    bad_cells = 0
    for rd, rs in zip(d, s):
        for j, (a, b) in enumerate(zip(rd, rs)):
            ok, rel = cells_equal(a, b)
            if not ok:
                bad_cells += 1
                bad_cols.add(dc[j])
            else:
                res["max_rel_diff"] = max(res["max_rel_diff"], rel)
    res["columns_mismatched"] = sorted(bad_cols)
    if bad_cells:
        res["problems"].append(f"{bad_cells} cells differ in {sorted(bad_cols)}")
    return res


def duckdb_marts(path: Path) -> dict:
    import duckdb

    con = duckdb.connect(str(path), read_only=True)
    try:
        names = [r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'marts' ORDER BY 1").fetchall()]
        out = {}
        for n in names:
            cur = con.execute(f"SELECT * FROM marts.{n}")
            out[n.lower()] = ([c[0] for c in cur.description], cur.fetchall())
        return out
    finally:
        con.close()


def snowflake_marts() -> dict:
    con = sfconn.connect("quantai-reconcile")
    try:
        cur = con.cursor()
        cur.execute("SHOW TABLES IN SCHEMA marts")
        names = sorted(r[1] for r in cur.fetchall())
        out = {}
        for n in names:
            cur.execute(f"SELECT * FROM marts.{n}")
            out[n.lower()] = ([c[0] for c in cur.description], cur.fetchall())
        return out
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--duckdb", type=Path, required=True, help="与 raw/ 快照同一次 ETL 产出的 DuckDB 文件")
    p.add_argument("--env-file", type=Path, help="KEY=VALUE 文件，如 .env.snowflake.local")
    args = p.parse_args(argv)
    if args.env_file:
        sfconn.read_env_file(args.env_file)

    duck = duckdb_marts(args.duckdb)
    try:
        snow = snowflake_marts()
    except Exception as exc:  # noqa: BLE001 - 报错信息打码后再抛
        raise SystemExit(f"[reconcile] Snowflake 读取失败：{sfconn.mask(exc)}") from None

    failed = 0
    if set(duck) != set(snow):
        failed += 1
        print(f"[table set] only duckdb {sorted(set(duck) - set(snow))}, only snowflake {sorted(set(snow) - set(duck))}")
    total_rows = total_cols = 0
    for name in sorted(set(duck) & set(snow)):
        r = compare_table(name, *duck[name], *snow[name])
        total_rows += r["rows"][0]
        total_cols += r["fingerprints_equal"][1]
        fp_eq, fp_n = r["fingerprints_equal"]
        status = "OK  " if not r["problems"] else "DIFF"
        print(f"[{status}] {name:<24} rows {r['rows'][0]:>7} / {r['rows'][1]:<7} "
              f"column fingerprints {fp_eq}/{fp_n}  max float rel diff {r['max_rel_diff']:.1e}")
        for prob in r["problems"]:
            print(f"        {prob}")
        failed += bool(r["problems"])
    print(f"[reconcile] {len(set(duck) & set(snow))} tables, {total_rows} rows, {total_cols} columns; "
          f"{'all equal' if not failed else f'{failed} with differences'}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
