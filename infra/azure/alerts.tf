# 告警：管道失败、触发器失败各一条，发邮件；外加订阅预算，和 AWS 那边的 $1 实际 / $5 预测同一个思路。
#
# 预算口径：Cost Management 显示的成本不扣免费额度和预付额度（Learn「Understand Cost Management data」），
# 所以试用期里真实用量一出现就会通知，不会像 AWS 控制台默认口径那样被额度抵成 0（AWS 那边的坑见 infra/terraform/budgets.tf）。
# 免费试用（FreeTrial_2014-09-01）在同一篇文档的支持列表里。
# start_date 必须是某月 1 号，改了会重建预算，所以写死。

resource "azurerm_monitor_action_group" "email" {
  name                = "quantai-alerts"
  resource_group_name = azurerm_resource_group.quantai.name
  short_name          = "quantai"

  email_receiver {
    name                    = "owner"
    email_address           = var.alarm_email
    use_common_alert_schema = true
  }

  tags = local.tags
}

resource "azurerm_monitor_metric_alert" "pipeline_failed" {
  name                = "quantai-adf-pipeline-failed"
  resource_group_name = azurerm_resource_group.quantai.name
  scopes              = [azurerm_data_factory.quantai.id]
  description         = "A Data Factory pipeline run failed (the nightly marts load)."
  severity            = 2
  frequency           = "PT15M"
  window_size         = "PT1H"

  criteria {
    metric_namespace = "Microsoft.DataFactory/factories"
    metric_name      = "PipelineFailedRuns"
    aggregation      = "Total"
    operator         = "GreaterThan"
    threshold        = 0
  }

  action {
    action_group_id = azurerm_monitor_action_group.email.id
  }

  tags = local.tags
}

resource "azurerm_monitor_metric_alert" "trigger_failed" {
  name                = "quantai-adf-trigger-failed"
  resource_group_name = azurerm_resource_group.quantai.name
  scopes              = [azurerm_data_factory.quantai.id]
  description         = "The storage event trigger failed to start the load."
  severity            = 2
  frequency           = "PT15M"
  window_size         = "PT1H"

  criteria {
    metric_namespace = "Microsoft.DataFactory/factories"
    metric_name      = "TriggerFailedRuns"
    aggregation      = "Total"
    operator         = "GreaterThan"
    threshold        = 0
  }

  action {
    action_group_id = azurerm_monitor_action_group.email.id
  }

  tags = local.tags
}

resource "azurerm_consumption_budget_subscription" "monthly" {
  name            = "quantai-monthly"
  subscription_id = data.azurerm_subscription.current.id
  amount          = var.monthly_budget
  time_grain      = "Monthly"

  time_period {
    start_date = "2026-09-01T00:00:00Z"
  }

  notification {
    enabled        = true
    operator       = "GreaterThan"
    threshold      = 20
    threshold_type = "Actual"
    contact_emails = [var.alarm_email]
  }

  notification {
    enabled        = true
    operator       = "GreaterThan"
    threshold      = 100
    threshold_type = "Forecasted"
    contact_emails = [var.alarm_email]
  }
}
