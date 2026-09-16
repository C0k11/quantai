"""Snowflake 连接与打码：密钥对登录，参数全部来自环境变量。

本地由 git 忽略的 .env.snowflake.local 提供（--env-file），仓库里没有账号标识、桶名和私钥。
对象名的默认值与 warehouse/profiles.yml 一致，所以只设账号和私钥路径时 dbt 与这些脚本行为相同。
Snowflake 的报错可能带出账号、桶名或 IAM ARN，所以脚本打印报错前统一过 mask()。
"""
from __future__ import annotations

import os
import re
from pathlib import Path


def read_env_file(path: Path) -> None:
    """KEY=VALUE 逐行读进环境变量；已经设过的变量不覆盖。"""
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            key, value = s.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def connect(query_tag: str):
    import snowflake.connector

    e = os.environ
    return snowflake.connector.connect(
        account=e["SNOWFLAKE_ACCOUNT"],
        user=e.get("SNOWFLAKE_USER", "QUANTAI_DBT_USER"),
        role=e.get("SNOWFLAKE_ROLE", "QUANTAI_DBT"),
        warehouse=e.get("SNOWFLAKE_WAREHOUSE", "QUANTAI_WH"),
        database=e.get("SNOWFLAKE_DATABASE", "QUANTAI"),
        authenticator="SNOWFLAKE_JWT",
        private_key_file=e["SNOWFLAKE_PRIVATE_KEY_PATH"],
        login_timeout=30,
        session_parameters={"QUERY_TAG": query_tag},
    )


def mask(text: str, *extra: str) -> str:
    """把账号标识、额外给的值（如桶名）替换掉；任何 s3:// 桶名和 IAM ARN 里的账号 ID 也一律替换。"""
    secrets = [os.environ.get("SNOWFLAKE_ACCOUNT", ""), os.environ.get("SNOWFLAKE_ACCOUNT_LOCATOR", ""), *extra]
    out = str(text)
    for s in sorted({s for s in secrets if s}, key=len, reverse=True):
        out = out.replace(s, "<masked>").replace(s.upper(), "<masked>").replace(s.lower(), "<masked>")
    out = re.sub(r"s3://[^/\s'\"]+", "s3://<masked>", out)
    return re.sub(r"arn:aws:iam::\d{12}", "arn:aws:iam::<masked>", out)
