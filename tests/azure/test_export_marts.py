"""Azure 导出的边界测试：头寸永远不能进 Azure 的落地区，时间戳要写成 Data Factory 认得的 INT96。不连 Azure。"""
from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_DIR = _ROOT / "infra" / "azure"
sys.path.insert(0, str(_DIR))
_SPEC = importlib.util.spec_from_file_location("export_marts", _DIR / "export_marts.py")
export_marts = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(export_marts)

PUBLISHED = dt.datetime(2026, 9, 29, 12, 34, 56, 123456)


def _marts_db(path: Path, held: bool = False) -> Path:
    """白名单里的表都建上（大多是空表），dim_symbol 和 fact_news 各一行，头寸表也塞一行。"""
    import duckdb

    con = duckdb.connect(str(path))
    con.execute("CREATE SCHEMA marts")
    for table in export_marts.MARTS_EXPORT_TABLES:
        if table == "dim_symbol":
            con.execute("CREATE TABLE marts.dim_symbol (symbol VARCHAR, is_currently_held BOOLEAN)")
            con.execute("INSERT INTO marts.dim_symbol VALUES ('SPY', ?)", [held])
        elif table == "fact_news":
            con.execute("CREATE TABLE marts.fact_news (link VARCHAR, published TIMESTAMP)")
            con.execute("INSERT INTO marts.fact_news VALUES ('https://example.com/a', ?)", [PUBLISHED])
        else:
            con.execute(f"CREATE TABLE marts.{table} (id INTEGER)")
    con.execute("CREATE TABLE marts.fact_positions (symbol VARCHAR, shares DOUBLE)")
    con.execute("INSERT INTO marts.fact_positions VALUES ('SPCX', 14)")
    con.close()
    return path


def test_whitelist_classifies_every_marts_model() -> None:
    models = {p.stem for p in (_ROOT / "warehouse" / "models" / "marts").glob("*.sql")}
    whitelist = set(export_marts.MARTS_EXPORT_TABLES)
    assert not export_marts.POSITION_MARTS & whitelist
    # 新加的 marts 模型必须明确决定去不去 Azure：要么进白名单，要么是头寸表。
    assert whitelist | export_marts.POSITION_MARTS == models


def test_position_marts_come_from_the_s3_boundary() -> None:
    assert export_marts.POSITION_MARTS == {"fact_positions"}
    assert (export_marts.HELD_TABLE, export_marts.HELD_COLUMN) == ("dim_symbol", "is_currently_held")


def test_export_writes_whitelist_and_manifest_never_positions(tmp_path: Path) -> None:
    db = _marts_db(tmp_path / "w.duckdb")
    dest = tmp_path / "out"

    counts = export_marts.export_marts(db, dest)

    names = {p.stem for p in dest.glob("*.parquet")}
    assert names == set(export_marts.MARTS_EXPORT_TABLES)
    assert "fact_positions" not in names, "头寸表有数据也不许导出"
    lines = (dest / export_marts.MANIFEST).read_text(encoding="utf-8").splitlines()
    assert lines[0] == "table_name,row_count"
    assert dict(line.split(",") for line in lines[1:]) == {t: str(n) for t, n in counts.items()}
    assert counts["dim_symbol"] == 1 and counts["fact_news"] == 1 and counts["dim_date"] == 0


def test_timestamps_are_int96_and_keep_microseconds(tmp_path: Path) -> None:
    import duckdb
    import pyarrow.parquet as pq

    dest = tmp_path / "out"
    export_marts.export_marts(_marts_db(tmp_path / "w.duckdb"), dest)

    news = dest / "fact_news.parquet"
    schema = pq.ParquetFile(news).schema
    physical = {schema.column(i).name: schema.column(i).physical_type for i in range(len(schema))}
    assert physical["published"] == "INT96"  # Data Factory 把 TIMESTAMP_MICROS 读成 Int64，INT96 才是 DateTime
    back = duckdb.sql(f"SELECT published FROM read_parquet('{news.as_posix()}')").fetchone()[0]
    assert back == PUBLISHED


def test_export_refuses_a_database_that_shows_holdings(tmp_path: Path) -> None:
    db = _marts_db(tmp_path / "w.duckdb", held=True)

    with pytest.raises(SystemExit, match="is_currently_held"):
        export_marts.export_marts(db, tmp_path / "out")
    assert not list((tmp_path / "out").glob("*.parquet")), "中止时一个文件都不该写"


def test_audit_rejects_position_table(tmp_path: Path) -> None:
    dest = tmp_path / "out"
    dest.mkdir()
    for table in export_marts.MARTS_EXPORT_TABLES:
        (dest / f"{table}.parquet").write_bytes(b"x")
    (dest / "fact_positions.parquet").write_bytes(b"x")

    with pytest.raises(SystemExit, match="头寸表"):
        export_marts.audit(dest)


def test_audit_rejects_unlisted_and_missing_tables(tmp_path: Path) -> None:
    dest = tmp_path / "out"
    dest.mkdir()
    for table in export_marts.MARTS_EXPORT_TABLES[1:]:
        (dest / f"{table}.parquet").write_bytes(b"x")
    (dest / "something_new.parquet").write_bytes(b"x")

    with pytest.raises(SystemExit, match="白名单不符"):
        export_marts.audit(dest)
