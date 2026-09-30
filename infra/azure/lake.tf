# ADLS Gen2（开了分层命名空间的 StorageV2），只放每晚的 marts 快照：landing/marts/<快照>/*.parquet 加 _manifest.csv。
#
# 关掉共享密钥：没有账户密钥、没有 SAS，谁读谁写都靠 Entra ID 身份加 RBAC。
# 写入方只有两个：GitHub Actions 的托管身份（github_oidc.tf）和我本人（手动补跑、排查）；Data Factory 只读。
# 快照 30 天后自动删除：数据源头在 AWS 湖里，这里只是落地区。

resource "azurerm_storage_account" "lake" {
  name                             = local.storage_account_name
  resource_group_name              = azurerm_resource_group.quantai.name
  location                         = azurerm_resource_group.quantai.location
  account_kind                     = "StorageV2"
  account_tier                     = "Standard"
  account_replication_type         = "LRS"
  access_tier                      = "Hot"
  is_hns_enabled                   = true
  min_tls_version                  = "TLS1_2"
  https_traffic_only_enabled       = true
  shared_access_key_enabled        = false
  default_to_oauth_authentication  = true
  allow_nested_items_to_be_public  = false
  cross_tenant_replication_enabled = false
  tags                             = local.tags
}

resource "azurerm_storage_container" "landing" {
  name                  = local.landing_container
  storage_account_id    = azurerm_storage_account.lake.id
  container_access_type = "private"
}

resource "azurerm_storage_management_policy" "lake" {
  storage_account_id = azurerm_storage_account.lake.id

  rule {
    name    = "expire-landing-snapshots"
    enabled = true
    filters {
      blob_types   = ["blockBlob"]
      prefix_match = ["${local.landing_container}/marts/"]
    }
    actions {
      base_blob {
        delete_after_days_since_modification_greater_than = 30
      }
    }
  }
}

# Data Factory 只读 landing。
resource "azurerm_role_assignment" "adf_reads_landing" {
  scope                = azurerm_storage_container.landing.id
  role_definition_name = "Storage Blob Data Reader"
  principal_id         = azurerm_data_factory.quantai.identity[0].principal_id
  principal_type       = "ServicePrincipal"
}

# 我本人（跑 terraform 的这个登录身份）可以手动放快照和排查。
resource "azurerm_role_assignment" "operator_writes_landing" {
  scope                = azurerm_storage_container.landing.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = data.azurerm_client_config.current.object_id
  principal_type       = "User"
}
