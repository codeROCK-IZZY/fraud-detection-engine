provider "aws" {
  region = var.region
  # Credentials come from the environment (SSO profile, or the GitHub OIDC role in CI).
  # Never put access keys in this repository.
  default_tags {
    tags = {
      Project     = "predictive-maintenance"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}
