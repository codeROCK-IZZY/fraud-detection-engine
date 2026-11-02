variable "region" {
  type    = string
  default = "us-east-1"
}

variable "environment" {
  type    = string
  default = "prod"
}

variable "name" {
  description = "Name prefix for all resources."
  type        = string
  default     = "fde"
}

variable "vpc_cidr" {
  type    = string
  default = "10.40.0.0/16"
}

variable "single_nat_gateway" {
  description = "One NAT gateway (~$33/month) instead of one per AZ. Cheaper, but private subnets lose egress if that AZ fails."
  type        = bool
  default     = true
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "db_multi_az" {
  description = "Standby replica in a second AZ. Roughly doubles RDS cost; recommended for real production."
  type        = bool
  default     = false
}

variable "image_tag" {
  description = "Container image tag to deploy (set by the deploy pipeline, e.g. the git SHA)."
  type        = string
  default     = "latest"
}

variable "certificate_arn" {
  description = "ACM certificate for HTTPS. Without it the ALB serves plain HTTP — acceptable only for a short-lived demo."
  type        = string
  default     = ""
}

variable "alarm_email" {
  description = "E-mail address for CloudWatch alarms and the budget alert (confirmation e-mail is sent)."
  type        = string
  default     = ""
}

variable "monthly_budget_usd" {
  type    = number
  default = 150
}

variable "github_repository" {
  description = "owner/repo allowed to deploy through GitHub Actions OIDC (empty = no deploy role)."
  type        = string
  default     = ""
}
