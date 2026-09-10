"""`--no-positions` 的接线测试：云侧 ETL 必须真的不装载头寸。

这是边界开关，不是普通 flag——错接了不会报错，只会在云端悄悄多出一张
raw.positions。所以钉住 CLI 到 `_load_raw` 的传参。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "warehouse_cli", Path(__file__).resolve().parents[2] / "scripts" / "warehouse.py"
)
warehouse_cli = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(warehouse_cli)


@pytest.fixture
def captured(monkeypatch):
    """拦掉真正的装载，只记录 _load_raw 收到的参数。"""
    seen: dict = {}

    def fake_load_raw(db_path, portfolio_file, years, benchmark, with_positions=True):
        seen["with_positions"] = with_positions

    monkeypatch.setattr(warehouse_cli, "_load_raw", fake_load_raw)
    return seen


def test_default_loads_positions(captured, tmp_path):
    warehouse_cli.main(["--load", "--db", str(tmp_path / "x.duckdb")])
    assert captured["with_positions"] is True


def test_no_positions_flag_blocks_them(captured, tmp_path):
    warehouse_cli.main(["--load", "--no-positions", "--db", str(tmp_path / "x.duckdb")])
    assert captured["with_positions"] is False, "云侧模式绝不能装载头寸"


def test_full_respects_no_positions(captured, monkeypatch, tmp_path):
    """--full 展开成 load+dbt+export，边界开关不能在展开时被丢掉。"""
    monkeypatch.setattr(warehouse_cli, "_run_dbt", lambda *a, **k: None)
    monkeypatch.setattr(warehouse_cli, "_export", lambda *a, **k: None)

    warehouse_cli.main(["--full", "--no-positions", "--db", str(tmp_path / "x.duckdb")])

    assert captured["with_positions"] is False
