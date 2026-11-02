output "app_url" { value = local.app_origin }
output "ecr_api_url" { value = aws_ecr_repository.api.repository_url }
output "ecr_web_url" { value = aws_ecr_repository.web.repository_url }
output "cluster_name" { value = aws_ecs_cluster.this.name }
output "deploy_role_arn" { value = var.github_repository != "" ? aws_iam_role.deploy[0].arn : null }
output "migrate_command" {
  value = join(" ", [
    "aws ecs run-task --cluster ${aws_ecs_cluster.this.name} --launch-type FARGATE",
    "--task-definition ${aws_ecs_task_definition.worker.family}",
    "--network-configuration 'awsvpcConfiguration={subnets=[${join(",", var.private_subnet_ids)}],securityGroups=[${aws_security_group.tasks.id}]}'",
    "--overrides '{\"containerOverrides\":[{\"name\":\"worker\",\"command\":[\"python\",\"-m\",\"app.cli\",\"migrate\"]}]}'",
  ])
}
