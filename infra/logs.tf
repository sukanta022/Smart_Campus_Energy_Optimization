resource "aws_cloudwatch_log_group" "gridwise" {
  name              = "/ecs/${local.name_prefix}"
  retention_in_days = var.environment == "prod" ? 30 : 7
  kms_key_id        = aws_kms_key.secrets.arn # reuse; cheaper than a per-resource CMK

  tags = local.tags
}

# Metric filter: count 5xx responses by scanning the JSON access logs.
resource "aws_cloudwatch_log_metric_filter" "five_xx" {
  name           = "${local.name_prefix}-5xx"
  log_group_name = aws_cloudwatch_log_group.gridwise.name
  pattern        = "{ $.status_code >= 500 }"

  metric_transformation {
    name      = "${var.project}-5xx"
    namespace = "GridWise"
    value     = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "five_xx" {
  alarm_name          = "${local.name_prefix}-5xx-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "${var.project}-5xx"
  namespace           = "GridWise"
  period              = 60
  statistic           = "Sum"
  threshold           = 10
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]

  tags = local.tags
}

resource "aws_sns_topic" "alerts" {
  name              = "${local.name_prefix}-alerts"
  kms_master_key_id = aws_kms_key.secrets.arn
  tags              = local.tags
}
