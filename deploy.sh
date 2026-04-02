#!/bin/bash
set -e

echo "============================================"
echo "K8s Self-Healing Agent - One-Click Deploy"
echo "============================================"

# Configuration
AWS_REGION="${AWS_REGION:-ap-south-1}"
CLUSTER_NAME="${CLUSTER_NAME:-atlas-api-manager-eks-cluster}"
NAMESPACE="${NAMESPACE:-atlas-dev}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
AGENT_REPO="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/k8s-healing-agent"
DASHBOARD_REPO="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/k8s-healing-dashboard"

echo ""
echo "Configuration:"
echo "  Region:    ${AWS_REGION}"
echo "  Cluster:   ${CLUSTER_NAME}"
echo "  Namespace: ${NAMESPACE}"
echo "  Account:   ${ACCOUNT_ID}"
echo ""

# Step 1: Terraform (infrastructure)
echo "[1/5] Setting up AWS infrastructure..."
cd terraform
cp terraform.tfvars.example terraform.tfvars 2>/dev/null || true
terraform init
terraform apply -auto-approve
cd ..
echo "Infrastructure ready."

# Step 2: Build and push images
echo "[2/5] Building and pushing Docker images..."
aws ecr get-login-password --region ${AWS_REGION} | docker login --username AWS --password-stdin ${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com

docker build -t ${AGENT_REPO}:latest .
docker push ${AGENT_REPO}:latest

cd ../k8s-healing-dashboard
docker build -t ${DASHBOARD_REPO}:latest .
docker push ${DASHBOARD_REPO}:latest
cd ../k8s-self-healing-agent
echo "Images pushed."

# Step 3: Update kubeconfig
echo "[3/5] Configuring kubectl..."
aws eks update-kubeconfig --name ${CLUSTER_NAME} --region ${AWS_REGION}

# Step 4: Helm install
echo "[4/5] Deploying with Helm..."
helm upgrade --install k8s-healing-agent ./helm/k8s-healing-agent \
  --namespace ${NAMESPACE} \
  --create-namespace \
  --set agent.image.repository=${AGENT_REPO} \
  --set agent.image.tag=latest \
  --set dashboard.image.repository=${DASHBOARD_REPO} \
  --set dashboard.image.tag=latest \
  --set clusterName=${CLUSTER_NAME} \
  --set awsRegion=${AWS_REGION} \
  --set namespace=${NAMESPACE} \
  --wait --timeout 120s
echo "Helm deploy complete."

# Step 5: Verify
echo "[5/5] Verifying deployment..."
kubectl get pods -n ${NAMESPACE} -l "app in (k8s-healing-agent, healing-dashboard)"
echo ""
echo "============================================"
echo "Deployment complete!"
echo ""
echo "Dashboard: https://$(kubectl get ingress -n ${NAMESPACE} healing-dashboard-ingress -o jsonpath='{.spec.rules[0].host}' 2>/dev/null || echo 'N/A')"
echo "Agent logs: kubectl logs -n ${NAMESPACE} -l app=k8s-healing-agent -f"
echo "============================================"
