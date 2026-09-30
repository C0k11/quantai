"""在 Azure SQL 里建装载要用的对象，并给 Data Factory 的托管身份授权。可以重复跑：已有且一致的跳过，结构不符就停。

建什么：
1. schema：stage（Data Factory 复制进来的落地表）、marts（查询层）、ops（控制表和存储过程）。
2. 白名单（export_marts.MARTS_EXPORT_TABLES）里每张表在 stage 和 marts 各建一张，列和类型取自云侧 DuckDB 的 marts，
   和 export_marts 导出的 Parquet 是同一份结构。类型对应见 TSQL_TYPES；遇到别的类型就停，不猜。
3. ops.marts_tables（装载白名单，与导出白名单同步）、ops.load_log（每次发布一行）。
4. ops.truncate_stage、ops.publish_marts：EXECUTE AS OWNER，Data Factory 只要执行权限，不要表的 ALTER、DELETE 权限。
   publish_marts：清单里的表必须正好是白名单，每张 stage 表的行数必须等于清单，然后在一个事务里整体替换 marts。
5. Data Factory 的托管身份建成数据库用户，只有 stage 的 SELECT、INSERT 和 ops 的 EXECUTE。
   用 WITH SID ... TYPE = E 建，不走 FROM EXTERNAL PROVIDER：后者要拿管理员身份去查目录，
   个人 Microsoft 账号当管理员时查不到。SID 是托管身份的 appId 按 uniqueidentifier 的二进制写法（小端）。

用法：
    python infra/azure/sql_setup.py --duckdb <云侧 quantai.duckdb> [--recreate]
--recreate：marts 的列变了（dbt 改了模型）时用，删掉重建 stage 和 marts 的表；marts 在下一次装载后补回。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import azsql  # noqa: E402

_spec = importlib.util.spec_from_file_location("export_marts", HERE / "export_marts.py")
EXPORT = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(EXPORT)

TSQL_TYPES = {
    "BIGINT": "bigint",
    "INTEGER": "int",
    "DOUBLE": "float",
    "BOOLEAN": "bit",
    "DATE": "date",
    "TIMESTAMP": "datetime2(6)",
    "VARCHAR": "nvarchar(max)",
}

OPS_DDL = """
IF OBJECT_ID(N'ops.marts_tables') IS NULL
    CREATE TABLE ops.marts_tables (table_name sysname NOT NULL PRIMARY KEY);
IF OBJECT_ID(N'ops.load_log') IS NULL
    CREATE TABLE ops.load_log (
        id int IDENTITY(1, 1) NOT NULL PRIMARY KEY,
        snapshot nvarchar(200) NOT NULL,
        run_id nvarchar(100) NOT NULL,
        tables int NOT NULL,
        row_count bigint NOT NULL,
        published_at_utc datetime2(0) NOT NULL
    );
"""

TRUNCATE_STAGE = """
CREATE OR ALTER PROCEDURE ops.truncate_stage @table sysname
WITH EXECUTE AS OWNER
AS
BEGIN
    SET NOCOUNT ON;
    IF NOT EXISTS (SELECT 1 FROM ops.marts_tables WHERE table_name = @table)
        THROW 50001, N'truncate_stage: table is not in ops.marts_tables', 1;
    DECLARE @sql nvarchar(400) = N'TRUNCATE TABLE stage.' + QUOTENAME(@table) + N';';
    EXEC sys.sp_executesql @sql;
