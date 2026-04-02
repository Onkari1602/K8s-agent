# K8s Self-Healing Agent — Architecture & Deployment Guide

---

## 1. Prerequisites

### AWS Account Requirements

| Requirement | Details |
|---|---|
| AWS Account | With admin or PowerUser access |
| EKS Cluster | Running Kubernetes 1.27+ |
| ECR Access | To push/pull container images |
| Bedrock Access | Claude 3 Haiku model enabled |
| VPC | With private subnets + NAT Gateway |
| IAM | Permission to create roles, policies, VPC endpoints |
| Route53 | Hosted zone for dashboard domain (optional) |

### Tools Required

| Tool | Version | Purpose |
|---|---|---|
| AWS CLI v2 | 2.x | AWS resource management |
| kubectl | 1.27+ | Kubernetes cluster access |
| Docker | 20+ | Build container images |
| Helm | 3.x | Deploy via Helm charts (optional) |
| Terraform | 1.5+ | Infrastructure as Code (optional) |
| Git | 2.x | Source code management |

### Azure (Optional — for Multi-Cloud)

| Requirement | Details |
|---|---|
| Azure Subscription | With AKS access |
| Service Principal | With `Azure Kubernetes Service Cluster Admin Role` |
| Azure CLI | For credential setup |

### GCP (Optional — for Multi-Cloud)

| Requirement | Details |
|---|---|
| GCP Project | With GKE access |
| Service Account | With `roles/container.clusterAdmin` |
| gcloud CLI | For credential setup |

---

