"""薄 CLI：数据仓库编排（ETL 装载 -> dbt build -> BI 导出）。

示例：
    # 端到端：抓真实行情 + 加载持仓/信号/回测 + dbt build + 导出 BI 数据源
    python scripts/warehouse.py --full --portfolio portfolio.example.yaml

    # 只重跑 SQL 转换层（raw 不动）
    python scripts/warehouse.py --dbt

    # 只导出 marts 层给 BI（CSV，见 powerbi/SPEC.md）
    python scripts/warehouse.py --export

计算路径：yfinance/持仓/信号/回测 ->(pandas ETL)-> raw ->(dbt SQL)-> staging -> marts
->(export)-> data/exports/*.csv 或 BI 直连 DuckDB。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_REPO = Path(__file__).resolve().parent.parent
_DBT_DIR = _REPO / "warehouse"
_DEFAULT_DB = _REPO / "data" / "warehouse" / "quantai.duckdb"
# 导出目录可用 QUANTAI_EXPORT_DIR 覆盖：Lambda 的文件系统只读，只有 /tmp 能写。
_EXPORT_DIR = Path(os.environ.get("QUANTAI_EXPORT_DIR") or (_REPO / "data" / "exports"))

_MARTS = (
    "dim_date", "dim_symbol", "fact_prices", "fact_positions",
    "fact_trades", "fact_signals", "fact_backtest_results", "fact_backtest_equity",
    "fact_news", "fact_event_odds",
)


def _drop_partial_today(prices: dict) -> int:
    """收盘口径守卫：剔掉"今天还没走完"的日线尾巴再入库。

    盘中跑 --load 时 yfinance 会把当日**进行中**的 bar 当日线返回（close=最新价、
    volume=半日量），全量替换进 raw.prices 后无任何临时标记，dbt/BI 全按
    收盘价消费（审查实锤：75 秒内两次抓取同一"日线"volume 在涨）。规则：
    美股尾根 bar 日期=纽约今天且未到收盘（16:10 缓冲）-> 剔；加密（-USD，UTC
    0 点切日）尾根 bar 日期=UTC 今天 -> 剔。宁可少一根完整 bar（下次跑补回），
    绝不让半根 bar 冒充收盘。
    """
    from zoneinfo import ZoneInfo

    dropped = 0
    now_et = datetime.now(ZoneInfo("America/New_York"))
    today_utc = datetime.now(ZoneInfo("UTC")).date()
    for sym, df in list(prices.items()):
        if df is None or df.empty:
            continue
        last = df.index[-1]
        last_date = last.date() if hasattr(last, "date") else last
        if str(sym).upper().endswith("-USD"):
            partial = last_date == today_utc
        else:
            partial = last_date == now_et.date() and (now_et.hour, now_et.minute) < (16, 10)
        if partial:
            prices[sym] = df.iloc[:-1]
            dropped += 1
    return dropped


def _load_raw(db_path: Path, portfolio_file: str, years: int, benchmark: str,
              with_positions: bool = True) -> None:
    """装载 raw 层。

    `with_positions=False` 是云侧（Lambda）用的：头寸数字按设计不出本地边界，
    所以云上既不读 portfolio 文件也不写 raw.positions。标的全集不受影响——
    自选股清单已覆盖持仓标的，行情/新闻照抓。
    """
    from quantai.backtest import run_backtest
    from quantai.config import load_config
    from quantai.data.news import NewsFetcher
    from quantai.data.prices import PriceFetcher
    from quantai.portfolio import load_portfolio
    from quantai.signals.generator import SignalGenerator
    from quantai.warehouse import (
        connect, load_backtest, load_news, load_positions, load_prices,
        load_signals, load_trading_days,
    )
    from quantai.warehouse.etl import init_raw_tables
    import pandas as pd

    from quantai.data.watchlist import load_watchlist

    portfolio = load_portfolio(portfolio_file) if with_positions else None
    watchlist = load_watchlist(load_config().portfolio.watchlist_file)
    held = portfolio.symbols if portfolio is not None else []
    symbols = list(dict.fromkeys(held + watchlist + [benchmark]))
    start = (datetime.now() - timedelta(days=years * 365)).strftime("%Y-%m-%d")
    end = datetime.now().strftime("%Y-%m-%d")

    print(f"[etl] fetching {symbols} since {start} ...")
    prices = PriceFetcher().fetch_prices(symbols, start)
    n_partial = _drop_partial_today(prices)
    if n_partial:
        print(f"[etl] 收盘口径守卫：剔除 {n_partial} 根当日进行中 bar（收盘后重跑会补回）")
    con = connect(db_path)
    try:
        init_raw_tables(con)  # 全部 raw 表建齐（含还没有数据的，如 trades）——dbt 才能全模型编译
        n = load_prices(con, prices)
        print(f"[etl] raw.prices          +{n}")
        n = load_trading_days(con, start, end)
        print(f"[etl] raw.trading_days    +{n}")
        as_of = max(str(df.index[-1].date()) for df in prices.values()) if prices else end
        if portfolio is not None:
            n = load_positions(con, portfolio, as_of=as_of)
            print(f"[etl] raw.positions       +{n} (as_of {as_of})")
        else:
            print("[etl] raw.positions       skipped（头寸不出本地边界）")
        gen = SignalGenerator()
        for sym, df in prices.items():
            load_signals(con, sym, gen.generate(df))
        print(f"[etl] raw.signals         {len(prices)} symbols")
        news = NewsFetcher(extra_feeds=load_config().data.news_feeds).fetch_all(symbols)
        n = load_news(con, news)
        print(f"[etl] raw.news            +{n} new items ({len(news)} fetched)")
        event_tags = load_config().data.event_tags
        if event_tags:
            from quantai.data.events import EventsFetcher
            from quantai.warehouse import load_event_odds

            odds = EventsFetcher().fetch_all(event_tags, limit_per_tag=15)
            n = load_event_odds(con, odds, as_of=as_of)
            print(f"[etl] raw.event_odds      +{n} markets (tags {event_tags})")
        if benchmark in prices:
            df = prices[benchmark]
            weight = pd.Series(0.6, index=df.index)  # demo：60% 恒权重基准回测
            result = run_backtest(df, weight)
            load_backtest(con, f"const60_{benchmark}", result, strategy="const_weight_0.6", symbol=benchmark)
            print(f"[etl] raw.backtest_*      run=const60_{benchmark} (sharpe {result.metrics.sharpe:.2f})")
    finally:
        con.close()


def _run_dbt(db_path: Path) -> None:
    env = {**os.environ, "QUANTAI_DB_PATH": str(db_path)}
    dbt_exe = Path(sys.executable).parent / ("dbt.exe" if os.name == "nt" else "dbt")
    cmd = [str(dbt_exe), "build", "--project-dir", str(_DBT_DIR), "--profiles-dir", str(_DBT_DIR)]
    print(f"[dbt] {' '.join(cmd)}")
    res = subprocess.run(cmd, env=env)
    if res.returncode != 0:
        raise SystemExit(f"dbt build 失败（exit {res.returncode}）")


def _export(db_path: Path, with_positions: bool = True) -> None:
    """导出 marts -> CSV。

    `with_positions=False` 时连 `fact_positions` 这张表都不导——云侧那张表本来
    就是空的，但导出一个叫 fact_positions.csv 的文件会让下游的边界审计无法区分
    "空表"和"真泄露"。让边界在源头就成立，而不是靠下游放宽判据。
    """
    from quantai.warehouse import connect

    tables = _MARTS if with_positions else tuple(t for t in _MARTS if t != "fact_positions")
    _EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    con = connect(db_path)
    try:
        for t in tables:
            out = _EXPORT_DIR / f"{t}.csv"
            path_sql = out.as_posix().replace("'", "''")  # 路径含单引号时转义（SQL 字面量）
            con.execute(
                f"COPY (SELECT * FROM marts.{t}) TO '{path_sql}' (HEADER, DELIMITER ',')"
            )
            n = con.execute(f"SELECT count(*) FROM marts.{t}").fetchone()[0]
            print(f"[export] {out.name:<28} {n:>8} rows")
    finally:
        con.close()
    print(f"[export] done -> {_EXPORT_DIR}")


def main(argv: list[str] | None = None) -> int:
    from quantai.config import load_config

    # 默认值统一从 PortfolioConfig 取（与 scripts/analyze.py 同源）——否则用户在
    # config/env 覆盖 portfolio.file/benchmark 后，两个 CLI 会静默分析不同的组合。
    cfg = load_config().portfolio
    p = argparse.ArgumentParser(description="QuantAI warehouse orchestrator")
    p.add_argument("--db", default=str(_DEFAULT_DB), help="DuckDB 文件路径")
    p.add_argument("--portfolio", default=cfg.file, help=f"持仓文件（默认 {cfg.file}）")
    p.add_argument("--benchmark", default=cfg.benchmark)
    p.add_argument("--years", type=int, default=cfg.history_years)
    p.add_argument("--load", action="store_true", help="pandas ETL 装载 raw 层")
    p.add_argument("--dbt", action="store_true", help="跑 dbt build（staging+marts+tests）")
    p.add_argument("--export", action="store_true", help="导出 marts -> data/exports/*.csv")
    p.add_argument("--full", action="store_true", help="load + dbt + export 一条龙")
    p.add_argument("--no-positions", action="store_true",
                   help="不读持仓文件、不写 raw.positions（云侧 Lambda 用：头寸不出本地边界）")
    args = p.parse_args(argv)

    db_path = Path(args.db)
    if args.full:
        args.load = args.dbt = args.export = True
    if not (args.load or args.dbt or args.export):
        p.print_help()
        return 1
    if args.load:
        _load_raw(db_path, args.portfolio, args.years, args.benchmark,
                  with_positions=not args.no_positions)
    if args.dbt:
        _run_dbt(db_path)
    if args.export:
        _export(db_path, with_positions=not args.no_positions)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
