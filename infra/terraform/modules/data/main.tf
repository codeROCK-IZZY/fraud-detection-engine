# PostgreSQL (RDS), the model-artifact bucket, and application secrets.

variable "name" { type = string }
variable "vpc_id" { type = string }
variable "private_subnet_ids" { type = list(string) }
variable "db_instance_class" { type = string }
variable "db_multi_az" { type = bool }

# ---------------------------------------------------------------- secrets
# Generated here, stored in Secrets Manager, injected into containers by ECS.
# They also exist in the Terraform state, which is why the state bucket must be
# encrypted and access-restricted (see docs/AWS_DEPLOYMENT.md).
resource "random_password" "db" {
  length  = 32
  special = false
}

resource "random_password" "jwt" {
  length  = 64
  special = false
}

# Key for pseudonymising device fingerprints and payee references (must stay stable).
resource "random_password" "pseudonym" {
  length  = 64
  special = false
}

resource "aws_secretsmanager_secret" "database_url" {
  name                    = "${var.name}/database-url"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "database_url" {
  secret_id     = aws_secretsmanager_secret.database_url.id
  secret_string = "postgresql+psycopg://fde:${random_password.db.result}@${aws_db_instance.this.address}:5432/fde?sslmode=require"
}

resource "aws_secretsmanager_secret" "jwt" {
  name                    = "${var.name}/jwt-secret"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "jwt" {
  secret_id     = aws_secretsmanager_secret.jwt.id
  secret_string = random_password.jwt.result
}

resource "aws_secretsmanager_secret" "pseudonym" {
  name                    = "${var.name}/pseudonym-secret"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "pseudonym" {
  secret_id     = aws_secretsmanager_secret.pseudonym.id
  secret_string = random_password.pseudonym.result
}

# ---------------------------------------------------------------- database
resource "aws_security_group" "db" {
  name        = "${var.name}-db"
  description = "PostgreSQL: ingress only from application tasks (rule added by the app module)"
  vpc_id      = var.vpc_id
}

resource "aws_db_subnet_group" "this" {
  name       = "${var.name}-db"
  subnet_ids = var.private_subnet_ids
}

resource "aws_db_parameter_group" "this" {
  name   = "${var.name}-pg16"
  family = "postgres16"
  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
  parameter {
    name  = "log_min_duration_statement"
    value = "1000" # log queries slower than 1 s
  }
}

resource "aws_db_instance" "this" {
  identifier                      = "${var.name}-db"
  engine                          = "postgres"
  engine_version                  = "16"
  instance_class                  = var.db_instance_class
  allocated_storage               = 20
  max_allocated_storage           = 100
  storage_type                    = "gp3"
  storage_encrypted               = true
  db_name                         = "fde"
  username                        = "fde"
  password                        = random_password.db.result
  db_subnet_group_name            = aws_db_subnet_group.this.name
  vpc_security_group_ids          = [aws_security_group.db.id]
  parameter_group_name            = aws_db_parameter_group.this.name
  multi_az                        = var.db_multi_az
  publicly_accessible             = false
  backup_retention_period         = 7
  backup_window                   = "03:00-04:00"
  maintenance_window              = "sun:04:30-sun:05:30"
  auto_minor_version_upgrade      = true
  deletion_protection             = true
  skip_final_snapshot             = false
  final_snapshot_identifier       = "${var.name}-db-final"
  copy_tags_to_snapshot           = true
  performance_insights_enabled    = true
  enabled_cloudwatch_logs_exports = ["postgresql"]
}

# ---------------------------------------------------------------- model artifacts
data "aws_caller_identity" "current" {}

resource "aws_s3_bucket" "artifacts" {
  bucket = "${var.name}-model-artifacts-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_policy" "artifacts_tls_only" {
  bucket = aws_s3_bucket.artifacts.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.artifacts.arn, "${aws_s3_bucket.artifacts.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    id     = "expire-old-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration { noncurrent_days = 90 }
  }
}

output "db_security_group_id" { value = aws_security_group.db.id }
output "db_instance_id" { value = aws_db_instance.this.identifier }
output "database_url_secret_arn" { value = aws_secretsmanager_secret.database_url.arn }
output "jwt_secret_arn" { value = aws_secretsmanager_secret.jwt.arn }
output "pseudonym_secret_arn" { value = aws_secretsmanager_secret.pseudonym.arn }
output "artifact_bucket_name" { value = aws_s3_bucket.artifacts.bucket }
output "artifact_bucket_arn" { value = aws_s3_bucket.artifacts.arn }
