# Per-environment CMK so we can scope data-plane permissions in prod.
resource "aws_kms_key" "ecr" {
  description             = "KMS CMK for ${local.name_prefix} ECR repository encryption."
  deletion_window_in_days = 30
  enable_key_rotation     = true

  tags = local.tags
}

resource "aws_kms_alias" "ecr" {
  name          = "alias/${local.name_prefix}-ecr"
  target_key_id = aws_kms_key.ecr.key_id
}

resource "aws_ecr_repository" "gridwise" {
  name                 = var.project
  image_tag_mutability = "MUTABLE" # CI deploys use immutable digests; mutable for dev

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.ecr.arn
  }

  tags = local.tags
}

# Keep the last 30 tagged deploys plus untagged-but-most-recent for fast rollback.
resource "aws_ecr_lifecycle_policy" "gridwise" {
  repository = aws_ecr_repository.gridwise.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images after 7 days"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 7
        }
        action = {
          type = "expire"
        }
      },
      {
        rulePriority = 2
        description  = "Keep last 30 tagged release images"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["release-"]
          countType     = "imageCountMoreThan"
          countNumber   = 30
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}
