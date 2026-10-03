terraform {
  required_version = ">= 1.7.0, < 2.0.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region              = var.region
  allowed_account_ids = [var.lab_account_id]
}

data "aws_iam_role" "execution" {
  name = var.execution_role_name
}

# Both buckets already exist. This stack creates neither bucket nor IAM roles.
data "aws_s3_bucket" "offline" {
  bucket = var.offline_store_bucket
}

locals {
  feature_group = "${var.project_prefix}-features-${var.feature_version}"
  package_group = "${var.project_prefix}-models"
  pipeline_name = "${var.project_prefix}-pipeline"
  code_prefix   = "pipeline/code"
  code_files = {
    "preprocess.py" = "${path.module}/preprocess.py"
    "process.py"    = "${path.module}/process.py"
    "ingest.py"     = "${path.module}/ingest.py"
    "evaluate.py"   = "${path.module}/evaluate.py"
    "evaluation_metrics.py" = "${path.module}/evaluation_metrics.py"
    "manifest.py"   = "${path.module}/manifest.py"
    "manifest_config.py" = "${path.module}/manifest_config.py"
    "stage_manifest.py" = "${path.module}/stage_manifest.py"
  }
}

resource "aws_s3_object" "code" {
  for_each               = local.code_files
  bucket                 = var.output_bucket
  key                    = "${local.code_prefix}/${each.key}"
  source                 = each.value
  etag                   = filemd5(each.value)
  server_side_encryption = "AES256"
}

resource "aws_sagemaker_feature_group" "features" {
  feature_group_name             = local.feature_group
  record_identifier_feature_name = "customerid"
  event_time_feature_name        = "event_time"
  role_arn                       = data.aws_iam_role.execution.arn

  dynamic "feature_definition" {
    for_each = jsondecode(file("${path.module}/schema.json"))
    content {
      feature_name = feature_definition.value.FeatureName
      feature_type = feature_definition.value.FeatureType
    }
  }

  offline_store_config {
    disable_glue_table_creation = false
    s3_storage_config {
      s3_uri = "s3://${data.aws_s3_bucket.offline.id}/features/${var.project_prefix}/cell2cell/${var.feature_version}"
    }
  }
  # Online store is omitted: this project uses batch data.
}

resource "aws_sagemaker_model_package_group" "models" {
  model_package_group_name = local.package_group
}

resource "aws_sagemaker_pipeline" "churn" {
  pipeline_name         = local.pipeline_name
  pipeline_display_name = local.pipeline_name
  role_arn              = data.aws_iam_role.execution.arn
  pipeline_definition   = jsonencode(local.definition)
  depends_on            = [aws_s3_object.code]
}

output "pipeline_name" {
  value = aws_sagemaker_pipeline.churn.pipeline_name
}
output "feature_group" {
  value = aws_sagemaker_feature_group.features.feature_group_name
}
output "model_package_group" {
  value = aws_sagemaker_model_package_group.models.model_package_group_name
}
