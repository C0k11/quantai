# Data Factory：landing 里的一份 marts 快照 -> Azure SQL。
#
# 触发：GitHub Actions 最后写 landing/marts/<快照>/_manifest.csv，存储事件触发器看到它才启动管道，
# 所以管道开跑时这一份快照的 Parquet 一定已经写完（不靠猜时间）。
# 管道 load_marts：
#   1. 读 _manifest.csv（表名、行数）；
#   2. 每张表复制进 stage.<表>（复制前 ops.truncate_stage 清空），最多 4 张并行；
#   3. ops.publish_marts 核对 stage 行数与清单一致，在一个事务里整体替换 marts，并写 ops.load_log。
# 任何一步失败，管道就是失败，alerts.tf 的告警发邮件。
# 认证全部用 Data Factory 的托管身份：读 landing 靠 lake.tf 的 RBAC，连 SQL 靠数据库里的用户（sql_setup.py 建）。
#
# 无服务器数据库暂停后第一次连接要等它恢复，约一分钟，所以复制和存储过程都带重试。

resource "azurerm_data_factory" "quantai" {
  name                   = local.data_factory_name
  location               = azurerm_resource_group.quantai.location
  resource_group_name    = azurerm_resource_group.quantai.name
  public_network_enabled = true

  identity {
    type = "SystemAssigned"
  }

  tags = local.tags
}

resource "azurerm_data_factory_linked_service_data_lake_storage_gen2" "lake" {
  name                 = "ls_lake"
  data_factory_id      = azurerm_data_factory.quantai.id
  url                  = azurerm_storage_account.lake.primary_dfs_endpoint
  use_managed_identity = true
}

resource "azurerm_data_factory_linked_service_azure_sql_database" "sql" {
  name                 = "ls_sql"
  data_factory_id      = azurerm_data_factory.quantai.id
  connection_string    = "Data Source=${azurerm_mssql_server.quantai.fully_qualified_domain_name};Initial Catalog=${local.sql_database_name};Encrypt=True;Connection Timeout=60"
  use_managed_identity = true
}

resource "azurerm_data_factory_dataset_delimited_text" "manifest" {
  name                = "ds_manifest"
  data_factory_id     = azurerm_data_factory.quantai.id
  linked_service_name = azurerm_data_factory_linked_service_data_lake_storage_gen2.lake.name
  parameters          = { folder = "" }
  first_row_as_header = true
  encoding            = "UTF-8"
  # 分隔符要写明：不写时 azurerm 存成空的行分隔符，Data Factory 读清单时报
  # "Column Delimiter must be empty string when Row Delimiter is empty"（第一次手动装载实测）。
  column_delimiter = ","
  row_delimiter    = "\n"

  azure_blob_fs_location {
    file_system          = local.landing_container
    path                 = "@dataset().folder"
    dynamic_path_enabled = true
    filename             = "_manifest.csv"
  }
}

resource "azurerm_data_factory_dataset_parquet" "snapshot_table" {
  name                = "ds_snapshot_table"
  data_factory_id     = azurerm_data_factory.quantai.id
  linked_service_name = azurerm_data_factory_linked_service_data_lake_storage_gen2.lake.name
  parameters          = { folder = "", file = "" }
  compression_codec   = "snappy"

  azure_blob_fs_location {
    file_system              = local.landing_container
    path                     = "@dataset().folder"
    dynamic_path_enabled     = true
    filename                 = "@dataset().file"
    dynamic_filename_enabled = true
  }
}

resource "azurerm_data_factory_dataset_azure_sql_table" "stage_table" {
  name              = "ds_stage_table"
  data_factory_id   = azurerm_data_factory.quantai.id
  linked_service_id = azurerm_data_factory_linked_service_azure_sql_database.sql.id
  parameters        = { table = "" }
  schema            = "stage"
  table             = "@dataset().table"
}

locals {
  # 触发器给的是 landing/marts/<快照>，数据集的路径不带容器名：去掉第一段。
  folder_path     = "pipeline().parameters.folderPath"
  snapshot_folder = "@substring(${local.folder_path}, add(indexOf(${local.folder_path}, '/'), 1), sub(length(${local.folder_path}), add(indexOf(${local.folder_path}, '/'), 1)))"
}

