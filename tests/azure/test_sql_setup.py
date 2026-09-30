"""Azure SQL 建表脚本的离线测试：类型怎么对应、表怎么建、缺表和陌生类型怎么拒绝。不连 Azure，也不需要 pyodbc。"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parents[2] / "infra" / "azure"
sys.path.insert(0, str(_DIR))
_SPEC = importlib.util.spec_from_file_location("sql_setup", _DIR / "sql_setup.py")
sql_setup = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sql_setup)


def test_every_marts_type_has_a_fixed_tsql_type() -> None:
    assert sql_setup.tsql_type("BIGINT") == "bigint"
    assert sql_setup.tsql_type("integer") == "int"
    assert sql_setup.tsql_type("DOUBLE") == "float"  # float(53)，和 DuckDB 的 DOUBLE 同为 8 字节，对账才能逐位相等
    assert sql_setup.tsql_type("BOOLEAN") == "bit"
    assert sql_setup.tsql_type("DATE") == "date"
    assert sql_setup.tsql_type("TIMESTAMP") == "datetime2(6)"  # 微秒，和 DuckDB 一样
    assert sql_setup.tsql_type("VARCHAR") == "nvarchar(max)"


def test_unknown_type_stops_instead_of_guessing() -> None:
    with pytest.raises(SystemExit, match="HUGEINT"):
        sql_setup.tsql_type("HUGEINT")


def test_create_table_sql_keeps_column_order_and_nullability() -> None:
    sql = sql_setup.create_table_sql("stage", "fact_prices", [("symbol", "nvarchar(max)"), ("close", "float")])
    assert sql.startswith("CREATE TABLE stage.[fact_prices] (")
    assert sql.index("[symbol] nvarchar(max) NULL") < sql.index("[close] float NULL")


def test_duckdb_columns_reads_whitelist_in_order_and_rejects_missing(tmp_path: Path) -> None:
    import duckdb

    db = tmp_path / "w.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE SCHEMA marts")
    for table in sql_setup.EXPORT.MARTS_EXPORT_TABLES:
        con.execute(f"CREATE TABLE marts.{table} (b BIGINT, a VARCHAR, t TIMESTAMP)")
    con.execute("CREATE TABLE marts.fact_positions (shares DOUBLE)")
    con.close()

    cols = sql_setup.duckdb_columns(db)
    assert set(cols) == set(sql_setup.EXPORT.MARTS_EXPORT_TABLES)  # 头寸表不在内
    assert cols["fact_news"] == [("b", "bigint"), ("a", "nvarchar(max)"), ("t", "datetime2(6)")]

    con = duckdb.connect(str(db))
    con.execute("DROP TABLE marts.fact_news")
    con.close()
    with pytest.raises(SystemExit, match="fact_news"):
        sql_setup.duckdb_columns(db)


def test_publish_procedure_checks_the_manifest_before_touching_marts() -> None:
    sql = sql_setup.PUBLISH_MARTS
    assert "EXECUTE AS OWNER" in sql
    # 先核对清单和行数，再开事务替换 marts
    assert sql.index("THROW 50002") < sql.index("THROW 50003") < sql.index("BEGIN TRANSACTION")
    assert "SET XACT_ABORT ON" in sql
