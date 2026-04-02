variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "ap-south-1"
}

variable "cluster_name" {
  description = "EKS cluster name"
  type        = string
}

variable "vpc_id" {
  description = "VPC ID where EKS cluster runs"
  type        = string
}

variable "private_subnet_ids" {
  description = "Private subnet IDs for VPC Endpoint"
  type        = list(string)
}

variable "eks_cluster_sg_id" {
  description = "EKS cluster security group ID"
  type        = string
}

variable "ecr_repository_prefix" {
  description = "ECR repository prefix"
  type        = string
  default     = "k8s-healing"
}

variable "bedrock_model_id" {
  description = "Bedrock model ID"
  type        = string
  default     = "anthropic.claude-3-haiku-20240307-v1:0"
}

variable "namespace" {
  description = "Kubernetes namespace for the agent"
  type        = string
  default     = "atlas-dev"
}

variable "dashboard_domain" {
  description = "Domain for the healing dashboard"
  type        = string
  default     = ""
}

variable "route53_zone_id" {
  description = "Route53 hosted zone ID for dashboard domain"
  type        = string
  default     = ""
}

variable "nlb_dns" {
  description = "NLB DNS name for ingress"
  type        = string
  default     = ""
}

variable "cognito_enabled" {
  description = "Enable Cognito authentication"
  type        = bool
  default     = true
}

variable "tags" {
  description = "Tags for all resources"
  type        = map(string)
  default = {
    Project   = "k8s-healing-agent"
    ManagedBy = "terraform"
  }
}
