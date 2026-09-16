"""DuckDB 与 Snowflake 对账逻辑的离线测试：不连 Snowflake，只喂两个驱动会返回的 Python 值。"""
from __future__ import annotations

import datetime as dt
import decimal
import importlib.util
import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parents[2] / "infra" / "snowflake"
sys.path.insert(0, str(_DIR))
_SPEC = importlib.util.spec_from_file_location("reconcile", _DIR / "reconcile.py")
reconcile = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(reconcile)

COLS = ["symbol", "date", "close", "is_trading_day"]


def _duck():
    return [("SPY", dt.date(2026, 1, 2), 0.1 + 0.2, True), ("QQQ", dt.date(2026, 1, 2), 1.0, False)]


def test_equal_despite_float_noise_row_order_and_driver_types() -> None:
    # Snowflake 大写列名、不同行序、NUMBER 回来是 Decimal、浮点末位不同：都不算差异。
    snow = [("QQQ", dt.date(2026, 1, 2), decimal.Decimal("1.0"), False), ("SPY", dt.date(2026, 1, 2), 0.3, True)]
    r = reconcile.compare_table("t", COLS, _duck(), [c.upper() for c in COLS], snow)
    assert r["problems"] == []
    assert r["fingerprints_equal"] == (4, 4)


def test_changed_value_is_reported_with_its_column() -> None:
    snow = [("SPY", dt.date(2026, 1, 2), 0.31, True), ("QQQ", dt.date(2026, 1, 2), 1.0, False)]
    r = reconcile.compare_table("t", COLS, _duck(), COLS, snow)
    assert r["columns_mismatched"] == ["close"]
    assert r["problems"]


def test_missing_row_and_missing_column_are_reported() -> None:
    r = reconcile.compare_table("t", COLS, _duck(), COLS, _duck()[:1])
    assert any("row count" in p for p in r["problems"])
    r = reconcile.compare_table("t", COLS, _duck(), COLS[:3], [row[:3] for row in _duck()])
    assert any("columns differ" in p for p in r["problems"])


def test_null_and_nan_match_but_null_does_not_match_zero() -> None:
    cols = ["x"]
    assert reconcile.compare_table("t", cols, [(None,)], cols, [(float("nan"),)])["problems"] == []
    assert reconcile.compare_table("t", cols, [(None,)], cols, [(0.0,)])["problems"]


def test_date_timestamp_and_text_are_kept_apart() -> None:
    assert reconcile.norm(dt.date(2026, 1, 2)) != reconcile.norm(dt.datetime(2026, 1, 2))
    assert reconcile.norm("2026-01-02") != reconcile.norm(dt.date(2026, 1, 2))
    assert reconcile.norm(True) != reconcile.norm(1)


def test_integers_compare_exactly_even_when_large() -> None:
    # 成交量这类整数不吃浮点容差：15 亿差 1 股也要报出来。
    cols = ["volume"]
    r = reconcile.compare_table("t", cols, [(1_500_000_001,)], cols, [(1_500_000_000,)])
    assert r["columns_mismatched"] == ["volume"]
    r = reconcile.compare_table("t", cols, [(181_746_419_401,)], cols, [(decimal.Decimal("181746419401"),)])
    assert r["problems"] == []


def test_small_floats_are_held_to_the_relative_tolerance() -> None:
    # 0.001 与 0.0010000009 相对差 9e-7，远超 1e-9；绝对容差 1e-12 不会替它放行。
    cols = ["daily_return"]
    r = reconcile.compare_table("t", cols, [(0.001,)], cols, [(0.0010000009,)])
    assert r["columns_mismatched"] == ["daily_return"]
