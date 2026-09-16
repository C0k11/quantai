-- fact_positions：持仓事实表（粒度 = as_of × symbol；lots 在 SQL 层聚合）。
-- 估值：ASOF JOIN 取 as_of 当日或之前最近的收盘价（DuckDB 原生 ASOF）。
-- 该表的 unrealized_pnl 与 pandas PortfolioAnalyzer 有一条 pytest 对账测试
-- （SQL 与 pandas 两条独立路径必须算出同一个数字）。
with lots as (
    select * from {{ ref('stg_positions') }}
),

agg as (
    select
        as_of,
        symbol,
        sum(shares)                                        as shares,
        sum(shares * cost_basis) / nullif(sum(shares), 0)  as avg_cost,
        min(open_date)                                     as first_open_date
    from lots
    group by 1, 2
    having sum(shares) <> 0
),

priced as (
    select
        a.*,
        p.close  as last_close,
        p.date   as price_date
    from agg a
    -- DuckDB 写 asof left join，Snowflake 写 asof join ... match_condition，见 macros/cross_db.sql。
    {{ asof_left_join(ref('stg_prices'), 'p', 'a.symbol = p.symbol', 'a.as_of', 'p.date') }}
)

select
    as_of,
    symbol,
    shares,
    avg_cost,
    first_open_date,
    last_close,
    price_date,
    shares * avg_cost                                  as cost_value,
    shares * last_close                                as market_value,
    shares * (last_close - avg_cost)                   as unrealized_pnl,
    case
        -- 守卫与 pandas 完全一致（cost_value <> 0；分母 abs 已处理负成本符号）。
        -- 旧守卫 avg_cost > 0 会把混合多空 lot 聚出的负 avg_cost 判成 NULL，
        -- 与 PortfolioAnalyzer 分叉（对账测试现已覆盖 pct）。
        when shares <> 0 and avg_cost <> 0
        then (shares * (last_close - avg_cost)) / abs(shares * avg_cost)
    end                                                as unrealized_pnl_pct
from priced
