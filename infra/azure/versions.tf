# QuantAI 的 Azure 一侧：把 AWS 数据湖里的 marts 快照每晚装进 Azure SQL，供查询。
#
# 数据流（每个工作日夜里一次）：
#   AWS ETL (01:00Z) 写 s3://<lake>/warehouse/quantai.duckdb
#   -> GitHub Actions（两边都走 OIDC，没有长期密钥）把 marts 导成 Parquet，放进 ADLS Gen2 的 landing/marts/<快照>/，
#      最后写 _manifest.csv（表名和行数）
#   -> _manifest.csv 落地触发 Data Factory 的存储事件触发器：逐表复制进 stage，
#      再由 ops.publish_marts 核对行数，在一个事务里发布到 marts
#   -> 失败时 Azure Monitor 告警发邮件
#
# 分工：这里管 Azure 的全部资源。GitHub 读湖用的 AWS 只读角色在 infra/terraform/（AWS 账号那一侧），
# 和 Snowflake 读 raw/ 的角色放在一起。
# 头寸数据不会过来：云上的 DuckDB 本来就不含持仓，导出脚本另有白名单和体检。
#
# state 和 AWS 那一侧共用同一个 state 桶，key 不同：
#   terraform init -backend-config=../terraform/backend.local.hcl   # 桶名只在那个 gitignore 的文件里
#   terraform plan
# 订阅 ID 不进仓库：azurerm 默认用 az CLI 当前选中的订阅。
terraform {
  required_version = ">= 1.10"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 5.7"
    }
    # azurerm 的 mssql_database 不支持免费档（useFreeLimit），数据库本身用 AzAPI 直接调 ARM 建。
    azapi = {
      source  = "azure/azapi"
      version = "~> 2.13"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.9"
    }
  }

  backend "s3" {
    key          = "quantai/azure.tfstate"
    region       = "ca-central-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "azurerm" {
  features {}

  # 5.0 起默认不自动注册资源提供程序；只注册这套配置实际用到的。
  resource_providers_to_register = [
    "Microsoft.DataFactory",
    "Microsoft.EventGrid", # Data Factory 的存储事件触发器靠它
    "Microsoft.Insights",
    "Microsoft.ManagedIdentity",
    "Microsoft.Sql",
    "Microsoft.Storage",
  ]

  # 存储账户关了共享密钥，数据面一律走 Entra ID。
  storage_use_azuread = true
}

provider "azapi" {}

data "azurerm_client_config" "current" {}

data "azurerm_subscription" "current" {}
