# 声明式收编（Terraform 1.5+ import 块）：收编过程本身进代码评审，而不是某人
# 在终端里敲过一次 `terraform import`、没人知道。已在 state 里的资源 Terraform 会
# 跳过这些块，所以留着无害，也记录了这些资源的来路。
#
# id 全部走变量/数据源，仓库里没有桶名、没有账号 ID。

import {
  to = aws_s3_bucket.data
  id = var.data_bucket_name
}

import {
  to = aws_s3_bucket_versioning.data
  id = var.data_bucket_name
}

import {
  to = aws_s3_bucket_server_side_encryption_configuration.data
  id = var.data_bucket_name
}

import {
  to = aws_s3_bucket_public_access_block.data
  id = var.data_bucket_name
}

import {
  to = aws_s3_bucket_ownership_controls.data
  id = var.data_bucket_name
}

import {
  to = aws_s3_bucket_policy.data
  id = var.data_bucket_name
}

import {
  to = aws_budgets_budget.actual_1usd
  id = "${data.aws_caller_identity.current.account_id}:alarm-actual-1usd"
}

import {
  to = aws_budgets_budget.forecast_5usd
  id = "${data.aws_caller_identity.current.account_id}:alarm-forecast-5usd"
}
