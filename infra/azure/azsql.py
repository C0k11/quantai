"""Azure SQL 连接与打码：只用 Entra ID 令牌登录（服务器关了 SQL 认证），令牌取自本机 az CLI 当前登录的身份。

服务器和数据库名：先看 AZURE_SQL_SERVER、AZURE_SQL_DATABASE 环境变量，没有就读 infra/azure 的 terraform output。
这些名字是标识，不进仓库、不打印；脚本打印报错前统一过 mask()。
无服务器数据库暂停后第一次连接会报 40613（正在恢复），这里等 20 秒重试，最多 6 次。
驱动是 ODBC Driver 18 for SQL Server（pyodbc 只在用到时才导入，离线测试不需要它）。
"""
from __future__ import annotations

import os
import re
import shutil
import struct
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SQL_COPT_SS_ACCESS_TOKEN = 1256  # msodbcsql.h
TOKEN_RESOURCE = "https://database.windows.net/"
GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def _az() -> str:
    exe = shutil.which("az") or shutil.which("az.cmd") or r"D:\AzureCLI\bin\az.cmd"
    return exe


def tf_output(name: str) -> str:
    terraform = shutil.which("terraform") or r"D:\Terraform\terraform.exe"
    env = dict(os.environ, PATH=str(Path(_az()).parent) + os.pathsep + os.environ.get("PATH", ""))
    p = subprocess.run([terraform, "output", "-raw", name], cwd=HERE, capture_output=True, text=True, env=env)
    if p.returncode != 0 or not p.stdout.strip():
        raise SystemExit(f"读不到 terraform output {name}（先在 infra/azure 跑过 init 和 apply）")
    return p.stdout.strip()


def server_and_database() -> tuple[str, str]:
    server = os.environ.get("AZURE_SQL_SERVER") or tf_output("sql_server_fqdn")
    database = os.environ.get("AZURE_SQL_DATABASE") or tf_output("sql_database_name")
    return server, database


def access_token() -> str:
    p = subprocess.run([_az(), "account", "get-access-token", "--resource", TOKEN_RESOURCE,
                        "--query", "accessToken", "--output", "tsv"], capture_output=True, text=True)
    if p.returncode != 0 or not p.stdout.strip():
        raise SystemExit("az 拿不到 Azure SQL 的访问令牌：先 az login（个人账号要带 --tenant）")
    return p.stdout.strip()


def _token_struct(token: str) -> bytes:
    raw = token.encode("utf-16-le")
    return struct.pack("<I", len(raw)) + raw


def connect(attempts: int = 6, wait_s: int = 20):
    import pyodbc

    server, database = server_and_database()
    conn_str = (f"Driver={{ODBC Driver 18 for SQL Server}};Server=tcp:{server},1433;Database={database};"
                "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=60")
    token = _token_struct(access_token())
    for i in range(1, attempts + 1):
        try:
            return pyodbc.connect(conn_str, attrs_before={SQL_COPT_SS_ACCESS_TOKEN: token}, autocommit=True)
        except pyodbc.Error as exc:
            if "40613" not in str(exc) or i == attempts:
                raise
            print(f"[azsql] 数据库在从暂停中恢复（40613），{wait_s} 秒后重试（{i}/{attempts}）")
            time.sleep(wait_s)
    raise AssertionError("unreachable")


def mask(text, *extra: str) -> str:
    """服务器、账户、工厂名（带随机后缀的全局唯一名）和一切 GUID（订阅、租户、对象 ID）一律替换。"""
    out = str(text)
    names = [os.environ.get("AZURE_SQL_SERVER", ""), *extra]
    for s in sorted({s for s in names if s}, key=len, reverse=True):
        out = out.replace(s, "<masked>")
    out = re.sub(r"quantai(?:lake[a-z0-9]{6}|-(?:sql|adf)-[a-z0-9]{6})", "<masked>", out)
    return GUID.sub("<masked>", out)
