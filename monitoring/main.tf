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
  dimensions = { ModelName = var.project_prefix, Source = "live" }
  alarms = {
    drift          = { metric = "MaxFeaturePSI", threshold = var.monitoring_psi_threshold, comparison = "GreaterThanOrEqualToThreshold", stat = "Maximum", period = 60, periods = 1, missing = "notBreaching" }
    quality        = { metric = "F2", threshold = var.monitoring_f2_floor, comparison = "LessThanThreshold", stat = "Minimum", period = 60, periods = 1, missing = "notBreaching" }
    bias           = { metric = "FourFifthsBreach", threshold = 1, comparison = "GreaterThanOrEqualToThreshold", stat = "Maximum", period = 60, periods = 1, missing = "notBreaching" }
    nobody-flagged = { metric = "NobodyFlagged", threshold = 1, comparison = "GreaterThanOrEqualToThreshold", stat = "Maximum", period = 60, periods = 1, missing = "notBreaching" }
    stale          = { metric = "JobSucceeded", threshold = 1, comparison = "LessThanThreshold", stat = "Sum", period = 86400, periods = var.monitoring_stale_days, missing = "breaching" }
  }
  charts = [
    { title = "Feature drift - max PSI", metric = "MaxFeaturePSI" },
    { title = "Outreach flag rate", metric = "FlagRate" },
    { title = "Model quality - F2", metric = "F2" },
    { title = "Credit-rating selection ratio", metric = "DI_CreditRating" },
    { title = "Label coverage", metric = "LabelCoverage" },
    { title = "Quality assessed", metric = "QualityAssessed" },
    { title = "Bias assessed", metric = "BiasAssessed" },
    { title = "Customers scored", metric = "RowsScored" }
  ]
}
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
  dashboard_body = jsonencode({ widgets = concat(
    [{ type = "text", x = 0, y = 0, width = 24, height = 3, properties = {
      markdown = "# Customer churn monitoring\nLive holdout and labelled replay are separate series. Missing quality metrics mean labels were unavailable, not that quality passed. Alarms use live metrics only. PSI alarm: ${var.monitoring_psi_threshold}; provisional F2 floor: ${var.monitoring_f2_floor}; bias needs ${var.monitoring_min_flagged} flagged records. Investigate signals before deciding to retrain."
    } }],
    [for i, chart in local.charts : { type = "metric", x = (i % 2) * 12, y = 3 + floor(i / 2) * 6, width = 12, height = 6, properties = {
      title   = chart.title, region = var.region, view = "timeSeries", stat = "Average", period = 60,
      metrics = [for source in ["live", "replay"] : [local.namespace, chart.metric, "ModelName", var.project_prefix, "Source", source, { label = source }]]
    } }],
    [{ type = "alarm", x = 0, y = 27, width = 24, height = 3, properties = {
      title = "Live monitoring alarms", alarms = [for alarm in aws_cloudwatch_metric_alarm.monitoring : alarm.arn]
    } }]
  ) })
}
output "dashboard_name" { value = aws_cloudwatch_dashboard.monitoring.dashboard_name }
