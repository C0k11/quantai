# 这些值要进 GitHub 的 repository secrets 和本机脚本，但都是标识，一律 sensitive：plan、apply 不打印，
# 要用时 terraform output -raw <名字> 直接管道给下一个命令，不落在终端和日志里。

output "storage_account_name" {
  value     = azurerm_storage_account.lake.name
  sensitive = true
}

output "sql_server_fqdn" {
  value     = azurerm_mssql_server.quantai.fully_qualified_domain_name
  sensitive = true
}

output "sql_database_name" {
  value = local.sql_database_name
}

output "data_factory_name" {
  description = "Also the name of the Data Factory managed identity inside the database (CREATE USER ... FROM EXTERNAL PROVIDER)."
  value       = azurerm_data_factory.quantai.name
  sensitive   = true
}

output "github_sync_client_id" {
  value     = azurerm_user_assigned_identity.github_sync.client_id
  sensitive = true
}

output "tenant_id" {
  value     = data.azurerm_client_config.current.tenant_id
  sensitive = true
}

output "subscription_id" {
  value     = data.azurerm_client_config.current.subscription_id
  sensitive = true
}
