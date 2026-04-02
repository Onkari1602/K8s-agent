# ============================================
# CI/CD Pipeline — Auto-deploy on code push
# CodeCommit → CodeBuild → ECR → EKS
# ============================================

# CodeCommit Repository
resource "aws_codecommit_repository" "agent" {
  repository_name = "k8s-healing-agent"
  description     = "K8s Self-Healing Agent source code"
  tags            = var.tags
}

# CodeBuild IAM Role
resource "aws_iam_role" "codebuild" {
  name = "k8s-healing-agent-codebuild-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "codebuild.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy" "codebuild" {
  name = "k8s-healing-agent-codebuild-policy"
  role = aws_iam_role.codebuild.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "ecr:GetAuthorizationToken", "ecr:BatchCheckLayerAvailability",
          "ecr:CompleteLayerUpload", "ecr:InitiateLayerUpload",
          "ecr:PutImage", "ecr:UploadLayerPart", "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "eks:DescribeCluster"
        ]
        Resource = "arn:aws:eks:${var.aws_region}:${data.aws_caller_identity.current.account_id}:cluster/${var.cluster_name}"
      },
      {
        Effect = "Allow"
        Action = [
          "codecommit:GitPull"
        ]
        Resource = aws_codecommit_repository.agent.arn
      }
    ]
  })
}

# Attach EKS access for kubectl
resource "aws_iam_role_policy_attachment" "codebuild_eks" {
  role       = aws_iam_role.codebuild.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

# CodeBuild Project
resource "aws_codebuild_project" "agent" {
  name          = "k8s-healing-agent-build"
  description   = "Build and deploy K8s Healing Agent"
  build_timeout = 15
  service_role  = aws_iam_role.codebuild.arn

  source {
    type     = "CODECOMMIT"
    location = aws_codecommit_repository.agent.clone_url_http
  }

  artifacts {
    type = "NO_ARTIFACTS"
  }

  environment {
    compute_type                = "BUILD_GENERAL1_SMALL"
    image                       = "aws/codebuild/amazonlinux2-x86_64-standard:5.0"
    type                        = "LINUX_CONTAINER"
    privileged_mode             = true
    image_pull_credentials_type = "CODEBUILD"

    environment_variable {
      name  = "AWS_REGION"
      value = var.aws_region
    }
    environment_variable {
      name  = "CLUSTER_NAME"
      value = var.cluster_name
    }
    environment_variable {
      name  = "NAMESPACE"
      value = var.namespace
    }
  }

  tags = var.tags
}

# CloudWatch Event Rule — Trigger on CodeCommit push
resource "aws_cloudwatch_event_rule" "codecommit_push" {
  name        = "k8s-healing-agent-push"
  description = "Trigger build on code push to main branch"

  event_pattern = jsonencode({
    source      = ["aws.codecommit"]
    detail-type = ["CodeCommit Repository State Change"]
    resources   = [aws_codecommit_repository.agent.arn]
    detail = {
      event         = ["referenceCreated", "referenceUpdated"]
      referenceType = ["branch"]
      referenceName = ["main", "master"]
    }
  })

  tags = var.tags
}

resource "aws_iam_role" "eventbridge_codebuild" {
  name = "k8s-healing-agent-eventbridge-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy" "eventbridge_codebuild" {
  name = "start-codebuild"
  role = aws_iam_role.eventbridge_codebuild.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "codebuild:StartBuild"
      Resource = aws_codebuild_project.agent.arn
    }]
  })
}

resource "aws_cloudwatch_event_target" "codebuild" {
  rule     = aws_cloudwatch_event_rule.codecommit_push.name
  arn      = aws_codebuild_project.agent.arn
  role_arn = aws_iam_role.eventbridge_codebuild.arn
}
