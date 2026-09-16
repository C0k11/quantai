-- fact_prices：日行情事实表（粒度 = symbol × date）。
-- 派生列用窗口函数在 SQL 层算（daily_return / 52 周高点距离），BI 不用再拉明细回 pandas。
select
    p.symbol,
    p.date,
    p.open,
    p.high,
    p.low,
    p.close,
    p.volume,
    -- 窗口全部内联写：Snowflake 不支持命名 WINDOW 子句，内联后 DuckDB 与 Snowflake 用同一份 SQL。
    p.close / lag(p.close) over (partition by p.symbol order by p.date) - 1 as daily_return,
    -- 不足 252 根时窗口只是"上市以来高点"，冒充 52 周高点会误导 -> 诚实置 NULL。
    case
        when count(*) over (
            partition by p.symbol order by p.date
            rows between 251 preceding and current row
        ) >= 252
        then p.close / max(p.high) over (
            partition by p.symbol order by p.date
            rows between 251 preceding and current row
        ) - 1
    end                                                          as pct_from_52w_high
from {{ ref('stg_prices') }} p
