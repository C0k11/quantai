# 资源组和命名。存储账户、SQL 服务器、Data Factory 的名字全球唯一，带一个随机后缀；
# 后缀只存在 state 里，不进仓库（和桶名一样，这些名字也算标识）。

resource "random_string" "suffix" {
  length  = 6
  upper   = false
  special = false
}

locals {
  suffix = random_string.suffix.result
  tags = {
    project    = "quantai"
    managed_by = "terraform"
  }

  storage_account_name = "quantailake${local.suffix}" # 只能小写字母和数字，3 到 24 位
  sql_server_name      = "quantai-sql-${local.suffix}"
  data_factory_name    = "quantai-adf-${local.suffix}"
  sql_database_name    = "quantai"
  landing_container    = "landing"
}

resource "azurerm_resource_group" "quantai" {
  name     = "quantai"
  location = var.location
  tags     = local.tags
}