## 2. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              INTERNET / USERS                               │
│                                                                             │
│                          https://healing.your-domain.com                     │
└─────────────────────────────────┬───────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                            AWS CLOUD                                        │
│                                                                             │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │                         VPC (10.0.0.0/16)                            │   │
│  │                                                                      │   │
│  │  ┌─────────────┐     ┌──────────────┐     ┌──────────────────────┐  │   │
│  │  │ Route 53    │────▶│ NLB          │────▶│ Nginx Ingress        │  │   │
│  │  │ DNS         │     │ Load Balancer│     │ Controller           │  │   │
│  │  └─────────────┘     └──────────────┘     └──────────┬───────────┘  │   │
│  │                                                       │              │   │
│  │  ┌────────────────────── EKS CLUSTER ─────────────────┼──────────┐  │   │
│  │  │                                                    │          │  │   │
│  │  │  ┌─────────────────────────────────────────────────┼───────┐  │  │   │
│  │  │  │              WORKER NODES (SPOT)                │       │  │  │   │
│  │  │  │                                                 ▼       │  │  │   │
│  │  │  │  ┌──────────────────┐  ┌──────────────────────────┐    │  │  │   │
│  │  │  │  │ HEALING AGENT    │  │ HEALING DASHBOARD         │    │  │  │   │
│  │  │  │  │ (Pod)            │  │ (Pod)                     │    │  │  │   │
│  │  │  │  │                  │  │                           │    │  │  │   │
│  │  │  │  │ ┌──────────────┐│  │ ┌───────────────────────┐ │    │  │  │   │
│  │  │  │  │ │Watch Loop    ││  │ │ 10 Dashboard Pages     │ │    │  │  │   │
│  │  │  │  │ │(real-time)   ││  │ │                       │ │    │  │  │   │
│  │  │  │  │ ├──────────────┤│  │ │ • Dashboard           │ │    │  │  │   │
│  │  │  │  │ │Poll Loop     ││  │ │ • Cost Optimizer      │ │    │  │  │   │
│  │  │  │  │ │(every 30s)   ││  │ │ • Cost Visibility     │ │    │  │  │   │
│  │  │  │  │ ├──────────────┤│  │ │ • Node Optimizer      │ │    │  │  │   │
│  │  │  │  │ │Continuous    ││  │ │ • Live Optimizer       │ │    │  │  │   │
│  │  │  │  │ │Optimizer     ││  │ │ • Spot Manager         │ │    │  │  │   │
│  │  │  │  │ │(every 5min)  ││  │ │ • Bin Packing          │ │    │  │  │   │
│  │  │  │  │ ├──────────────┤│  │ │ • Security Scanner     │ │    │  │  │   │
│  │  │  │  │ │Spot Manager  ││  │ │ • AI Predictions       │ │    │  │  │   │
│  │  │  │  │ │(every 30s)   ││  │ │ • GPU Optimizer        │ │    │  │  │   │
│  │  │  │  │ └──────────────┘│  │ └───────────────────────┘ │    │  │  │   │
│  │  │  │  └───────┬──────────┘  └──────────┬───────────────┘    │  │  │   │
│  │  │  │          │                        │                    │  │  │   │
│  │  │  │          │    ┌───────────────┐   │                    │  │  │   │
│  │  │  │          │    │ YOUR APP PODS │   │                    │  │  │   │
│  │  │  │          │    │ (monitored)   │   │                    │  │  │   │
│  │  │  │          │    │               │   │                    │  │  │   │
│  │  │  │          ├───▶│ Pod A (fix)   │   │                    │  │  │   │
│  │  │  │          ├───▶│ Pod B (watch) │   │                    │  │  │   │
│  │  │  │          ├───▶│ Pod C (fix)   │   │                    │  │  │   │
│  │  │  │          │    └───────────────┘   │                    │  │  │   │
│  │  │  │          │                        │                    │  │  │   │
│  │  │  └──────────┼────────────────────────┼────────────────────┘  │  │   │
│  │  └─────────────┼────────────────────────┼───────────────────────┘  │   │
│  │                │                        │                          │   │
│  │  ┌─────────────┼────────────────────────┼───────────────────────┐  │   │
│  │  │             │    PRIVATE SUBNETS      │                       │  │   │
│  │  │             ▼                        ▼                       │  │   │
│  │  │  ┌──────────────────┐  ┌──────────────────┐                 │  │   │
│  │  │  │ VPC Endpoint     │  │ VPC Endpoint      │                 │  │   │
│  │  │  │ (Bedrock)        │  │ (DynamoDB)        │                 │  │   │
│  │  │  │ PRIVATE - no     │  │ PRIVATE - no      │                 │  │   │
│  │  │  │ internet needed  │  │ internet needed   │                 │  │   │
│  │  │  └────────┬─────────┘  └────────┬──────────┘                 │  │   │
│  │  │           │                     │                            │  │   │
│  │  └───────────┼─────────────────────┼────────────────────────────┘  │   │
│  └──────────────┼─────────────────────┼───────────────────────────────┘   │
│                 │                     │                                   │
│  ┌──────────────▼──────┐  ┌──────────▼──────────┐  ┌──────────────────┐  │
│  │ Amazon Bedrock      │  │ Amazon DynamoDB      │  │ Amazon Cognito   │  │
│  │                     │  │                      │  │                  │  │
│  │ Claude 3 Haiku      │  │ 3 Tables:            │  │ User Pool        │  │
│  │ - Root cause AI     │  │ • State (dedup,      │  │ - Authentication │  │
│  │ - Cost analysis     │  │   counters, actions) │  │ - OIDC           │  │
│  │ - Predictions       │  │ • Usage (history)    │  │ - Role-based     │  │
│  │ - Security advice   │  │ • Costs (trending)   │  │   access         │  │
│  └─────────────────────┘  └──────────────────────┘  └──────────────────┘  │
│                                                                           │
│  ┌─────────────────────┐  ┌──────────────────────┐  ┌──────────────────┐  │
│  │ Amazon ECR          │  │ AWS CodeBuild        │  │ Amazon SES       │  │
│  │                     │  │                      │  │ (optional)       │  │
│  │ 2 Repos:            │  │ CI/CD Pipeline       │  │                  │  │
│  │ • k8s-healing-agent │  │ Auto-deploy on push  │  │ Email alerts     │  │
│  │ • k8s-healing-dash  │  │ CodeCommit → Build   │  │                  │  │
│  │                     │  │ → ECR → EKS          │  │                  │  │
│  └─────────────────────┘  └──────────────────────┘  └──────────────────┘  │
│                                                                           │
└───────────────────────────────────────────────────────────────────────────┘


                    MULTI-CLOUD (Optional)

┌───────────────────┐  ┌───────────────────┐  ┌───────────────────┐
│ AWS EKS           │  │ Azure AKS         │  │ GCP GKE           │
│ (Primary)         │  │ (Optional)        │  │ (Optional)        │
│                   │  │                   │  │                   │
│ ap-south-1 ●      │  │ eastus ○          │  │ us-central1 ○     │
│ us-east-1  ○      │  │ westeurope ○      │  │ asia-south1 ○     │
│ eu-west-1  ○      │  │                   │  │                   │
│                   │  │ Needs:            │  │ Needs:            │
│ Connected via     │  │ • Service Princ.  │  │ • Service Account │
│ IAM Role          │  │ • Subscription ID │  │ • Project ID      │
└───────────────────┘  └───────────────────┘  └───────────────────┘
         │                      │                      │
         └──────────────────────┼──────────────────────┘
                                │
                    ┌───────────▼───────────┐
                    │ UNIFIED DASHBOARD     │
                    │ Cloud Switcher        │
                    │ [AWS ▼] [ap-south-1]  │
                    │ Switch between clouds │
                    │ and regions in one    │
                    │ click                 │
                    └───────────────────────┘
