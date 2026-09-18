variable "project" {
  description = "Project name used in resource names and tags."
  type        = string
  default     = "gridwise"
}

variable "environment" {
  description = "Deployment environment (dev, staging, prod)."
  type        = string
  default     = "prod"
}

variable "owner" {
  description = "Owner/team tag value."
  type        = string
  default     = "platform"
}

variable "region" {
  description = "AWS region."
  type        = string
  default     = "ap-southeast-1"
}

variable "vpc_cidr" {
  description = "VPC CIDR block."
  type        = string
  default     = "10.40.0.0/16"
}

variable "container_port" {
  description = "Port the app listens on inside the container."
  type        = number
  default     = 8080
}

variable "container_image" {
  description = "ECR image URI (overridden by CI/CD or kept as latest pin)."
  type        = string
  default     = ""
}

variable "openai_api_key_secret_arn" {
  description = "ARN of OPENAI_API_KEY in Secrets Manager. Created by secrets.tf when empty."
  type        = string
  default     = ""
}

variable "task_cpu" {
  description = "Fargate task CPU units (1024 = 1 vCPU)."
  type        = number
  default     = 1024
}

variable "task_memory" {
  description = "Fargate task memory (MiB)."
  type        = number
  default     = 2048
}

variable "task_desired_count" {
  description = "Desired number of ECS tasks (autoscaler will move this)."
  type        = number
  default     = 3
}

variable "task_min_count" {
  description = "Autoscaling minimum tasks."
  type        = number
  default     = 3
}

variable "task_max_count" {
  description = "Autoscaling maximum tasks."
  type        = number
  default     = 100
}

variable "rate_limit_per_minute" {
  description = "Per-IP requests per minute."
  type        = number
  default     = 600
}

variable "domain_name" {
  description = "Optional custom domain for the ALB (creates A-record/alias when set)."
  type        = string
  default     = ""
}

variable "certificate_arn" {
  description = "ACM cert ARN for HTTPS listener when domain_name is provided."
  type        = string
  default     = ""
}

variable "allowed_cidrs" {
  description = "CIDRs allowed to hit the ALB. Restrict in prod; * for dev."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "redis_node_type" {
  description = "ElastiCache node type."
  type        = string
  default     = "cache.t4g.small"
}
