resource "aws_wafv2_web_acl" "gridwise" {
  name        = local.name_prefix
  scope       = "REGIONAL"
  description = "GridWise ALB WAF v2 ACL."

  default_action {
    allow {}
  }

  # AWS managed core rule set — protect against OWASP Top 10.
  rule {
    name     = "AWSManagedRulesCommonRuleSet"
    priority = 10
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesCommonRuleSet"
        vendor_name = "AWS"
      }
    }
    visibility_config {
      sampled_requests_enabled   = true
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.project}-common"
    }
  }

  # Known-bad inputs.
  rule {
    name     = "AWSManagedRulesKnownBadInputsRuleSet"
    priority = 20
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesKnownBadInputsRuleSet"
        vendor_name = "AWS"
      }
    }
    visibility_config {
      sampled_requests_enabled   = true
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.project}-known-bad"
    }
  }

  # Per-IP rate limit at the edge — 1500 req / 5 min (≈5 RPS steady, accommodates bursts).
  rule {
    name     = "RateLimitPerIP"
    priority = 30
    action {
      block {}
    }
    statement {
      rate_based_statement {
        limit              = 1500
        aggregate_key_type = "IP"
      }
    }
    visibility_config {
      sampled_requests_enabled   = true
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.project}-ratelimit"
    }
  }

  visibility_config {
    sampled_requests_enabled   = true
    cloudwatch_metrics_enabled = true
    metric_name                = local.name_prefix
  }

  tags = local.tags
}

resource "aws_wafv2_web_acl_association" "gridwise" {
  resource_arn = aws_lb.gridwise.arn
  web_acl_arn  = aws_wafv2_web_acl.gridwise.arn
}
