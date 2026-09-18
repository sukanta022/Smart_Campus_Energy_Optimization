resource "aws_security_group" "alb" {
  name        = "${local.name_prefix}-alb"
  description = "Public ALB ingress."
  vpc_id      = module.vpc.vpc_id

  tags = merge(local.tags, { Name = "${local.name_prefix}-alb" })
}

resource "aws_vpc_security_group_ingress_rule" "alb_http" {
  count = length(var.allowed_cidrs)

  security_group_id = aws_security_group.alb.id
  description       = "Public HTTP from ${var.allowed_cidrs[count.index]}"
  cidr_ipv4         = var.allowed_cidrs[count.index]
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "alb_https" {
  count = var.certificate_arn != "" ? length(var.allowed_cidrs) : 0

  security_group_id = aws_security_group.alb.id
  description       = "Public HTTPS from ${var.allowed_cidrs[count.index]}"
  cidr_ipv4         = var.allowed_cidrs[count.index]
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "alb_egress" {
  security_group_id = aws_security_group.alb.id
  description       = "ALB → app tasks (all ports)."
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_security_group" "task" {
  name        = "${local.name_prefix}-task"
  description = "Fargate task ingress."
  vpc_id      = module.vpc.vpc_id

  tags = merge(local.tags, { Name = "${local.name_prefix}-task" })
}

resource "aws_vpc_security_group_ingress_rule" "task_from_alb" {
  security_group_id            = aws_security_group.task.id
  description                  = "App port from ALB only."
  referenced_security_group_id = aws_security_group.alb.id
  from_port                    = var.container_port
  to_port                      = var.container_port
  ip_protocol                  = "tcp"
}

# Tasks only need egress to OpenAI, Redis, AWS APIs (covered by VPC endpoints + NAT).
resource "aws_vpc_security_group_egress_rule" "task_https" {
  security_group_id = aws_security_group.task.id
  description       = "Outbound HTTPS for OpenAI + AWS APIs."
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_security_group" "redis" {
  name        = "${local.name_prefix}-redis"
  description = "ElastiCache Redis ingress."
  vpc_id      = module.vpc.vpc_id

  tags = merge(local.tags, { Name = "${local.name_prefix}-redis" })
}

resource "aws_vpc_security_group_ingress_rule" "redis_from_task" {
  security_group_id            = aws_security_group.redis.id
  description                  = "Redis (6379) from tasks only."
  referenced_security_group_id = aws_security_group.task.id
  from_port                    = 6379
  to_port                      = 6379
  ip_protocol                  = "tcp"
}
