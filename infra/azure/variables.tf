# 真实值只在 gitignore 的 terraform.tfvars 里；仓库只留 terraform.tfvars.example。

variable "location" {
  type        = string
  default     = "canadacentral"
  description = "Azure region for every resource. Canada Central keeps the data residency story of the AWS side (ca-central-1)."
}

variable "alarm_email" {
  type        = string
  description = "Recipient for the pipeline failure alert and the budget notifications."
}

variable "sql_client_ips" {
  type        = list(string)
  default     = []
  description = "Public IPv4 addresses allowed through the SQL firewall for schema setup and reconciliation. Empty means only Azure services (Data Factory) can connect."
}

variable "github_oidc_subject" {
  type        = string
  default     = "repo:C0k11@156249989/quantai@1116026972:ref:refs/heads/main"
  description = "The sub claim GitHub signs for this repository's main branch. It carries immutable owner and repo IDs (public metadata), the same form the AWS deploy role trusts."
}

variable "monthly_budget" {
  type        = number
  default     = 5
  description = "Monthly subscription budget in the billing currency. Alerts go out at 20 percent actual and 100 percent forecast."
}
