resource "aws_kms_key" "secrets" {
  description             = "KMS CMK for ${local.name_prefix} Secrets Manager."
  deletion_window_in_days = 30
  enable_key_rotation     = true

  tags = local.tags
}

resource "aws_kms_alias" "secrets" {
  name          = "alias/${local.name_prefix}-secrets"
  target_key_id = aws_kms_key.secrets.key_id
}

resource "aws_secretsmanager_secret" "openai_api_key" {
  name                    = "${var.project}/${var.environment}/openai-api-key"
  description             = "OpenAI API key for the GridWise LLM service."
  kms_key_id              = aws_kms_key.secrets.arn
  recovery_window_in_days = var.environment == "prod" ? 30 : 7

  tags = local.tags
}

# Empty initial value — populated by the deploy workflow or `aws secretsmanager put-secret-value`.
# We deliberately do NOT take a plaintext value through Terraform state.
resource "aws_secretsmanager_secret_version" "openai_api_key" {
  count = var.openai_api_key_secret_arn == "" ? 1 : 0

  secret_id = aws_secretsmanager_secret.openai_api_key.id
  secret_string = jsonencode({
    placeholder = "replace-via-deploy-workflow"
  })
}

locals {
  openai_secret_arn = var.openai_api_key_secret_arn != "" ? var.openai_api_key_secret_arn : aws_secretsmanager_secret.openai_api_key.arn
}