```

---

## 3. Agent Internal Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    K8s Healing Agent (Pod)                       │
│                                                                 │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │                    MAIN PROCESS                            │  │
│  │                                                           │  │
│  │  Thread 1: WATCH LOOP (real-time)                         │  │
│  │  ├── Watch K8s pod events via Watch API                   │  │
│  │  ├── Detect: CrashLoopBackOff, OOMKilled, Error           │  │
│  │  ├── Detect: ImagePullBackOff, Pending                    │  │
│  │  └── Trigger: analyze → remediate → report                │  │
│  │                                                           │  │
│  │  Thread 2: POLL LOOP (every 30s)                          │  │
│  │  ├── Full cluster scan across all namespaces              │  │
│  │  ├── Safety net for missed watch events                   │  │
│  │  └── Cleanup old reports                                  │  │
│  │                                                           │  │
│  │  Thread 3: CONTINUOUS OPTIMIZER (every 5min)              │  │
│  │  ├── Collect usage metrics per deployment                 │  │
│  │  ├── Intelligent autoscale (trend-based)                  │  │
│  │  ├── Auto right-size (P95-based)                          │  │
│  │  └── Node consolidation (bin-packing)                     │  │
│  │                                                           │  │
│  │  Thread 4: SPOT MANAGER (every 30s)                       │  │
│  │  ├── Check spot termination notices                       │  │
│  │  ├── Auto-drain interrupted nodes                         │  │
│  │  └── Fallback to on-demand if spot unavailable            │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                 │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────────┐  │
│  │Analyzer  │ │Remediator│ │Reporter  │ │Persistent Store  │  │
│  │(Bedrock) │ │(K8s API) │ │(ConfigMap│ │(DynamoDB)        │  │
│  │          │ │          │ │ + Events)│ │                  │  │
│  └──────────┘ └──────────┘ └──────────┘ └──────────────────┘  │
│                                                                 │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────────┐  │
│  │Cost      │ │Security  │ │Prediction│ │Multi-Cloud       │  │
│  │Optimizer │ │Scanner   │ │Engine    │ │Manager           │  │
│  │          │ │          │ │          │ │(AWS+Azure+GCP)   │  │
│  └──────────┘ └──────────┘ └──────────┘ └──────────────────┘  │
│                                                                 │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────────┐  │
│  │Node      │ │Bin Packer│ │GPU       │ │Enterprise        │  │
│  │Optimizer │ │          │ │Optimizer │ │(Slack/Teams/SLA) │  │
│  └──────────┘ └──────────┘ └──────────┘ └──────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 4. Data Flow

```
ISSUE DETECTED                  AI ANALYSIS                 AUTO-FIX                    REPORT

Pod crashes ──────▶ Collect context ──────▶ Send to Bedrock ──────▶ Execute action ──────▶ Store report
                    • Logs (50 lines)       (via VPC Endpoint)      • Restart pod          • ConfigMap
                    • Events                                        • Increase memory      • DynamoDB
                    • Resources             AI returns:             • Rollback deploy      • K8s Event
                    • Owner info            • Root cause            • Report only          • Slack/Teams
                                            • Action                                      • Email
                                            • Confidence
                                            • Prevention steps      Only if:
                                                                    confidence ≥ 70%
                                                                    Not protected NS
                                                                    Not self-healing pod
