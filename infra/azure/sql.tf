# Azure SQL：查询层。一个逻辑服务器加一个免费档数据库（serverless，每月 100,000 vCore 秒和 32 GB 免费）。
#
# 只开 Entra ID 认证：没有 SQL 登录名和密码。管理员是跑 terraform 的这个登录身份；
# Data Factory 用自己的托管身份连进来，数据库里的用户和授权由 infra/azure/sql_setup.py 建，不在 ARM 里。
# 免费额度用完就自动暂停到下个月（AutoPause），不会转成计费。
#
# 防火墙：0.0.0.0 那条是 Azure 的保留写法，意思是允许 Azure 内部服务（Data Factory 的 Azure IR）连进来；
# 本机的公网 IP 只在 tfvars 里，用于建表和对账。认证仍然只认 Entra ID，放行 IP 不等于放行访问。

resource "azurerm_mssql_server" "quantai" {
  name                          = local.sql_server_name
  resource_group_name           = azurerm_resource_group.quantai.name
  location                      = azurerm_resource_group.quantai.location
  version                       = "12.0"
  minimum_tls_version           = "1.2"
  public_network_access_enabled = true

  azuread_administrator {
    login_username              = "quantai-operator"
    object_id                   = data.azurerm_client_config.current.object_id
    tenant_id                   = data.azurerm_client_config.current.tenant_id
    azuread_authentication_only = true
  }

  tags = local.tags
}

resource "azurerm_mssql_firewall_rule" "azure_services" {
  name             = "AllowAllWindowsAzureIps"
  server_id        = azurerm_mssql_server.quantai.id
  start_ip_address = "0.0.0.0"
  end_ip_address   = "0.0.0.0"
}

resource "azurerm_mssql_firewall_rule" "clients" {
  for_each         = toset(var.sql_client_ips)
  name             = "client-${index(var.sql_client_ips, each.value)}"
  server_id        = azurerm_mssql_server.quantai.id
  start_ip_address = each.value
  end_ip_address   = each.value
}

# azurerm 的 mssql_database 没有 useFreeLimit，只能直接调 ARM。
resource "azapi_resource" "sql_database" {
  type      = "Microsoft.Sql/servers/databases@2025-01-01"
  name      = local.sql_database_name
  parent_id = azurerm_mssql_server.quantai.id
  location  = azurerm_resource_group.quantai.location
  tags      = local.tags

  body = {
    sku = {
      # ARM 把 GP_S_Gen5_2 存成 name GP_S_Gen5 加 capacity 2；这里照它的写法，否则每次 plan 都报改动。
      name     = "GP_S_Gen5"
      tier     = "GeneralPurpose"
      family   = "Gen5"
      capacity = 2
    }
    properties = {
      useFreeLimit                = true
      freeLimitExhaustionBehavior = "AutoPause"
      # 不设 autoPauseDelay：免费档配 AutoPause 时只能用默认值（60 分钟）。
      # 设成 15 实测被拒：ProvisioningDisabled, "Only default value for auto pause delay is allowed for Free Limit database".
      minCapacity                      = 0.5
      maxSizeBytes                     = 34359738368 # 32 GB，免费档的数据上限
      requestedBackupStorageRedundancy = "Local"
      zoneRedundant                    = false
    }
  }
}
