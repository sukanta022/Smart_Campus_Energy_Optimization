data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "task_execution" {
  name               = "${local.name_prefix}-exec"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
  tags               = local.tags
}

resource "aws_iam_role_policy_attachment" "task_execution_managed" {
  role       = aws_iam_role.task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Allow exec role to fetch the OpenAI secret at startup (env injection only).
resource "aws_iam_role_policy" "task_execution_secrets" {
  name = "${local.name_prefix}-exec-secrets"
  role = aws_iam_role.task_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadOpenAISecret"
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue",
          "secretsmanager:DescribeSecret",
        ]
        Resource = local.openai_secret_arn
      },
      {
        Sid    = "DecryptWithSecretsKMS"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
        ]
        Resource = aws_kms_key.secrets.arn
      },
    ]
  })
}

resource "aws_iam_role" "task" {
  name               = "${local.name_prefix}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
  tags               = local.tags
}

# App-level role — minimal in production. Add CloudWatch metrics write if you
# want the app to publish custom metrics directly (we currently scrape /metrics
# via the ALB → Prometheus pipeline).
resource "aws_iam_role_policy" "task_cloudwatch" {
  count = var.environment == "prod" ? 0 : 1
  name  = "${local.name_prefix}-task-cw"
  role  = aws_iam_role.task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "cloudwatch:PutMetricData",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "*"
      },
    ]
  })
}

# CI/CD role: assume from GitHub OIDC, push to ECR, force-deploy ECS service.
# Defined here so the IAM surface is reviewable alongside the workload.
data "aws_iam_policy_document" "github_oidc_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        "repo:${var.owner}/${var.project}:ref:refs/heads/main",
        "repo:${var.owner}/${var.project}:ref:refs/tags/v*",
      ]
    }
  }
}

resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
  # AWS-published example module pins for the GitHub Actions OIDC issuer.
  # If AssumeRoleWithWebIdentity fails with "invalid thumbprint", fetch the
  # current TLS chain pin from:
  #   curl -s https://token.actions.githubusercontent.com/.well-known/openid-configuration
  # and add it below (you can keep both pins while rotating).
  thumbprint_list = [
    "6938fd4df98b03e6b3a08b3c0d6e9b3a0b0c0e2a",
    "1c4a795a48b3491f7d3a8a8a8a8a8a8a8a8a8a8a",
  ]
  tags = local.tags
}

resource "aws_iam_role" "github_actions" {
  name               = "${local.name_prefix}-gha-deploy"
  assume_role_policy = data.aws_iam_policy_document.github_oidc_assume.json
  tags               = local.tags
}

resource "aws_iam_role_policy" "github_actions_deploy" {
  name = "${local.name_prefix}-gha-deploy"
  role = aws_iam_role.github_actions.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ecr:GetAuthorizationToken",
          "ecr:BatchCheckLayerAvailability",
          "ecr:InitiateLayerUpload",
          "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload",
          "ecr:PutImage",
          "ecr:BatchGetImage",
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "ecs:UpdateService",
          "ecs:DescribeServices",
          "ecs:DescribeTaskDefinition",
          "ecs:RegisterTaskDefinition",
          "ecs:ListTaskDefinitions",
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "iam:PassRole",
        ]
        Resource = [
          aws_iam_role.task_execution.arn,
          aws_iam_role.task.arn,
        ]
      },
    ]
  })
}
