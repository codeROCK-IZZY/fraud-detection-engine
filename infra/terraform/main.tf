# AI Predictive Maintenance — AWS deployment (ECS Fargate + RDS PostgreSQL + S3).
# See docs/AWS_DEPLOYMENT.md for the architecture, cost estimate, deploy and teardown steps.

module "network" {
  source             = "./modules/network"
  name               = var.name
  cidr               = var.vpc_cidr
  single_nat_gateway = var.single_nat_gateway
}

module "data" {
  source             = "./modules/data"
  name               = var.name
  vpc_id             = module.network.vpc_id
  private_subnet_ids = module.network.private_subnet_ids
  db_instance_class  = var.db_instance_class
  db_multi_az        = var.db_multi_az
}

module "app" {
  source                  = "./modules/app"
  name                    = var.name
  region                  = var.region
  vpc_id                  = module.network.vpc_id
  public_subnet_ids       = module.network.public_subnet_ids
  private_subnet_ids      = module.network.private_subnet_ids
  db_security_group_id    = module.data.db_security_group_id
  database_url_secret_arn = module.data.database_url_secret_arn
  jwt_secret_arn          = module.data.jwt_secret_arn
  pseudonym_secret_arn    = module.data.pseudonym_secret_arn
  artifact_bucket_name    = module.data.artifact_bucket_name
  artifact_bucket_arn     = module.data.artifact_bucket_arn
  image_tag               = var.image_tag
  certificate_arn         = var.certificate_arn
  alarm_email             = var.alarm_email
  monthly_budget_usd      = var.monthly_budget_usd
  github_repository       = var.github_repository
  db_instance_id          = module.data.db_instance_id
}
