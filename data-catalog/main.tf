terraform {
  required_version = ">= 1.7.0, < 2.0.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

# AWS_PROFILE comes from the exported .env, just as it does for Python.
provider "aws" {
  region              = var.region
  allowed_account_ids = [var.lab_account_id]
}

locals {
  database = replace(var.project_prefix, "-", "_")
  raw_columns = [
    "customerid",
    "churn",
    "monthlyrevenue",
    "monthlyminutes",
    "totalrecurringcharge",
    "directorassistedcalls",
    "overageminutes",
    "roamingcalls",
    "percchangeminutes",
    "percchangerevenues",
    "droppedcalls",
    "blockedcalls",
    "unansweredcalls",
    "customercarecalls",
    "threewaycalls",
    "receivedcalls",
    "outboundcalls",
    "inboundcalls",
    "peakcallsinout",
    "offpeakcallsinout",
    "droppedblockedcalls",
    "callforwardingcalls",
    "callwaitingcalls",
    "monthsinservice",
    "uniquesubs",
    "activesubs",
    "servicearea",
    "handsets",
    "handsetmodels",
    "currentequipmentdays",
    "agehh1",
    "agehh2",
    "childreninhh",
    "handsetrefurbished",
    "handsetwebcapable",
    "truckowner",
    "rvowner",
    "homeownership",
    "buysviamailorder",
    "respondstomailoffers",
    "optoutmailings",
    "nonustravel",
    "ownscomputer",
    "hascreditcard",
    "retentioncalls",
    "retentionoffersaccepted",
    "newcellphoneuser",
    "notnewcellphoneuser",
    "referralsmadebysubscriber",
    "incomegroup",
    "ownsmotorcycle",
    "adjustmentstocreditrating",
    "handsetprice",
    "madecalltoretentionteam",
    "creditrating",
    "prizmcode",
    "occupation",
    "maritalstatus",
  ]
}

resource "aws_glue_catalog_database" "catalog" {
  name = local.database
}

resource "aws_glue_catalog_table" "raw" {
  name          = "cell2cell_raw"
  database_name = aws_glue_catalog_database.catalog.name
  table_type    = "EXTERNAL_TABLE"
  parameters = {
    EXTERNAL                 = "TRUE"
    classification           = "csv"
    "skip.header.line.count" = "1"
  }
  storage_descriptor {
    location      = var.raw_s3_uri
    input_format  = "org.apache.hadoop.mapred.TextInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat"
    ser_de_info {
      serialization_library = "org.apache.hadoop.hive.serde2.OpenCSVSerde"
      parameters = {
        separatorChar = ","
        quoteChar     = "\""
        escapeChar    = "\\"
      }
    }
    # CSV column order must match the source files. SQL performs type conversion.
    dynamic "columns" {
      for_each = local.raw_columns
      content {
        name = columns.value
        type = "string"
      }
    }
  }
}

resource "aws_athena_workgroup" "catalog" {
  name = var.project_prefix
  configuration {
    # CTAS specifies its own Parquet destination; enforcing a result location
    # would prevent Athena from accepting that external_location.
    enforce_workgroup_configuration = false
    result_configuration {
      output_location = "s3://${var.output_bucket}/athena-results/"
      encryption_configuration {
        encryption_option = "SSE_S3"
      }
    }
  }
}

output "database" {
  value = aws_glue_catalog_database.catalog.name
}
output "workgroup" {
  value = aws_athena_workgroup.catalog.name
}
output "curated_location" {
  value = "s3://${var.output_bucket}/curated/cell2cell/${var.curated_version}/"
}
