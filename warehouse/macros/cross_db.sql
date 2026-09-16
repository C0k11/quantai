{#
  DuckDB 与 Snowflake 写法不同的 SQL 片段，按 adapter 分派（adapter.dispatch）。
  default__ 是 DuckDB 写法（本地和云上 Lambda 用），snowflake__ 是 Snowflake 写法。
  Snowflake 写法都先在无仓库会话里用 EXPLAIN 编译通过，再由 dbt build 实跑验证。
#}

{# 日历日序列：bounds_cte 里从 lo 到 hi（含两端）每天一行，列名 date。 #}
{% macro day_spine(bounds_cte, lo, hi) -%}
    {{ return(adapter.dispatch('day_spine', 'quantai_warehouse')(bounds_cte, lo, hi)) }}
{%- endmacro %}

{% macro default__day_spine(bounds_cte, lo, hi) -%}
    select cast(unnest(generate_series({{ lo }}, {{ hi }}, interval '1 day')) as date) as date
    from {{ bounds_cte }}
{%- endmacro %}

{% macro snowflake__day_spine(bounds_cte, lo, hi) -%}
    select dateadd(day, g.value::int, b.{{ lo }})::date as date
    from {{ bounds_cte }} b,
        lateral flatten(input => array_generate_range(0, datediff(day, b.{{ lo }}, b.{{ hi }}) + 1)) g
{%- endmacro %}

{# 年月字符串 'YYYY-MM'。 #}
{% macro year_month(col) -%}
    {{ return(adapter.dispatch('year_month', 'quantai_warehouse')(col)) }}
{%- endmacro %}

{% macro default__year_month(col) -%}
    strftime({{ col }}, '%Y-%m')
{%- endmacro %}

{% macro snowflake__year_month(col) -%}
    to_char({{ col }}, 'YYYY-MM')
{%- endmacro %}

{# ISO 星期几：1 是周一，7 是周日。 #}
{% macro iso_day_of_week(col) -%}
    {{ return(adapter.dispatch('iso_day_of_week', 'quantai_warehouse')(col)) }}
{%- endmacro %}

{% macro default__iso_day_of_week(col) -%}
    extract(isodow from {{ col }})
{%- endmacro %}

{% macro snowflake__iso_day_of_week(col) -%}
    dayofweekiso({{ col }})
{%- endmacro %}

{#
  ASOF 左连接：左表每行取右表里 right_time <= left_time 的最近一行，找不到就补 NULL。
  Snowflake 的 ASOF JOIN 没有匹配时本身就补 NULL（官方文档）；时间比较写在 MATCH_CONDITION，ON 只能写等值条件。
#}
{% macro asof_left_join(relation, alias, equal_on, left_time, right_time) -%}
    {{ return(adapter.dispatch('asof_left_join', 'quantai_warehouse')(relation, alias, equal_on, left_time, right_time)) }}
{%- endmacro %}

{% macro default__asof_left_join(relation, alias, equal_on, left_time, right_time) -%}
    asof left join {{ relation }} {{ alias }}
        on {{ equal_on }} and {{ right_time }} <= {{ left_time }}
{%- endmacro %}

{% macro snowflake__asof_left_join(relation, alias, equal_on, left_time, right_time) -%}
    asof join {{ relation }} {{ alias }}
        match_condition ({{ left_time }} >= {{ right_time }})
        on {{ equal_on }}
{%- endmacro %}