END
"""

PUBLISH_MARTS = """
CREATE OR ALTER PROCEDURE ops.publish_marts @manifest nvarchar(max), @snapshot nvarchar(200), @run_id nvarchar(100)
WITH EXECUTE AS OWNER
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;
    DECLARE @m TABLE (table_name sysname NOT NULL PRIMARY KEY, row_count bigint NOT NULL);
    INSERT @m (table_name, row_count)
        SELECT table_name, row_count
        FROM OPENJSON(@manifest) WITH (table_name sysname '$.table_name', row_count bigint '$.row_count');
    IF EXISTS (SELECT table_name FROM @m EXCEPT SELECT table_name FROM ops.marts_tables)
       OR EXISTS (SELECT table_name FROM ops.marts_tables EXCEPT SELECT table_name FROM @m)
        THROW 50002, N'publish_marts: the manifest tables are not exactly ops.marts_tables', 1;

    DECLARE @t sysname, @expected bigint, @actual bigint, @rows bigint = 0, @sql nvarchar(max), @msg nvarchar(400);
    DECLARE counts CURSOR LOCAL FAST_FORWARD FOR SELECT table_name, row_count FROM @m ORDER BY table_name;
    OPEN counts;
    FETCH NEXT FROM counts INTO @t, @expected;
    WHILE @@FETCH_STATUS = 0
    BEGIN
        SET @sql = N'SELECT @n = COUNT_BIG(*) FROM stage.' + QUOTENAME(@t) + N';';
        EXEC sys.sp_executesql @sql, N'@n bigint OUTPUT', @n = @actual OUTPUT;
        IF @actual <> @expected
        BEGIN
            SET @msg = CONCAT(N'publish_marts: stage.', @t, N' has ', @actual, N' rows, the manifest says ', @expected);
            THROW 50003, @msg, 1;
        END;
        SET @rows += @actual;
        FETCH NEXT FROM counts INTO @t, @expected;
    END;
    CLOSE counts;
    DEALLOCATE counts;

    BEGIN TRANSACTION;
    DECLARE swap CURSOR LOCAL FAST_FORWARD FOR SELECT table_name FROM @m ORDER BY table_name;
    OPEN swap;
    FETCH NEXT FROM swap INTO @t;
    WHILE @@FETCH_STATUS = 0
    BEGIN
        SET @sql = N'TRUNCATE TABLE marts.' + QUOTENAME(@t) + N'; '
                 + N'INSERT INTO marts.' + QUOTENAME(@t) + N' SELECT * FROM stage.' + QUOTENAME(@t) + N';';
        EXEC sys.sp_executesql @sql;
        FETCH NEXT FROM swap INTO @t;
    END;
    CLOSE swap;
    DEALLOCATE swap;
    INSERT ops.load_log (snapshot, run_id, tables, row_count, published_at_utc)
        VALUES (@snapshot, @run_id, (SELECT COUNT(*) FROM @m), @rows, SYSUTCDATETIME());
    COMMIT TRANSACTION;
