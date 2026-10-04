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
locals {
  namespace  = "${var.project_prefix}/ChurnMonitoring"
  dimensions = { ModelName = var.project_prefix }
  alarms = {
    drift          = { metric = "MaxFeaturePSI", threshold = var.monitoring_psi_threshold, comparison = "GreaterThanOrEqualToThreshold", stat = "Maximum", period = 60, periods = 1, missing = "notBreaching" }
    quality        = { metric = "F2", threshold = var.monitoring_f2_floor, comparison = "LessThanThreshold", stat = "Minimum", period = 60, periods = 1, missing = "notBreaching" }
    bias           = { metric = "FourFifthsBreach", threshold = 1, comparison = "GreaterThanOrEqualToThreshold", stat = "Maximum", period = 60, periods = 1, missing = "notBreaching" }
    nobody-flagged = { metric = "NobodyFlagged", threshold = 1, comparison = "GreaterThanOrEqualToThreshold", stat = "Maximum", period = 60, periods = 1, missing = "notBreaching" }
    stale          = { metric = "JobSucceeded", threshold = 1, comparison = "LessThanThreshold", stat = "Sum", period = 86400, periods = var.monitoring_stale_days, missing = "breaching" }
  }
}

data "aws_caller_identity" "current" {}

resource "aws_cloudwatch_metric_alarm" "monitoring" {
  for_each            = local.alarms
  alarm_name          = "${var.project_prefix}-${each.key}"
  namespace           = local.namespace
  metric_name         = each.value.metric
  dimensions          = local.dimensions
  statistic           = each.value.stat
  period              = each.value.period
  evaluation_periods  = each.value.periods
  datapoints_to_alarm = each.value.periods
  threshold           = each.value.threshold
  comparison_operator = each.value.comparison
  treat_missing_data  = each.value.missing
}
resource "aws_cloudwatch_dashboard" "monitoring" {
  dashboard_name = "${var.project_prefix}-monitoring"
  dashboard_body = templatefile("${path.module}/dashboard.json.tftpl", {
    region    = var.region
    namespace = local.namespace
    model     = var.project_prefix
    psi       = var.monitoring_psi_threshold
    f2_pct    = format("%.2f", var.monitoring_f2_floor * 100)
    min_flag  = var.monitoring_min_flagged
    account   = data.aws_caller_identity.current.account_id
  })
}

output "dashboard_name" { value = aws_cloudwatch_dashboard.monitoring.dashboard_name }