```

---

## 5. Dashboard Architecture

```
┌────────────────────────────────────────────────────────────────┐
│                     HEALING DASHBOARD                           │
│                                                                │
│  ┌──────────────────────────── TOP BAR ────────────────────┐   │
│  │ ☰  [K8] K8s Healing Agent   [AWS ▼ ap-south-1]  user   │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                │
│  ┌─────────┐  ┌──────────────────────────────────────────┐    │
│  │ SIDEBAR │  │              MAIN CONTENT                 │    │
│  │ (AWS    │  │                                          │    │
│  │  style) │  │  ┌────────────────────────────────────┐  │    │
│  │         │  │  │ Page-specific content               │  │    │
│  │ Monitor │  │  │                                    │  │    │
│  │ • Dash  │  │  │ Stats cards + Tables + Charts      │  │    │
│  │         │  │  │ + Action buttons                   │  │    │
│  │ Cost    │  │  │                                    │  │    │
│  │ • Optim │  │  └────────────────────────────────────┘  │    │
│  │ • Visib │  │                                          │    │
│  │ • Nodes │  │  FastAPI Backend:                        │    │
│  │         │  │  ├── /              Dashboard HTML        │    │
│  │ Optimize│  │  ├── /api/pods      Pod data (JSON)      │    │
│  │ • Live  │  │  ├── /api/resolve   AI fix (POST)        │    │
│  │ • Spot  │  │  ├── /api/cost-breakdown  Cost data      │    │
│  │ • BinPk │  │  ├── /api/node-optimize   Node recs      │    │
│  │ • GPU   │  │  ├── /api/security-scan   Security       │    │
│  │         │  │  ├── /api/predict         AI forecast    │    │
│  │ Intel   │  │  ├── /api/gpu-analyze     GPU data       │    │
│  │ • Secur │  │  ├── /api/spot-status     Spot info      │    │
│  │ • Pred  │  │  ├── /api/binpack         Bin packing    │    │
│  │         │  │  ├── /api/multi-cloud     All clusters   │    │
│  │ user    │  │  ├── /api/sla             SLA check      │    │
│  │ logout  │  │  └── /auth/callback       Cognito OIDC   │    │
│  └─────────┘  └──────────────────────────────────────────┘    │
└────────────────────────────────────────────────────────────────┘
```

---

## 6. Deployment Steps

### Option A: One-Click Deploy (Recommended)

```bash
# 1. Clone the repo
git clone https://github.com/Onkari1602/K8s-agent.git
cd K8s-agent

# 2. Set environment
export AWS_REGION=ap-south-1
export CLUSTER_NAME=your-eks-cluster
export NAMESPACE=your-namespace

# 3. Run deploy script
chmod +x deploy.sh
./deploy.sh
```

### Option B: Terraform + Helm

```bash
# 1. Infrastructure (ECR, VPC Endpoints, Cognito, DynamoDB, IAM, CI/CD)
cd terraform
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars with your values
terraform init
terraform apply

# 2. Build images
docker build -t <ACCOUNT>.dkr.ecr.<REGION>.amazonaws.com/k8s-healing-agent:v1 .
docker build -t <ACCOUNT>.dkr.ecr.<REGION>.amazonaws.com/k8s-healing-dashboard:v1 ./dashboard/
docker push <both images>

# 3. Deploy with Helm
helm install k8s-healing-agent ./helm/k8s-healing-agent \
  --namespace <NAMESPACE> --create-namespace \
  --set clusterName=<CLUSTER> \
  --set config.dryRun=true
```

### Option C: kubectl (Manual)

```bash
# 1. Create ECR repos
aws ecr create-repository --repository-name k8s-healing-agent
aws ecr create-repository --repository-name k8s-healing-dashboard

# 2. Build and push
aws ecr get-login-password | docker login --username AWS --password-stdin <ECR_URI>
docker build -t <ECR_URI>/k8s-healing-agent:v1 .
docker push <ECR_URI>/k8s-healing-agent:v1
docker build -t <ECR_URI>/k8s-healing-dashboard:v1 ./dashboard/
docker push <ECR_URI>/k8s-healing-dashboard:v1

# 3. Create VPC Endpoint for Bedrock
aws ec2 create-vpc-endpoint --vpc-id <VPC> \
  --service-name com.amazonaws.<REGION>.bedrock-runtime \
  --vpc-endpoint-type Interface --subnet-ids <SUBNETS> \
  --security-group-ids <SG> --private-dns-enabled

# 4. Deploy K8s resources
kubectl apply -f k8s/

