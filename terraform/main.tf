terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}

# ============================================
# ECR Repositories
# ============================================
resource "aws_ecr_repository" "agent" {
  name                 = "${var.ecr_repository_prefix}-agent"
  image_tag_mutability = "MUTABLE"
  tags                 = var.tags
}

resource "aws_ecr_repository" "dashboard" {
  name                 = "${var.ecr_repository_prefix}-dashboard"
  image_tag_mutability = "MUTABLE"
  tags                 = var.tags
}

# ============================================
# VPC Endpoint for Bedrock (Private Access)
# ============================================
resource "aws_security_group" "bedrock_vpce" {
  name        = "bedrock-vpce-sg"
  description = "Security group for Bedrock VPC Endpoint"
  vpc_id      = var.vpc_id

  ingress {
    from_port       = 443
    to_port         = 443
    protocol        = "tcp"
    security_groups = [var.eks_cluster_sg_id]
    description     = "HTTPS from EKS cluster"
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, { Name = "bedrock-vpce-sg" })
}

resource "aws_vpc_endpoint" "bedrock" {
  vpc_id              = var.vpc_id
  service_name        = "com.amazonaws.${var.aws_region}.bedrock-runtime"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = var.private_subnet_ids
  security_group_ids  = [aws_security_group.bedrock_vpce.id]
  private_dns_enabled = true

  tags = merge(var.tags, { Name = "bedrock-runtime-vpce" })
}

# ============================================
# VPC Endpoint for DynamoDB (Private Access)
# ============================================
resource "aws_vpc_endpoint" "dynamodb" {
  vpc_id            = var.vpc_id
  service_name      = "com.amazonaws.${var.aws_region}.dynamodb"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = data.aws_route_tables.private.ids

  tags = merge(var.tags, { Name = "dynamodb-vpce" })
}

data "aws_route_tables" "private" {
  vpc_id = var.vpc_id
  filter {
    name   = "association.subnet-id"
    values = var.private_subnet_ids
  }
}

# ============================================
# DynamoDB Tables
# ============================================
resource "aws_dynamodb_table" "state" {
  name         = "k8s-healing-agent-state"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute { name = "pk"; type = "S" }
  attribute { name = "sk"; type = "S" }

  ttl { attribute_name = "ttl"; enabled = true }
  tags = var.tags
}

resource "aws_dynamodb_table" "usage" {
  name         = "k8s-healing-agent-usage"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "deployment_key"
  range_key    = "timestamp"

  attribute { name = "deployment_key"; type = "S" }
  attribute { name = "timestamp";      type = "S" }

  ttl { attribute_name = "ttl"; enabled = true }
  tags = var.tags
}

resource "aws_dynamodb_table" "costs" {
  name         = "k8s-healing-agent-costs"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "date"

  attribute { name = "date"; type = "S" }

  ttl { attribute_name = "ttl"; enabled = true }
  tags = var.tags
}

# ============================================
# Cognito User Pool (Optional)
# ============================================
resource "aws_cognito_user_pool" "dashboard" {
  count = var.cognito_enabled ? 1 : 0

  name = "k8s-healing-dashboard-users"

  auto_verified_attributes = ["email"]
  username_attributes      = ["email"]

  password_policy {
    minimum_length    = 8
    require_uppercase = true
    require_lowercase = true
    require_numbers   = true
    require_symbols   = false
  }

  schema {
    name     = "email"
    required = true
    mutable  = true

    attribute_data_type = "String"
    string_attribute_constraints {
      min_length = 1
      max_length = 256
    }
  }

  tags = var.tags
}

resource "aws_cognito_user_pool_domain" "dashboard" {
  count        = var.cognito_enabled ? 1 : 0
  domain       = "k8s-healing-${data.aws_caller_identity.current.account_id}"
  user_pool_id = aws_cognito_user_pool.dashboard[0].id
}

resource "aws_cognito_user_pool_client" "dashboard" {
  count        = var.cognito_enabled ? 1 : 0
  name         = "healing-dashboard"
  user_pool_id = aws_cognito_user_pool.dashboard[0].id

  generate_secret                      = true
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = ["https://${var.dashboard_domain}/auth/callback"]
  logout_urls                          = ["https://${var.dashboard_domain}/logout"]
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  allowed_oauth_flows_user_pool_client = true
  explicit_auth_flows                  = ["ALLOW_REFRESH_TOKEN_AUTH", "ALLOW_USER_SRP_AUTH"]
}

# ============================================
# Route53 DNS (Optional)
# ============================================
resource "aws_route53_record" "dashboard" {
  count   = var.dashboard_domain != "" && var.route53_zone_id != "" ? 1 : 0
  zone_id = var.route53_zone_id
  name    = var.dashboard_domain
  type    = "CNAME"
  ttl     = 300
  records = [var.nlb_dns]
}

# ============================================
# IAM Role for IRSA (Best Practice)
# ============================================
data "aws_eks_cluster" "cluster" {
  name = var.cluster_name
}

data "aws_iam_openid_connect_provider" "eks" {
  url = data.aws_eks_cluster.cluster.identity[0].oidc[0].issuer
}

resource "aws_iam_role" "healing_agent" {
  name = "k8s-healing-agent-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Federated = data.aws_iam_openid_connect_provider.eks.arn
        }
        Action = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "${replace(data.aws_eks_cluster.cluster.identity[0].oidc[0].issuer, "https://", "")}:sub" = "system:serviceaccount:${var.namespace}:k8s-healing-agent-sa"
          }
        }
      }
    ]
  })

  tags = var.tags
}

resource "aws_iam_role_policy" "healing_agent" {
  name = "k8s-healing-agent-policy"
  role = aws_iam_role.healing_agent.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream"
        ]
        Resource = "arn:aws:bedrock:${var.aws_region}::foundation-model/${var.bedrock_model_id}"
      },
      {
        Effect = "Allow"
        Action = [
          "ecr:GetAuthorizationToken",
          "ecr:DescribeImages",
          "ecr:DescribeImageScanFindings"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "eks:DescribeCluster",
          "eks:ListClusters",
          "eks:ListNodegroups",
          "eks:DescribeNodegroup",
          "eks:UpdateNodegroupConfig"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "ec2:DescribeInstances",
          "ec2:DescribeInstanceStatus",
          "ec2:DescribeSpotPriceHistory",
          "ec2:CreateLaunchTemplateVersion",
          "ec2:DescribeLaunchTemplateVersions"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
          "dynamodb:Query",
          "dynamodb:Scan",
          "dynamodb:CreateTable",
          "dynamodb:DescribeTable",
          "dynamodb:ListTables",
          "dynamodb:UpdateTimeToLive"
        ]
        Resource = "arn:aws:dynamodb:${var.aws_region}:${data.aws_caller_identity.current.account_id}:table/k8s-healing-agent-*"
      }
    ]
  })
}
