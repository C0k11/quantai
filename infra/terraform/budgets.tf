# 两个预算告警，最初是在控制台里建的，这里收编。
#
# 收编时顺手修掉 Phase 0 留下的缺口：控制台建的预算 CostTypes 为空 = 用默认值 =
# **计入 credits 与 refund**。账号有 $100 免费额度，默认口径下费用被额度抵成 $0，
# 预算在额度烧完之前一直显示 $0，$1 告警永远不响。排除 credit/refund 后预算看的是
# 抵扣前的毛额，真实用量一出现就能报警。
#
# 控制台那条路（Scope -> Charge type -> Excludes -> Credit）要等 Cost Explorer 攒够数据
# 才有候选值；Budgets API 的 cost_types 没有这个依赖。
#
# 为什么 plan 里同时出现"删 metrics、加 cost_types"：Budgets 有两套互斥的口径模型——
# 经典的 cost_types，与较新的 metrics + filter_expression（provider 要求这两个同时给）。
# 控制台建出的预算落在新模型里（Metrics=UnblendedCost 却没有 FilterExpression），这个组合
# provider 表达不了。选经典模型才能把"排除 credits"写成代码，所以删 metrics 是同一处变更
# 的一部分，不是误伤；use_blended/use_amortized 均为 false，口径仍是 unblended，与原来一致。
#
# time_period_start/end 必须与线上完全一致，否则 provider 会判定为替换（先删后建）。

locals {
  gross_cost = {
    include_credit = false
    include_refund = false
  }
}

resource "aws_budgets_budget" "actual_1usd" {
  name              = "alarm-actual-1usd"
  budget_type       = "COST"
  limit_amount      = "1.0"
  limit_unit        = "USD"
  time_unit         = "MONTHLY"
  time_period_start = "2026-09-01_00:00"
  time_period_end   = "2087-06-15_00:00"

  cost_types {
    include_credit = local.gross_cost.include_credit
    include_refund = local.gross_cost.include_refund
  }

  notification {
    notification_type          = "ACTUAL"
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    subscriber_email_addresses = [var.alarm_email]
  }
}

resource "aws_budgets_budget" "forecast_5usd" {
  name              = "alarm-forecast-5usd"
  budget_type       = "COST"
  limit_amount      = "5.0"
  limit_unit        = "USD"
  time_unit         = "MONTHLY"
  time_period_start = "2026-09-01_00:00"
  time_period_end   = "2087-06-15_00:00"

  cost_types {
    include_credit = local.gross_cost.include_credit
    include_refund = local.gross_cost.include_refund
  }

  notification {
    notification_type          = "FORECASTED"
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    subscriber_email_addresses = [var.alarm_email]
  }
}
