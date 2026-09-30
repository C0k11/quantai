# GitHub Actions 的 azure-sync 任务读湖用的角色：只能读 warehouse/quantai.duckdb 这一个对象（云侧仓库，不含持仓）。
#
# 信任和 CI 部署角色同一个做法（aws/bootstrap/quantai-cicd.yaml）：GitHub OIDC，aud 是 sts.amazonaws.com，
# sub 锁死本仓库 main 分支，带不可变的 owner 和 repo 数字 ID。OIDC provider 是账号级的，
# 由 aws/bootstrap/github-oidc-provider.yaml 建，这里按 URL 引用，不重复建。
# 同一个任务写 Azure 用的身份在 infra/azure/github_oidc.tf，信任的是同一个 sub（那边的同名变量，默认值必须一致）。

variable "github_oidc_subject" {
  type        = string
  default     = "repo:C0k11@156249989/quantai@1116026972:ref:refs/heads/main"
  description = "The sub claim GitHub signs for this repository's main branch (immutable owner and repo IDs). Keep equal to infra/azure."
}

data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

resource "aws_iam_role" "github_azure_sync" {
  name                 = "quantai-github-azure-sync"
  description          = "GitHub Actions azure-sync workflow: read the cloud DuckDB snapshot, nothing else."
  max_session_duration = 3600

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          "token.actions.githubusercontent.com:sub" = var.github_oidc_subject
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "github_azure_sync" {
  name = "read-cloud-duckdb-only"
  role = aws_iam_role.github_azure_sync.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "ReadCloudWarehouseFile"
      Effect   = "Allow"
      Action   = "s3:GetObject"
      Resource = "${aws_s3_bucket.data.arn}/warehouse/quantai.duckdb"
    }]
  })
}

output "github_azure_sync_role_arn" {
  description = "Goes into the AZURE_SYNC_ROLE_ARN repository secret. Sensitive: it contains the account ID."
  value       = aws_iam_role.github_azure_sync.arn
  sensitive   = true
}
