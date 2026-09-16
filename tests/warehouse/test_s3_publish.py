"""S3 发布的边界测试：头寸数据永远不能进云端暂存目录。

这条边界是设计决策（PII 与头寸不跨边界），不是约定——所以用测试钉死：
往暂存目录里放头寸文件或没清零的持有标志，`audit()` 必须让发布失败。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import pytest

_SPEC = importlib.util.spec_from_file_location(
    "s3_publish", Path(__file__).resolve().parents[2] / "scripts" / "s3_publish.py"
)
s3_publish = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(s3_publish)


def _write(d: Path, name: str, df: pd.DataFrame) -> None:
    df.to_csv(d / name, index=False)


def test_stage_drops_position_file(tmp_path: Path) -> None:
    src, dest = tmp_path / "src", tmp_path / "dest"
    src.mkdir()
    _write(src, "fact_positions.csv", pd.DataFrame(
        {"symbol": ["SPCX"], "shares": [14.0], "unrealized_pnl": [-655.06]}))
    _write(src, "fact_prices.csv", pd.DataFrame({"symbol": ["SPY"], "close": [1.0]}))

    staged = s3_publish.stage(src, dest)

    assert "fact_positions.csv" not in staged
    assert not (dest / "fact_positions.csv").exists()
    assert (dest / "fact_prices.csv").exists()


def test_stage_blanks_held_flag(tmp_path: Path) -> None:
    src, dest = tmp_path / "src", tmp_path / "dest"
    src.mkdir()
    _write(src, "dim_symbol.csv", pd.DataFrame(
        {"symbol": ["SPCX", "SPY"], "is_currently_held": [True, False]}))

    s3_publish.stage(src, dest)

    out = pd.read_csv(dest / "dim_symbol.csv")
    assert not out["is_currently_held"].any(), "云端副本不得暴露持有哪些标的"


def test_audit_rejects_leaked_position_file(tmp_path: Path) -> None:
    dest = tmp_path / "dest"
    dest.mkdir()
    _write(dest, "fact_positions.csv", pd.DataFrame({"shares": [14.0]}))

    with pytest.raises(SystemExit, match="头寸文件"):
        s3_publish.audit(dest)


def test_audit_rejects_unblanked_held_flag(tmp_path: Path) -> None:
    dest = tmp_path / "dest"
    dest.mkdir()
    _write(dest, "dim_symbol.csv", pd.DataFrame(
        {"symbol": ["SPCX"], "is_currently_held": [True]}))

    with pytest.raises(SystemExit, match="is_currently_held"):
        s3_publish.audit(dest)


def test_audit_passes_on_clean_stage(tmp_path: Path) -> None:
    dest = tmp_path / "dest"
    dest.mkdir()
    _write(dest, "dim_symbol.csv", pd.DataFrame(
        {"symbol": ["SPCX"], "is_currently_held": [False]}))
    _write(dest, "fact_prices.csv", pd.DataFrame({"symbol": ["SPY"], "close": [1.0]}))

    s3_publish.audit(dest)  # 不抛异常即通过


# ---- raw 层 Parquet 快照（给 Snowflake）：头寸表同样不许上云 ----

def _raw_db(path: Path) -> Path:
    """建齐全部 raw 表，并往头寸两张表和 prices 各塞一行。"""
    import duckdb

    from quantai.warehouse.etl import init_raw_tables

    con = duckdb.connect(str(path))
    con.execute("CREATE SCHEMA IF NOT EXISTS raw")
    init_raw_tables(con)
    con.execute("INSERT INTO raw.positions (as_of, symbol, shares, cost_basis, open_date) "
                "VALUES ('2026-01-02', 'SPCX', 14, 40, '2025-12-01')")
    con.execute("INSERT INTO raw.portfolio_cash (as_of, cash) VALUES ('2026-01-02', 1000)")
    con.execute("INSERT INTO raw.prices (symbol, date, close) VALUES ('SPY', '2026-01-02', 1.0)")
    con.close()
    return path


def test_raw_whitelist_classifies_every_raw_table() -> None:
    from quantai.warehouse.etl import _DDL

    assert not s3_publish.POSITION_TABLES & set(s3_publish.RAW_EXPORT_TABLES)
    # 新加的 raw 表必须明确决定上不上云：要么进白名单，要么是头寸表。
    assert set(s3_publish.RAW_EXPORT_TABLES) | s3_publish.POSITION_TABLES == set(_DDL)


def test_export_raw_never_writes_position_tables(tmp_path: Path) -> None:
    import duckdb

    db = _raw_db(tmp_path / "w.duckdb")
    dest = tmp_path / "raw"

    written = s3_publish.export_raw(db, dest)

    names = {p.stem for p in dest.glob("*.parquet")}
    assert names == set(s3_publish.RAW_EXPORT_TABLES)
    assert {p.stem for p in written} == names
    assert not names & {"positions", "portfolio_cash"}, "头寸表有数据也不许导出"
    rows = duckdb.sql(f"SELECT count(*) FROM read_parquet('{(dest / 'prices.parquet').as_posix()}')").fetchone()[0]
    assert rows == 1
    s3_publish.audit_raw(dest)  # 导出结果本身必须过体检


def test_audit_raw_rejects_position_table(tmp_path: Path) -> None:
    dest = tmp_path / "raw"
    dest.mkdir()
    (dest / "positions.parquet").write_bytes(b"not really parquet")

    with pytest.raises(SystemExit, match="头寸表"):
        s3_publish.audit_raw(dest)


def test_audit_raw_rejects_unlisted_table(tmp_path: Path) -> None:
    dest = tmp_path / "raw"
    dest.mkdir()
    (dest / "something_new.parquet").write_bytes(b"not really parquet")

    with pytest.raises(SystemExit, match="白名单外"):
        s3_publish.audit_raw(dest)