# 5. Verify
kubectl get pods -n <NAMESPACE> -l "app in (k8s-healing-agent, healing-dashboard)"
```

---

## 7. Security Architecture

```
┌─────────────────────────────────────────────────────┐
│                  SECURITY LAYERS                     │
│                                                     │
│  1. NETWORK                                         │
│  ├── VPC Endpoints (Bedrock, DynamoDB) — no internet│
│  ├── Private subnets for worker nodes               │
│  └── Security groups restrict access                │
│                                                     │
│  2. AUTHENTICATION                                  │
│  ├── AWS Cognito OIDC for dashboard                 │
│  ├── Role-based access (admin/operator/viewer)      │
│  └── 24-hour session tokens                         │
│                                                     │
│  3. AUTHORIZATION (K8s RBAC)                        │
│  ├── ClusterRole with least-privilege               │
│  ├── Read: pods, logs, events, nodes                │
│  ├── Write: delete pods, patch deployments          │
│  ├── Cannot: create deployments, access secrets     │
│  └── self-healing=disabled label protects agent      │
│                                                     │
│  4. AWS IAM                                         │
│  ├── IRSA (IAM Role for Service Account)            │
│  ├── Only: bedrock:InvokeModel                      │
│  ├── Only: dynamodb:* on agent tables               │
│  ├── Only: ecr:Describe* for vulnerability scan     │
│  └── Only: eks:Describe* for multi-cluster          │
│                                                     │
│  5. DATA PROTECTION                                 │
│  ├── All data stays in VPC                          │
│  ├── DynamoDB encrypted at rest                     │
│  ├── TTL auto-deletes old data                      │
│  └── No static credentials (IRSA only)              │
│                                                     │
│  6. AGENT SAFETY                                    │
│  ├── DRY_RUN mode (default ON)                      │
│  ├── Confidence threshold (70%)                     │
│  ├── Protected namespaces (kube-system)             │
│  ├── Memory cap (4Gi max)                           │
│  ├── 3-cycle delay before scaling down              │
│  └── Agent cannot modify itself                     │
└─────────────────────────────────────────────────────┘
```

---

## 8. Cost Architecture

```
FIXED COSTS (always running):
├── Agent pod ────────────────── $0 (runs on existing node)
├── Dashboard pod ────────────── $0 (runs on existing node)
├── Dedicated SPOT node ──────── ~$22/mo (if dedicated)
├── VPC Endpoint (Bedrock) ──── ~$15/mo
├── VPC Endpoint (DynamoDB) ─── $0 (gateway type)
├── ECR (2 repos, ~150MB) ──── ~$1/mo
├── Cognito (<50 users) ────── $0 (free tier)
├── Route53 (1 record) ────── ~$0.50/mo
└── DynamoDB (3 tables) ────── ~$1-2/mo
                                ─────────
                                ~$40/mo TOTAL

VARIABLE COSTS:
├── Bedrock Claude Haiku ────── ~$3-5/mo (5000 calls)
├── CodeBuild CI/CD ─────────── ~$1/mo (20 builds)
├── CloudWatch Logs ─────────── ~$1/mo
└── Data Transfer ────────────── ~$0-1/mo
                                ─────────
                                ~$5-8/mo

TOTAL: ~$45-50/month (any cluster size)
```

---

## 9. Module Reference (21 Python Modules)

```
src/
├── agent.py              ── Main agent (4 background threads)
├── analyzer.py           ── Bedrock AI root cause analysis
├── remediation.py        ── Auto-fix (restart, memory, rollback)
├── reporter.py           ── Report generation (ConfigMap, Events)
├── continuous_optimizer.py── Autoscaling + right-sizing (P95)
├── spot_manager.py       ── Spot interruption handler + fallback
├── cost_optimizer.py     ── AI cost analysis + recommendations
├── cost_attribution.py   ── Cost per namespace/deployment/pod
├── node_optimizer.py     ── Node replacement + migration plan
├── bin_packer.py         ── Node scoring + defragmentation
├── gpu_optimizer.py      ── GPU tracking + right-sizing + cost
├── security_scanner.py   ── 6 security checks + compliance score
├── prediction_engine.py  ── AI forecast + anomaly + capacity
├── persistent_store.py   ── DynamoDB backend for state
├── multi_cloud.py        ── AWS + Azure + GCP cluster discovery
├── enterprise.py         ── Slack/Teams + SLA + RBAC + escalation
├── smart_autoscaler.py   ── Instance catalog + scaling logic
├── config.py             ── Environment variable configuration
├── models.py             ── Data classes (PodIssue, Report)
├── __init__.py           ── Package init
└── __main__.py           ── Entry point

dashboard/src/
├── app.py                ── FastAPI (30+ API endpoints)
├── auth.py               ── Cognito OIDC authentication
└── templates/
    ├── sidebar.html      ── AWS-style sidebar + cloud switcher
    ├── dashboard.html    ── Pod health + Resolve button
    ├── cost.html         ── AI cost analysis
    ├── costs.html        ── Cost per namespace/deployment/pod
    ├── nodes.html        ── Node optimizer + replacement
    ├── optimizer.html    ── Live autoscaling actions
    ├── spot.html         ── Spot savings + interruptions
    ├── binpack.html      ── Bin packing efficiency
    ├── security.html     ── Security scanner + score
    ├── predict.html      ── AI predictions + capacity
    ├── gpu.html          ── GPU optimizer
    └── login.html        ── Cognito login page
```
