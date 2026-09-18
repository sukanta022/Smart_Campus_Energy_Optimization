resource "aws_kms_key" "redis" {
  description             = "KMS CMK for ${local.name_prefix} ElastiCache at-rest encryption."
  deletion_window_in_days = 30
  enable_key_rotation     = true
  tags                    = local.tags
}

resource "aws_elasticache_subnet_group" "gridwise" {
  name       = local.name_prefix
  subnet_ids = module.vpc.private_subnet_ids
  tags       = local.tags
}

resource "aws_elasticache_parameter_group" "gridwise" {
  name        = local.name_prefix
  family      = "redis7"
  description = "GridWise Redis params."

  parameter {
    name  = "maxmemory-policy"
    value = "allkeys-lru"
  }

  tags = local.tags
}

resource "aws_elasticache_replication_group" "gridwise" {
  replication_group_id       = replace(local.name_prefix, "-", "")
  replication_group_description = "GridWise rate-limit + breaker state"
  engine                     = "redis"
  engine_version             = "7.1"
  node_type                  = var.redis_node_type
  number_cache_clusters      = 2 # primary + 1 replica (1 per AZ)
  port                       = 6379

  subnet_group_name          = aws_elasticache_subnet_group.gridwise.name
  security_group_ids         = [aws_security_group.redis.id]
  parameter_group_name       = aws_elasticache_parameter_group.gridwise.name

  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  kms_key_id                 = aws_kms_key.redis.arn

  automatic_failover_enabled = true
  multi_az_enabled           = true

  snapshot_retention_limit = 3
  snapshot_window          = "03:00-05:00"
  maintenance_window       = "sun:05:00-sun:07:00"

  tags = local.tags
}
