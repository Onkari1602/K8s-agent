output "ecr_agent_repository" {
  value = aws_ecr_repository.agent.repository_url
}

output "ecr_dashboard_repository" {
  value = aws_ecr_repository.dashboard.repository_url
}

output "bedrock_vpce_id" {
  value = aws_vpc_endpoint.bedrock.id
}

output "bedrock_vpce_dns" {
  value = aws_vpc_endpoint.bedrock.dns_entry[0].dns_name
}

output "iam_role_arn" {
  value = aws_iam_role.healing_agent.arn
}

output "cognito_user_pool_id" {
  value = var.cognito_enabled ? aws_cognito_user_pool.dashboard[0].id : ""
}

output "cognito_client_id" {
  value = var.cognito_enabled ? aws_cognito_user_pool_client.dashboard[0].id : ""
}

output "cognito_domain" {
  value = var.cognito_enabled ? aws_cognito_user_pool_domain.dashboard[0].domain : ""
}

output "codecommit_repo_url" {
  value = aws_codecommit_repository.agent.clone_url_http
}

output "codebuild_project" {
  value = aws_codebuild_project.agent.name
}

output "cicd_setup_commands" {
  value = <<-EOT
    # Clone the repo
    git clone ${aws_codecommit_repository.agent.clone_url_http}
    cd k8s-healing-agent

    # Copy source code
    cp -r /path/to/k8s-self-healing-agent/* .
    cp -r /path/to/k8s-healing-dashboard ./dashboard

    # Push to trigger auto-deploy
    git add -A
    git commit -m "Initial deploy"
    git push origin main

    # Pipeline will automatically:
    # 1. Build Agent Docker image
    # 2. Build Dashboard Docker image
    # 3. Push both to ECR
    # 4. Deploy to EKS via kubectl
  EOT
}

output "helm_install_command" {
  value = <<-EOT
    helm install k8s-healing-agent ./helm/k8s-healing-agent \
      --namespace ${var.namespace} \
      --set agent.image.repository=${aws_ecr_repository.agent.repository_url} \
      --set dashboard.image.repository=${aws_ecr_repository.dashboard.repository_url} \
      --set clusterName=${var.cluster_name} \
      --set awsRegion=${var.aws_region} \
      --set namespace=${var.namespace} \
      ${var.cognito_enabled ? "--set cognito.userPoolId=${aws_cognito_user_pool.dashboard[0].id}" : ""} \
      ${var.cognito_enabled ? "--set cognito.clientId=${aws_cognito_user_pool_client.dashboard[0].id}" : ""} \
      ${var.cognito_enabled ? "--set cognito.clientSecret=${aws_cognito_user_pool_client.dashboard[0].client_secret}" : ""} \
      ${var.cognito_enabled ? "--set cognito.domain=${aws_cognito_user_pool_domain.dashboard[0].domain}" : ""}
  EOT
}
