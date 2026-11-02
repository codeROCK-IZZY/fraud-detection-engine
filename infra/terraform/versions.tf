terraform {
  required_version = ">= 1.9"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 5.80" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
  # Remote state: copy backend.hcl.example to backend.hcl, then
  #   terraform init -backend-config=backend.hcl
  backend "s3" {}
}
