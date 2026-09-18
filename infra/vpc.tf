module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.10"

  name = local.name_prefix
  cidr = var.vpc_cidr

  azs             = local.azs
  public_subnets  = [for i, _ in local.azs : cidrsubnet(var.vpc_cidr, 8, 10 + i)]
  private_subnets = [for i, _ in local.azs : cidrsubnet(var.vpc_cidr, 8, 20 + i)]

  enable_nat_gateway     = true
  single_nat_gateway     = false # one NAT per AZ for HA
  one_nat_gateway_per_az = true
  enable_dns_hostnames   = true
  enable_dns_support     = true

  # Gateway endpoints: free and keep ECR image pulls / CloudWatch logs off NAT.
  enable_s3_endpoint  = true
  enable_dynamodb_endpoint = true

  # Interface endpoints: ECS tasks should not need a NAT hop for AWS APIs.
  # Subset of what ECS/ECR/Secrets Manager/CloudWatch need.
  enable_interfaces_vpc_endpoints = true
  interfaces_vpc_endpoints = {
    ecr.api        = { service_name = "com.amazonaws.${var.region}.ecr.api" }
    ecr.dkr        = { service_name = "com.amazonaws.${var.region}.ecr.dkr" }
    ecs            = { service_name = "com.amazonaws.${var.region}.ecs" }
    ecs-telemetry  = { service_name = "com.amazonaws.${var.region}.ecs-telemetry" }
    logs           = { service_name = "com.amazonaws.${var.region}.logs" }
    secretsmanager = { service_name = "com.amazonaws.${var.region}.secretsmanager" }
    monitoring     = { service_name = "com.amazonaws.${var.region}.monitoring" }
  }

  public_subnet_tags = {
    "kubernetes.io/role/elb" = 1
  }
  private_subnet_tags = {
    "kubernetes.io/role/internal-elb" = 1
  }

  tags = local.tags
}
