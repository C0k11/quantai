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
