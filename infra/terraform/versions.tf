# QuantAI 账号级基础设施：只管 SAM 没管的东西。
#
# 分工（同一个资源绝不被两套 IaC 同时管）：
#   aws/template.yaml (SAM)        Lambda、HTTP API、IAM 执行角色、日志组、EventBridge、告警
#   aws/bootstrap/*.yaml (CFN)     GitHub OIDC provider、CI 部署角色、CFN 执行角色
#   infra/terraform/ (这里)         数据湖桶（SAM 只按名字引用它，从不创建它）、两个 Budgets
#
# 这里管的资源最初是手工/CLI 建的，用下面 imports.tf 的 import 块收编进 state，
# 不是重建——数据湖里的数据不动。
#
# 装 state 的那个桶是唯一的 CLI bootstrap 资源：先有桶才能放 state，Terraform 管不了
# 装自己 state 的桶（鸡生蛋）。它与数据湖桶分开，加固配置一致。
#
#   terraform init -backend-config=backend.local.hcl   # 桶名只在 gitignore 的这个文件里
#   terraform plan
terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  backend "s3" {
    key     = "quantai/account.tfstate"
    region  = "ca-central-1"
    encrypt = true
    # S3 原生锁（Terraform 1.10+），不再需要单独一张 DynamoDB 表来防并发写。
    use_lockfile = true
  }
}

provider "aws" {
  region = "ca-central-1"
}

data "aws_caller_identity" "current" {}
