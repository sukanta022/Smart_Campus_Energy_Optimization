output "vpc_id" {
  description = "VPC ID."
  value       = module.vpc.vpc_id
}

output "public_subnet_ids" {
  description = "Public subnet IDs hosting the ALB."
  value       = module.vpc.public_subnet_ids
}

output "private_subnet_ids" {
  description = "Private subnet IDs hosting ECS tasks and Redis."
  value       = module.vpc.private_subnet_ids
}

output "ecr_repository_url" {
  description = "ECR repository URL — used by CI/CD to push images."
  value       = aws_ecr_repository.gridwise.repository_url
}

output "alb_dns_name" {
  description = "ALB DNS name to point a domain at (or hit directly for dev)."
  value       = aws_lb.gridwise.dns_name
}

output "alb_arn_suffix" {
  description = "ALB ARN suffix (resource ID) — used by CloudWatch metrics."
  value       = aws_lb.gridwise.arn_suffix
}

output "ecs_cluster_name" {
  description = "ECS cluster name — used by kubectl-style exec and CLI tooling."
  value       = aws_ecs_cluster.gridwise.name
}

output "ecs_service_name" {
  description = "ECS service name — used by deploy workflows."
  value       = aws_ecs_service.gridwise.name
}

output "redis_primary_endpoint" {
  description = "ElastiCache Redis primary endpoint (set as REDIS_URL env var)."
  value       = aws_elasticache_replication_group.gridwise.primary_endpoint_address
  sensitive   = true
}

output "openai_secret_arn" {
  description = "Secrets Manager ARN holding OPENAI_API_KEY."
  value       = aws_secretsmanager_secret.openai_api_key.arn
}

output "waf_web_acl_arn" {
  description = "WAF v2 WebACL ARN associated with the ALB."
  value       = aws_wafv2_web_acl.gridwise.arn
}
