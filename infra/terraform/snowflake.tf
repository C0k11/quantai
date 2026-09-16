# Snowflake 从数据湖的 raw/ 前缀装载 Parquet：storage integration QUANTAI_S3_RAW 代入这个角色读桶。
#
# 信任分两步（Snowflake 的标准流程）：
#   1. 先建角色。这时还不知道 Snowflake 的 IAM 用户和 external ID：两个变量显式写成 ""，信任只写本账号 root
#      加占位 external ID "0000"，Snowflake 那边的主体满足不了。
#   2. Snowflake 里建好集成后，DESC INTEGRATION 给出 STORAGE_AWS_IAM_USER_ARN 和 STORAGE_AWS_EXTERNAL_ID，
#      写进 gitignore 的 terraform.tfvars 再 apply，信任收窄到那一个主体加那一个 external ID。
#
# 两个变量故意没有默认值：tfvars 丢了或漏写时 plan 直接报错，而不是悄悄把信任退回占位。
# external ID 标了 sensitive，plan 里整条信任策略只显示 (sensitive value)，看不出主体被换掉，
# 所以另有一个输出 snowflake_trust_is_placeholder，它一变 plan 里就看得见。
#
# 权限只读、只到 raw/：没有 PutObject、没有 DeleteObject，列桶也只能列 raw/ 前缀。
# 写 raw/ 的是 SAM 栈里的 ETL 角色（aws/template.yaml），两边互不重叠。

variable "snowflake_iam_user_arn" {
  type        = string
  description = "STORAGE_AWS_IAM_USER_ARN from DESC INTEGRATION QUANTAI_S3_RAW. Set to \"\" only for the first apply, before the integration exists."
}

variable "snowflake_external_id" {
  type        = string
  sensitive   = true
  description = "STORAGE_AWS_EXTERNAL_ID from DESC INTEGRATION QUANTAI_S3_RAW. Set to \"\" only for the first apply, before the integration exists."

  validation {
    condition     = (var.snowflake_external_id == "") == (var.snowflake_iam_user_arn == "")
    error_message = "Set both snowflake_iam_user_arn and snowflake_external_id, or set both to \"\" for the first apply."
  }
}

locals {
  snowflake_principal   = var.snowflake_iam_user_arn != "" ? var.snowflake_iam_user_arn : "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"
  snowflake_external_id = var.snowflake_external_id != "" ? var.snowflake_external_id : "0000"
}

resource "aws_iam_role" "snowflake_raw_reader" {
  name                 = "quantai-snowflake-raw-reader"
  description          = "Snowflake storage integration QUANTAI_S3_RAW: read-only on the data lake raw/ prefix."
  max_session_duration = 3600

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = local.snowflake_principal }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "sts:ExternalId" = local.snowflake_external_id } }
    }]
  })
}

resource "aws_iam_role_policy" "snowflake_raw_reader" {
  name = "read-raw-prefix-only"
  role = aws_iam_role.snowflake_raw_reader.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadRawObjects"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:GetObjectVersion"]
        Resource = "${aws_s3_bucket.data.arn}/raw/*"
      },
      {
        Sid       = "ListRawPrefixOnly"
        Effect    = "Allow"
        Action    = "s3:ListBucket"
        Resource  = aws_s3_bucket.data.arn
        Condition = { StringLike = { "s3:prefix" = ["raw/*"] } }
      },
      {
        Sid      = "BucketLocation"
        Effect   = "Allow"
        Action   = "s3:GetBucketLocation"
        Resource = aws_s3_bucket.data.arn
      },
    ]
  })
}

output "snowflake_trust_is_placeholder" {
  description = "True while the role still trusts only this account with the placeholder external ID."
  value       = var.snowflake_iam_user_arn == ""
}
