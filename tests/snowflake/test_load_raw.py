"""Snowflake 装载脚本的离线测试：装什么、怎么装、什么样的快照拒绝装、报错怎么打码，不连 Snowflake。"""
from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parents[2] / "infra" / "snowflake"
sys.path.insert(0, str(_DIR))
_SPEC = importlib.util.spec_from_file_location("load_raw", _DIR / "load_raw.py")
load_raw = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(load_raw)
sfconn = sys.modules["sfconn"]

T0 = dt.datetime(2026, 9, 16, 14, 6, 6, tzinfo=dt.timezone.utc)


def test_loads_exactly_what_the_etl_exports_and_never_positions() -> None:
    tables = set(load_raw.tables_to_load())
    assert tables == set(load_raw.S3P.RAW_EXPORT_TABLES)
    assert not tables & {"positions", "portfolio_cash"}


def test_copy_is_an_exact_file_full_refresh() -> None:
    sql = load_raw.copy_sql("prices")
    assert "INTO raw.prices" in sql
    assert "FILES = ('prices.parquet')" in sql  # 只装这一个文件，不按前缀匹配
    assert "FORCE = TRUE" in sql  # 快照没变也要重装，否则 DELETE 之后表是空的
    assert "MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE" in sql
    assert "ON_ERROR = ABORT_STATEMENT" in sql


def test_snapshot_from_one_run_is_accepted() -> None:
    listing = [(f"{t}.parquet", T0 + dt.timedelta(seconds=i)) for i, t in enumerate(load_raw.tables_to_load())]
    assert load_raw.check_snapshot(listing) == T0 + dt.timedelta(seconds=len(listing) - 1)


def test_snapshot_with_a_missing_file_is_rejected() -> None:
    listing = [(f"{t}.parquet", T0) for t in load_raw.tables_to_load()][1:]
    with pytest.raises(SystemExit, match="缺少"):
        load_raw.check_snapshot(listing)


def test_snapshot_mixing_two_runs_is_rejected() -> None:
    listing = [(f"{t}.parquet", T0) for t in load_raw.tables_to_load()]
    listing[0] = (listing[0][0], T0 - dt.timedelta(days=1))  # 一个文件还是前一晚的
    with pytest.raises(SystemExit, match="同一次 ETL"):
        load_raw.check_snapshot(listing)


def test_mask_hides_bucket_urls_and_account_ids_it_was_not_told_about() -> None:
    msg = "Remote file 's3://some-bucket-name/raw/x.parquet' was not found; role arn:aws:iam::123456789012:role/r"
    out = sfconn.mask(msg)
    assert "some-bucket-name" not in out
    assert "123456789012" not in out
