# GitHub Actions 写 landing 用的身份：user-assigned managed identity 加一条联合凭据，没有 client secret。
#
# 和 AWS 部署角色同一个做法：只信任 GitHub 给本仓库 main 分支签的 token，sub 里带不可变的 owner 和 repo 数字 ID，
# 仓库改名不影响信任，别人抢注同名仓库也冒充不了。audience 是 Azure 的固定值 api://AzureADTokenExchange。
# 权限只有 landing 容器的 Storage Blob Data Contributor：能放快照，碰不到 SQL、Data Factory 和订阅里的其他东西。

resource "azurerm_user_assigned_identity" "github_sync" {
  name                = "quantai-github-sync"
  resource_group_name = azurerm_resource_group.quantai.name
  location            = azurerm_resource_group.quantai.location
  tags                = local.tags
}

resource "azurerm_federated_identity_credential" "github_main" {
  name                      = "github-main"
  user_assigned_identity_id = azurerm_user_assigned_identity.github_sync.id
  audience                  = ["api://AzureADTokenExchange"]
  issuer                    = "https://token.actions.githubusercontent.com"
  subject                   = var.github_oidc_subject
}

resource "azurerm_role_assignment" "github_writes_landing" {
  scope                = azurerm_storage_container.landing.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.github_sync.principal_id
  principal_type       = "ServicePrincipal"
}
