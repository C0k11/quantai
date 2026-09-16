-- fact_news：新闻事实表（粒度 = link；date 由发布时间派生，可 join dim_date）。
-- published 缺失（源没给发布时间）时 date 为 NULL——诚实缺失，不用抓取时间冒充。
-- sentiment 由 LLM 标题级打分（LEFT JOIN：没打过分的头条 sentiment 为 NULL，
-- 不填 0 冒充中性）；BI 情绪时间线 = date × avg(sentiment) 按 symbol 分组。
select
    n.link,
    n.symbol,
    n.title,
    n.summary,
    -- published 是 UTC 墙钟：按美东时区归日再取 date——直接 cast 会把美东晚间
    -- 8 点后的新闻全记到"第二天"，情绪时间线与交易日系统性错位（审查实锤）
    -- DuckDB 的 timezone() 在 Snowflake 上是 convert_timezone()，见 macros/cross_db.sql。
    {{ local_date_from_utc('n.published', 'America/New_York') }} as date,
    n.published,
    n.source,
    s.sentiment,
    s.label                    as sentiment_label,
    s.scored_at
from {{ ref('stg_news') }} n
left join {{ ref('stg_news_scores') }} s using (link)
