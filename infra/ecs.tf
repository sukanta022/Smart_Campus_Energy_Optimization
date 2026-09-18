resource "aws_ecs_cluster" "gridwise" {
  name = local.name_prefix

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = local.tags
}

resource "aws_ecs_cluster_capacity_providers" "gridwise" {
  cluster_name       = aws_ecs_cluster.gridwise.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
    base              = 3 # first 3 tasks on stable Fargate
  }
  default_capacity_provider_strategy {
    capacity_provider = "FARGATE_SPOT"
    weight            = 4 # burst on Spot for cost
  }
}

locals {
  image_uri = var.container_image != "" ? var.container_image : "${aws_ecr_repository.gridwise.repository_url}:latest"
}

resource "aws_ecs_task_definition" "gridwise" {
  family                   = local.name_prefix
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  execution_role_arn       = aws_iam_role.task_execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    cpu_architecture        = "X86_64"
    operating_system_family = "LINUX"
  }

  container_definitions = jsonencode([
    {
      name              = "gridwise"
      image             = local.image_uri
      essential         = true
      user              = "gridwise"
      readonlyRootFilesystem = true
      portMappings      = [{ containerPort = var.container_port, protocol = "tcp" }]
      environment = [
        { name = "GRIDWISE_ENV",  value = var.environment },
        { name = "GRIDWISE_LOG_LEVEL", value = "INFO" },
        { name = "GRIDWISE_RATE_LIMIT_PER_MINUTE", value = tostring(var.rate_limit_per_minute) },
        { name = "REDIS_URL", value = "redis://${aws_elasticache_replication_group.gridwise.primary_endpoint_address}:6379/0" },
      ]
      secrets = [
        {
          name      = "OPENAI_API_KEY"
          valueFrom = local.openai_secret_arn
        }
      ]
      linuxParameters = {
        capabilities = {
          drop = ["ALL"]
        }
      }
      healthCheck = {
        command     = ["CMD-SHELL", "curl -fsS http://127.0.0.1:${var.container_port}/health || exit 1"]
        interval    = 15
        timeout     = 3
        retries     = 3
        startPeriod = 10
      }
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.gridwise.name
          awslogs-region        = var.region
          awslogs-stream-prefix = "gridwise"
        }
      }
    }
  ])

  tags = local.tags
}

resource "aws_ecs_service" "gridwise" {
  name             = local.name_prefix
  cluster          = aws_ecs_cluster.gridwise.id
  task_definition  = aws_ecs_task_definition.gridwise.arn
  desired_count    = var.task_desired_count
  launch_type      = "FARGATE"
  platform_version = "1.4.0"

  enable_ecs_managed_tags = true
  propagate_tags          = "TASK_DEFINITION"

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  deployment_controller {
    type = "ECS"
  }

  network_configuration {
    subnets          = module.vpc.private_subnet_ids
    security_groups  = [aws_security_group.task.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.gridwise.arn
    container_name   = "gridwise"
    container_port   = var.container_port
  }

  lifecycle {
    ignore_changes = [desired_count, task_definition] # managed by ASG + deploy workflow
  }

  tags = local.tags
}

# ---------- Application Auto Scaling ----------

resource "aws_appautoscaling_target" "gridwise" {
  max_capacity       = var.task_max_count
  min_capacity       = var.task_min_count
  resource_id        = "service/${aws_ecs_cluster.gridwise.name}/${aws_ecs_service.gridwise.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

# Scale out on CPU above 60% (warm PuLP solves keep CPU low — 60% is aggressive but safe).
resource "aws_appautoscaling_policy" "cpu" {
  name               = "${local.name_prefix}-cpu"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.gridwise.resource_id
  scalable_dimension = aws_appautoscaling_target.gridwise.scalable_dimension
  service_namespace  = aws_appautoscaling_target.gridwise.service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    target_value       = 60
    scale_in_cooldown  = 60
    scale_out_cooldown = 30
  }
}

# Scale out on memory above 70%.
resource "aws_appautoscaling_policy" "memory" {
  name               = "${local.name_prefix}-memory"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.gridwise.resource_id
  scalable_dimension = aws_appautoscaling_target.gridwise.scalable_dimension
  service_namespace  = aws_appautoscaling_target.gridwise.service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageMemoryUtilization"
    }
    target_value       = 70
    scale_in_cooldown  = 60
    scale_out_cooldown = 30
  }
}

# Scale on ALB request count per target (≈ RPS per task). Step scaling with
# tight bounds to match our 8s p99 budget.
resource "aws_appautoscaling_policy" "requests" {
  name               = "${local.name_prefix}-alb-requests"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.gridwise.resource_id
  scalable_dimension = aws_appautoscaling_target.gridwise.scalable_dimension
  service_namespace  = aws_appautoscaling_target.gridwise.service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ALBRequestCountPerTarget"
      resource_label         = "${aws_lb.gridwise.arn_suffix}/${aws_lb_target_group.gridwise.arn_suffix}"
    }
    target_value       = 200 # req/task; tune from k6 results
    scale_in_cooldown  = 120
    scale_out_cooldown = 30
  }
}