END
"""


def tsql_type(duckdb_type: str) -> str:
    t = TSQL_TYPES.get(duckdb_type.upper())
    if t is None:
        raise SystemExit(f"ABORT: DuckDB 类型 {duckdb_type} 没有约定的 T-SQL 对应，先在 TSQL_TYPES 里定下来")
    return t


def duckdb_columns(db_path: Path) -> dict[str, list[tuple[str, str]]]:
    """白名单里每张 marts 表的 (列名, T-SQL 类型)，按列顺序。"""
    import duckdb

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = con.execute(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'marts' ORDER BY table_name, ordinal_position").fetchall()
    finally:
        con.close()
    cols: dict[str, list[tuple[str, str]]] = {t: [] for t in EXPORT.MARTS_EXPORT_TABLES}
    for table, column, dtype in rows:
        if table in cols:
            cols[table].append((column, tsql_type(dtype)))
    missing = [t for t, c in cols.items() if not c]
    if missing:
        raise SystemExit(f"ABORT: DuckDB 的 marts 里没有 {missing}")
    return cols


def create_table_sql(schema: str, table: str, columns: list[tuple[str, str]]) -> str:
    body = ",\n    ".join(f"[{name}] {typ} NULL" for name, typ in columns)
    return f"CREATE TABLE {schema}.[{table}] (\n    {body}\n);"


def _existing_columns(cur, schema: str, table: str) -> list[tuple[str, str]]:
    cur.execute(
        "SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, DATETIME_PRECISION FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? ORDER BY ORDINAL_POSITION", schema, table)
    out = []
    for name, dtype, max_len, dt_precision in cur.fetchall():
        if dtype == "nvarchar":
            dtype = "nvarchar(max)" if max_len == -1 else f"nvarchar({max_len})"
        elif dtype == "datetime2":
            dtype = f"datetime2({dt_precision})"
        out.append((name, dtype))
    return out


def adf_principal() -> tuple[str, str]:
    """(Data Factory 名, 它托管身份的 appId)。两者都是标识，只在内存里用。"""
    name = azsql.tf_output("data_factory_name")
    sub = azsql.tf_output("subscription_id")
    url = (f"https://management.azure.com/subscriptions/{sub}/resourceGroups/quantai/providers/"
           f"Microsoft.DataFactory/factories/{name}?api-version=2018-06-01")
    az = azsql._az()
    factory = json.loads(subprocess.run([az, "rest", "--method", "get", "--url", url, "--output", "json"],
                                        capture_output=True, text=True, check=True).stdout)
    principal_id = factory["identity"]["principalId"]
    app_id = subprocess.run([az, "ad", "sp", "show", "--id", principal_id, "--query", "appId", "--output", "tsv"],
                            capture_output=True, text=True, check=True).stdout.strip()
    return name, app_id


def setup(cur, columns: dict[str, list[tuple[str, str]]], recreate: bool) -> None:
    for schema in ("stage", "marts", "ops"):
        cur.execute(f"IF SCHEMA_ID(N'{schema}') IS NULL EXEC(N'CREATE SCHEMA {schema}');")
    cur.execute(OPS_DDL)
    for table, cols in columns.items():
        for schema in ("stage", "marts"):
            have = _existing_columns(cur, schema, table)
            if have == cols:
                print(f"[setup] {schema}.{table:<22} 已存在，结构一致")
                continue
            if have and not recreate:
                raise SystemExit(f"ABORT: {schema}.{table} 的结构和 DuckDB 不一致；确认后加 --recreate 重建")
            if have:
                cur.execute(f"DROP TABLE {schema}.[{table}];")
            cur.execute(create_table_sql(schema, table, cols))
            print(f"[setup] {schema}.{table:<22} {'重建' if have else '新建'}，{len(cols)} 列")
    cur.execute("DELETE FROM ops.marts_tables;")
    for table in columns:
        cur.execute("INSERT INTO ops.marts_tables (table_name) VALUES (?);", table)
    cur.execute(TRUNCATE_STAGE)
    cur.execute(PUBLISH_MARTS)
    print(f"[setup] ops.marts_tables {len(columns)} 行；ops.truncate_stage、ops.publish_marts 已建")


def grant_data_factory(cur, name: str, app_id: str) -> None:
    sid = "0x" + uuid.UUID(app_id).bytes_le.hex().upper()
    cur.execute("SELECT COUNT(*) FROM sys.database_principals WHERE name = ?;", name)
    if cur.fetchone()[0] == 0:
        cur.execute(f"CREATE USER [{name}] WITH SID = {sid}, TYPE = E;")
        print("[setup] Data Factory 托管身份已建成数据库用户")
    else:
        print("[setup] Data Factory 托管身份的数据库用户已存在")
    cur.execute(f"GRANT SELECT, INSERT ON SCHEMA::stage TO [{name}];")
    cur.execute(f"GRANT EXECUTE ON SCHEMA::ops TO [{name}];")
    cur.execute(
        "SELECT p.permission_name, s.name FROM sys.database_permissions p "
        "JOIN sys.database_principals u ON u.principal_id = p.grantee_principal_id "
        "LEFT JOIN sys.schemas s ON p.class = 3 AND s.schema_id = p.major_id WHERE u.name = ? "
        "ORDER BY 2, 1;", name)
    print("[setup] Data Factory 的权限：", [f"{perm} ON {schema}" for perm, schema in cur.fetchall()])


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--duckdb", type=Path, required=True, help="云侧 DuckDB，用来取 marts 的列和类型")
    p.add_argument("--recreate", action="store_true", help="结构不符时删掉重建 stage 和 marts 的表")
    args = p.parse_args(argv)
    columns = duckdb_columns(args.duckdb)
    name = ""
    try:
        name, app_id = adf_principal()
        con = azsql.connect()
        try:
            cur = con.cursor()
            setup(cur, columns, args.recreate)
            grant_data_factory(cur, name, app_id)
        finally:
            con.close()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - 报错信息打码后再抛
        raise SystemExit(f"[setup] 失败：{azsql.mask(exc, name)}") from None
    print(f"[setup] 完成：{len(columns)} 张表 x 2 个 schema")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