resource "azurerm_data_factory_pipeline" "load_marts" {
  name            = "load_marts"
  data_factory_id = azurerm_data_factory.quantai.id
  description     = "Copy one marts snapshot from landing into stage, then publish it to marts in one transaction."
  concurrency     = 1
  parameters      = { folderPath = "" }
  variables       = { snapshotFolder = "" }

  activities_json = jsonencode([
    {
      name = "SnapshotFolder"
      type = "SetVariable"
      typeProperties = {
        variableName = "snapshotFolder"
        value        = local.snapshot_folder
      }
    },
    {
      name      = "ReadManifest"
      type      = "Lookup"
      dependsOn = [{ activity = "SnapshotFolder", dependencyConditions = ["Succeeded"] }]
      policy    = { timeout = "0.00:05:00", retry = 1, retryIntervalInSeconds = 30 }
      typeProperties = {
        source = {
          type          = "DelimitedTextSource"
          storeSettings = { type = "AzureBlobFSReadSettings", recursive = false }
          # Data Factory 存的时候会补上 compressionProperties = null；不写上它，每次 plan 都会报这个管道有改动。
          formatSettings = { type = "DelimitedTextReadSettings", compressionProperties = null }
        }
        dataset = {
          referenceName = azurerm_data_factory_dataset_delimited_text.manifest.name
          type          = "DatasetReference"
          parameters    = { folder = "@variables('snapshotFolder')" }
        }
        firstRowOnly = false
      }
    },
    {
      name      = "CopyEachTable"
      type      = "ForEach"
      dependsOn = [{ activity = "ReadManifest", dependencyConditions = ["Succeeded"] }]
      typeProperties = {
        items        = { value = "@activity('ReadManifest').output.value", type = "Expression" }
        isSequential = false
        batchCount   = 4
        activities = [
          {
            name   = "CopyToStage"
            type   = "Copy"
            policy = { timeout = "0.00:20:00", retry = 2, retryIntervalInSeconds = 60 }
            inputs = [{
              referenceName = azurerm_data_factory_dataset_parquet.snapshot_table.name
              type          = "DatasetReference"
              parameters = {
                folder = "@variables('snapshotFolder')"
                file   = "@concat(item().table_name, '.parquet')"
              }
            }]
            outputs = [{
              referenceName = azurerm_data_factory_dataset_azure_sql_table.stage_table.name
              type          = "DatasetReference"
              parameters    = { table = "@item().table_name" }
            }]
            typeProperties = {
              source = {
                type          = "ParquetSource"
                storeSettings = { type = "AzureBlobFSReadSettings", recursive = false }
              }
              sink = {
                type = "AzureSqlSink"
                # 不开 sqlWriterUseTableLock：几万行用不着表锁，数据库用户也就只要 INSERT，不要更多权限。
                preCopyScript = "@concat('EXEC ops.truncate_stage @table = N''', item().table_name, '''')"
                writeBehavior = "insert"
              }
              enableStaging        = false
              dataIntegrationUnits = 4
              translator = {
                type                   = "TabularTranslator"
                typeConversion         = true
                typeConversionSettings = { allowDataTruncation = false, treatBooleanAsNumber = false }
              }
            }
          }
        ]
      }
    },
    {
      name              = "Publish"
      type              = "SqlServerStoredProcedure"
      dependsOn         = [{ activity = "CopyEachTable", dependencyConditions = ["Succeeded"] }]
      policy            = { timeout = "0.00:10:00", retry = 1, retryIntervalInSeconds = 60 }
      linkedServiceName = { referenceName = azurerm_data_factory_linked_service_azure_sql_database.sql.name, type = "LinkedServiceReference" }
      typeProperties = {
        storedProcedureName = "ops.publish_marts"
        storedProcedureParameters = {
          manifest = { value = "@string(activity('ReadManifest').output.value)", type = "String" }
          snapshot = { value = "@last(split(pipeline().parameters.folderPath, '/'))", type = "String" }
          run_id   = { value = "@pipeline().RunId", type = "String" }
        }
      }
    },
  ])
}

resource "azurerm_data_factory_trigger_blob_event" "manifest_landed" {
  name                  = "on_manifest"
  data_factory_id       = azurerm_data_factory.quantai.id
  storage_account_id    = azurerm_storage_account.lake.id
  events                = ["Microsoft.Storage.BlobCreated"]
  blob_path_begins_with = "/${local.landing_container}/blobs/marts/"
  blob_path_ends_with   = "_manifest.csv"
  ignore_empty_blobs    = true
  activated             = true

  pipeline {
    name       = azurerm_data_factory_pipeline.load_marts.name
    parameters = { folderPath = "@triggerBody().folderPath" }
  }
}
