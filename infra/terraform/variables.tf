# 真实值只在 gitignore 的 terraform.tfvars 里；仓库只留 terraform.tfvars.example。

variable "data_bucket_name" {
  type        = string
  description = "Existing S3 data lake bucket. SAM references it by name; only Terraform manages it."
}

variable "alarm_email" {
  type        = string
  description = "Recipient for both budget alerts."
}
