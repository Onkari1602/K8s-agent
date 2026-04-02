  # K8s Self-Healing Agent — Complete Deployment Guide

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [AWS Services Used](#aws-services-used)
4. [Prerequisites](#prerequisites)
5. [Step 1: Create ECR Repositories](#step-1-create-ecr-repositories)
6. [Step 2: Create VPC Endpoint for Bedrock](#step-2-create-vpc-endpoint-for-bedrock-private-access)
7. [Step 3: Enable Bedrock Model Access](#step-3-enable-bedrock-model-access)
8. [Step 4: Project Structure and Source Files](#step-4-project-structure-and-source-files)
9. [Step 5: Build and Push Docker Images](#step-5-build-and-push-docker-images)
10. [Step 6: Deploy Kubernetes Resources](#step-6-deploy-kubernetes-resources)
11. [Step 7: Expose Dashboard via Ingress](#step-7-expose-dashboard-via-ingress)
12. [Step 8: Verify Deployment](#step-8-verify-deployment)
13. [Step 9: Switch to Active Remediation](#step-9-switch-to-active-remediation)
14. [Step 10: Verify Private Environment](#step-10-verify-private-environment)
15. [Configuration Reference](#configuration-reference)
16. [Agent Capabilities](#agent-capabilities)
17. [Safety Mechanisms](#safety-mechanisms)
18. [Dashboard Features](#dashboard-features)
19. [Roadmap](#roadmap)
20. [ROI Estimate](#roi-estimate)

---

## Overview

The K8s Self-Healing Agent is an AI-powered Kubernetes cluster monitoring and remediation system. It runs as a pod inside your EKS cluster, continuously watches all pods and nodes, detects failures, analyzes root causes using Amazon Bedrock (Claude AI), and auto-resolves common issues — all within your private VPC.

### Key Capabilities

- **Real-time monitoring** of all pods across all namespaces
- **AI-powered root cause analysis** using Amazon Bedrock (Claude)
- **Auto-remediation** of OOMKilled, CrashLoopBackOff, Error states
- **Web dashboard** with one-click "Resolve" button
- **Works in private environments** via VPC Endpoints (no internet required)
- **Report generation** with root cause, fix applied, and prevention steps

---

## Architecture

```
                    +---------------------------------------------+
                    |           EKS Cluster                        |
                    |                                             |
                    |   +-------------------------------------+   |
                    |   |   K8s Self-Healing Agent (Pod)       |   |
                    |   |                                     |   |
                    |   |   1. MONITOR                        |   |
                    |   |   Watch + Poll all pods/nodes       |   |
                    |   |          |                           |   |
                    |   |          v                           |   |
                    |   |   2. DETECT                          |   |
                    |   |   CrashLoopBackOff, OOMKilled        |   |
                    |   |   ImagePullBackOff, Pending, Error   |   |
                    |   |          |                           |   |
                    |   |          v                           |   |
                    |   |   3. ANALYZE (Bedrock AI)            |------- VPC Endpoint ----> Amazon Bedrock
                    |   |   Send logs + events + context       |                           (Claude Model)
                    |   |   Receive root cause + action        |
                    |   |          |                           |   |
                    |   |          v                           |   |
                    |   |   4. REMEDIATE                       |   |
                    |   |   - Restart Pod                      |   |
                    |   |   - Increase Memory                  |   |
                    |   |   - Rollback Deployment              |   |
                    |   |   - Report Only (complex issues)     |   |
                    |   |          |                           |   |
                    |   |          v                           |   |
                    |   |   5. REPORT                          |   |
                    |   |   Store in ConfigMap/S3              |   |
                    |   |   Emit K8s Event                     |   |
                    |   +-------------------------------------+   |
                    |                                             |
                    |   +-------------------------------------+   |
                    |   |   Healing Dashboard (Pod)            |   |
                    |   |                                     |   |
                    |   |   - Issues Tab (all unhealthy pods)  |   |
                    |   |   - AI Reports Tab                   |   |
                    |   |   - All Pods Tab                     |   |
                    |   |   - Resolve Button (on-demand fix)   |------- Bedrock AI
                    |   +-------------------------------------+   |
                    |                                             |
                    |   +------+ +------+ +------+                |
                    |   |Pod A | |Pod B | |Pod C |  ...           |
                    |   |(fix) | |(mon) | |(fix) |                |
                    |   +------+ +------+ +------+                |
                    +---------------------------------------------+
```

### Workflow

```
1. Agent watches all pods across all namespaces (every 30s)
       |
2. Detects unhealthy pod (CrashLoopBackOff, OOMKilled, etc.)
       |
3. Collects context: logs (50 lines), events, resource limits, owner info
       |
4. Sends to Amazon Bedrock (Claude) via VPC Endpoint (private, no internet)
       |
5. AI returns: root cause, recommended action, confidence score, prevention steps
       |
6. If confidence >= 70%: auto-executes the action
   If confidence < 70%: generates report only
       |
7. Stores report (ConfigMap) + emits K8s Event
       |
8. Dashboard shows everything in real-time
   Team can also click "Resolve" to trigger on-demand AI fix
```

---

## AWS Services Used

| # | Service | Purpose |
|---|---|---|
| 1 | **Amazon EKS** | Kubernetes cluster hosting the agent |
| 2 | **Amazon EC2 (SPOT)** | Worker nodes for agent pods |
| 3 | **Amazon ECR** | Container image registry for agent and dashboard |
| 4 | **Amazon Bedrock** | AI model (Claude) for root cause analysis and remediation |
| 5 | **AWS VPC Endpoints** | Private Bedrock access without internet |
| 6 | **AWS IAM** | RBAC, service accounts, Bedrock permissions |
| 7 | **Amazon S3** | Optional report storage (long-term) |
| 8 | **Amazon Route 53** | DNS for dashboard domain (optional) |

---

## Prerequisites

| Requirement | Details |
|---|---|
| AWS Account | With EKS, ECR, Bedrock, VPC access |
| EKS Cluster | Running with kubectl access |
| Docker | On a build machine (EC2 or local) |
| AWS CLI v2 | Configured with appropriate permissions |
| kubectl | Connected to your EKS cluster |
| Bedrock Model Access | Claude 3 Haiku enabled in your region |

---

## Step 1: Create ECR Repositories

### CLI

```bash
aws ecr create-repository \
  --repository-name k8s-healing-agent \
  --region ap-south-1 \
  --image-tag-mutability MUTABLE

aws ecr create-repository \
  --repository-name k8s-healing-dashboard \
  --region ap-south-1 \
  --image-tag-mutability MUTABLE
```

### Console

1. Go to **Amazon ECR** > **Repositories** > **Create repository**
2. Repository name: `k8s-healing-agent`
3. Image tag mutability: Mutable
4. Repeat for `k8s-healing-dashboard`

---

## Step 2: Create VPC Endpoint for Bedrock (Private Access)

This allows the agent to call Bedrock without internet access.

### CLI

```bash
# 1. Create Security Group for VPC Endpoint
aws ec2 create-security-group \
  --group-name bedrock-vpce-sg \
  --description "Security group for Bedrock VPC Endpoint" \
  --vpc-id <YOUR_VPC_ID> \
  --region ap-south-1

# Note the Security Group ID returned (e.g., sg-0xxxxxxxxx)

# 2. Allow HTTPS (443) from EKS cluster security group
aws ec2 authorize-security-group-ingress \
  --group-id <VPCE_SG_ID> \
  --protocol tcp \
  --port 443 \
  --source-group <EKS_CLUSTER_SG_ID> \
  --region ap-south-1

# 3. Create the VPC Endpoint
aws ec2 create-vpc-endpoint \
  --vpc-id <YOUR_VPC_ID> \
  --service-name com.amazonaws.ap-south-1.bedrock-runtime \
  --vpc-endpoint-type Interface \
  --subnet-ids <PRIVATE_SUBNET_1> <PRIVATE_SUBNET_2> \
  --security-group-ids <VPCE_SG_ID> \
  --private-dns-enabled \
  --region ap-south-1

# 4. Verify it's available
aws ec2 describe-vpc-endpoints \
  --vpc-endpoint-ids <VPCE_ID> \
  --query 'VpcEndpoints[0].{State: State, DNS: DnsEntries[0].DnsName}' \
  --region ap-south-1
```

Wait until State = `available`.

### Console

1. Go to **VPC** > **Endpoints** > **Create endpoint**
2. Service category: AWS services
3. Search: `com.amazonaws.ap-south-1.bedrock-runtime`
4. VPC: Select your EKS VPC
5. Subnets: Select private subnets
6. Security groups: Select/create SG allowing 443 from EKS cluster SG
7. Enable private DNS: Yes
8. Create endpoint

### Finding Required IDs

```bash
# Your VPC ID
aws eks describe-cluster --name <CLUSTER_NAME> --query 'cluster.resourcesVpcConfig.vpcId' --output text

# EKS Cluster Security Group
aws eks describe-cluster --name <CLUSTER_NAME> --query 'cluster.resourcesVpcConfig.clusterSecurityGroupId' --output text

# Private Subnets
aws ec2 describe-subnets --filters "Name=vpc-id,Values=<VPC_ID>" "Name=map-public-ip-on-launch,Values=false" --query 'Subnets[*].SubnetId' --output text
```

---

## Step 3: Enable Bedrock Model Access

### Console

1. Go to **Amazon Bedrock** > **Model access** (left sidebar)
2. Click **Manage model access**
3. Enable: `Anthropic - Claude 3 Haiku`
4. Click **Save changes**

This is a one-time setup per account per region. The agent cannot call models that haven't been enabled.

---

## Step 4: Project Structure and Source Files

```
k8s-self-healing-agent/
├── src/
│   ├── __init__.py          # Empty init
│   ├── __main__.py          # Entry point
│   ├── config.py            # Configuration from env vars
│   ├── models.py            # Data classes
│   ├── agent.py             # Main monitoring loop (Watch + Poll)
│   ├── analyzer.py          # AWS Bedrock AI analysis
│   ├── remediation.py       # Auto-fix actions with safety guards
│   └── reporter.py          # Report generation (ConfigMap/S3)
├── k8s/
│   ├── serviceaccount.yaml
│   ├── clusterrole.yaml
│   ├── clusterrolebinding.yaml
│   ├── configmap.yaml
│   └── deployment.yaml
├── Dockerfile
└── requirements.txt

k8s-healing-dashboard/
├── src/
│   ├── __init__.py
│   ├── app.py               # FastAPI dashboard app
│   └── templates/
│       └── dashboard.html   # Dashboard UI
├── Dockerfile
└── requirements.txt
```

All source files are located at:
- Agent: `C:\Users\Acc User\k8s-self-healing-agent\src\`
- Dashboard: `C:\Users\Acc User\k8s-healing-dashboard\src\`
- K8s manifests: `C:\Users\Acc User\k8s-self-healing-agent\k8s\`

---

## Step 5: Build and Push Docker Images

```bash
# Login to ECR
aws ecr get-login-password --region ap-south-1 | \
  docker login --username AWS --password-stdin <ACCOUNT_ID>.dkr.ecr.ap-south-1.amazonaws.com

# Build and push Agent
cd k8s-self-healing-agent
docker build -t <ACCOUNT_ID>.dkr.ecr.ap-south-1.amazonaws.com/k8s-healing-agent:v1 .
docker push <ACCOUNT_ID>.dkr.ecr.ap-south-1.amazonaws.com/k8s-healing-agent:v1

# Build and push Dashboard
cd ../k8s-healing-dashboard
docker build -t <ACCOUNT_ID>.dkr.ecr.ap-south-1.amazonaws.com/k8s-healing-dashboard:v1 .
docker push <ACCOUNT_ID>.dkr.ecr.ap-south-1.amazonaws.com/k8s-healing-dashboard:v1
```

Replace `<ACCOUNT_ID>` with your AWS account ID.

---

## Step 6: Deploy Kubernetes Resources

### 6a. Service Accounts

```yaml
# File: k8s/serviceaccount.yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: k8s-healing-agent-sa
  namespace: <YOUR_NAMESPACE>
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: healing-dashboard-sa
  namespace: <YOUR_NAMESPACE>
```

### 6b. ClusterRole (RBAC Permissions)

```yaml
# File: k8s/clusterrole.yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: k8s-healing-agent-role
rules:
  # Read access for monitoring
  - apiGroups: [""]
    resources: ["pods", "pods/log", "events", "nodes", "namespaces", "configmaps"]
    verbs: ["get", "list", "watch"]
  # Write access for remediation
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["delete"]
  # For emitting report events
  - apiGroups: [""]
    resources: ["events"]
    verbs: ["create"]
  # For report storage
  - apiGroups: [""]
    resources: ["configmaps"]
    verbs: ["create", "update", "delete"]
  # For patching deployments (memory increase, rollback)
  - apiGroups: ["apps"]
    resources: ["deployments", "statefulsets", "replicasets", "daemonsets"]
    verbs: ["get", "list", "patch"]
```

### 6c. ClusterRoleBindings

```yaml
# File: k8s/clusterrolebinding.yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: k8s-healing-agent-binding
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: k8s-healing-agent-role
subjects:
  - kind: ServiceAccount
    name: k8s-healing-agent-sa
    namespace: <YOUR_NAMESPACE>
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: healing-dashboard-binding
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: k8s-healing-agent-role
subjects:
  - kind: ServiceAccount
    name: healing-dashboard-sa
    namespace: <YOUR_NAMESPACE>
```

### 6d. ConfigMap (Agent Configuration)

```yaml
# File: k8s/configmap.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: healing-agent-config
  namespace: <YOUR_NAMESPACE>
data:
  MONITOR_INTERVAL: "30"
  MONITOR_NAMESPACES: ""                                          # empty = all non-excluded
  EXCLUDE_NAMESPACES: "kube-system,kube-public,kube-node-lease"
  BEDROCK_REGION: "ap-south-1"
  BEDROCK_MODEL_ID: "anthropic.claude-3-haiku-20240307-v1:0"
  BEDROCK_ENDPOINT_URL: "https://bedrock-runtime.ap-south-1.amazonaws.com"  # auto-resolves to VPC endpoint
  BEDROCK_MAX_TOKENS: "4096"
  AUTO_REMEDIATE: "true"
  DRY_RUN: "true"                                                 # Start with true for safety
  CONFIDENCE_THRESHOLD: "0.7"
  OOM_MEMORY_INCREASE_PERCENT: "50"
  OOM_MEMORY_MAX_MI: "4096"
  REPORT_STORAGE: "configmap"
  REPORT_NAMESPACE: "<YOUR_NAMESPACE>"
  REPORT_RETENTION_DAYS: "30"
  LOG_LEVEL: "INFO"
```

### 6e. Deployments (Agent + Dashboard)

```yaml
# File: k8s/deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: k8s-healing-agent-depl
  namespace: <YOUR_NAMESPACE>
  labels:
    app: k8s-healing-agent
spec:
  replicas: 1
  selector:
    matchLabels:
      app: k8s-healing-agent
  strategy:
    type: Recreate
  template:
    metadata:
      labels:
        app: k8s-healing-agent
        self-healing: disabled        # Prevents agent from healing itself
    spec:
      serviceAccountName: k8s-healing-agent-sa
      containers:
        - name: healing-agent
          image: <ACCOUNT_ID>.dkr.ecr.ap-south-1.amazonaws.com/k8s-healing-agent:v1
          imagePullPolicy: Always
          envFrom:
            - configMapRef:
                name: healing-agent-config
          resources:
            requests:
              cpu: 100m
              memory: 256Mi
            limits:
              cpu: 500m
              memory: 512Mi
          livenessProbe:
            exec:
              command: ["python", "-c", "import os; exit(0)"]
            initialDelaySeconds: 30
            periodSeconds: 60
      restartPolicy: Always
---
apiVersion: v1
kind: Service
metadata:
  name: healing-dashboard-svc
  namespace: <YOUR_NAMESPACE>
spec:
  selector:
    app: healing-dashboard
  type: ClusterIP
  ports:
    - name: http
      port: 8080
      targetPort: 8080
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: healing-dashboard-depl
  namespace: <YOUR_NAMESPACE>
  labels:
    app: healing-dashboard
spec:
  replicas: 1
  selector:
    matchLabels:
      app: healing-dashboard
  template:
    metadata:
      labels:
        app: healing-dashboard
        self-healing: disabled
    spec:
      serviceAccountName: healing-dashboard-sa
      containers:
        - name: dashboard
          image: <ACCOUNT_ID>.dkr.ecr.ap-south-1.amazonaws.com/k8s-healing-dashboard:v1
          imagePullPolicy: Always
          ports:
            - containerPort: 8080
          env:
            - name: REPORT_NAMESPACE
              value: "<YOUR_NAMESPACE>"
            - name: EXCLUDE_NAMESPACES
              value: "kube-system,kube-public,kube-node-lease"
            - name: BEDROCK_REGION
              value: "ap-south-1"
            - name: BEDROCK_MODEL_ID
              value: "anthropic.claude-3-haiku-20240307-v1:0"
            - name: BEDROCK_ENDPOINT_URL
              value: "https://bedrock-runtime.ap-south-1.amazonaws.com"
          resources:
            requests:
              cpu: 50m
              memory: 128Mi
            limits:
              cpu: 200m
              memory: 256Mi
          livenessProbe:
            httpGet:
              path: /health
              port: 8080
            initialDelaySeconds: 10
            periodSeconds: 30
          readinessProbe:
            httpGet:
              path: /health
              port: 8080
            initialDelaySeconds: 5
            periodSeconds: 10
      restartPolicy: Always
```

### Apply All Resources

```bash
# Replace <YOUR_NAMESPACE> and <ACCOUNT_ID> in all files first

kubectl apply -f k8s/serviceaccount.yaml
kubectl apply -f k8s/clusterrole.yaml
kubectl apply -f k8s/clusterrolebinding.yaml
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/deployment.yaml
```

---

## Step 7: Expose Dashboard via Ingress

### Ingress Resource

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: healing-dashboard-ingress
  namespace: <YOUR_NAMESPACE>
spec:
  ingressClassName: nginx
  rules:
  - host: healing.<YOUR_DOMAIN>
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: healing-dashboard-svc
            port:
              number: 8080
```

### Add DNS Record (Route 53)

```bash
aws route53 change-resource-record-sets \
  --hosted-zone-id <HOSTED_ZONE_ID> \
  --change-batch '{
    "Changes": [{
      "Action": "UPSERT",
      "ResourceRecordSet": {
        "Name": "healing.<YOUR_DOMAIN>",
        "Type": "CNAME",
        "TTL": 300,
        "ResourceRecords": [{"Value": "<YOUR_NLB_DNS>"}]
      }
    }]
  }'
```

### Console (Route 53)

1. Go to **Route 53** > **Hosted zones** > Select your domain
2. **Create record**
3. Record name: `healing`
4. Record type: CNAME
5. Value: Your NLB DNS name
6. TTL: 300
7. Create

---

## Step 8: Verify Deployment

```bash
# 1. Check pods are running
kubectl get pods -n <YOUR_NAMESPACE> -l "app in (k8s-healing-agent, healing-dashboard)"

# Expected output:
# NAME                                      READY   STATUS    RESTARTS   AGE
# k8s-healing-agent-depl-xxx                1/1     Running   0          1m
# healing-dashboard-depl-xxx                1/1     Running   0          1m

# 2. Check agent is detecting issues
kubectl logs -n <YOUR_NAMESPACE> -l app=k8s-healing-agent --tail=20

# Expected: Lines showing "Issue detected", "Analysis", "Remediation"

# 3. Check dashboard health
curl -sk https://healing.<YOUR_DOMAIN>/health
# Expected: {"status":"healthy"}

# 4. Check reports are being generated
kubectl get configmaps -n <YOUR_NAMESPACE> -l app=k8s-healing-agent,type=healing-report

# 5. Read a specific report
kubectl get configmap healing-report-<ID> -n <YOUR_NAMESPACE> -o jsonpath='{.data.report\.md}'
```

---

## Step 9: Switch to Active Remediation

After verifying the agent detects issues correctly in dry-run mode:

```bash
# Enable active remediation
kubectl patch configmap healing-agent-config -n <YOUR_NAMESPACE> \
  --type='merge' -p='{"data":{"DRY_RUN":"false"}}'

# Restart the agent to pick up the change
kubectl rollout restart deployment k8s-healing-agent-depl -n <YOUR_NAMESPACE>

# Verify it's actively fixing issues
kubectl logs -n <YOUR_NAMESPACE> -l app=k8s-healing-agent --tail=20 | grep -E "Deleted pod|Increased memory|Rolled back"
```

---

## Step 10: Verify Private Environment

Confirm Bedrock works via VPC Endpoint without internet:

```bash
kubectl exec -it <AGENT_POD> -n <YOUR_NAMESPACE> -- python3 -c "
import boto3, json
client = boto3.client('bedrock-runtime', region_name='ap-south-1')
resp = client.invoke_model(
    modelId='anthropic.claude-3-haiku-20240307-v1:0',
    body=json.dumps({
        'anthropic_version': 'bedrock-2023-05-31',
        'max_tokens': 100,
        'messages': [{'role': 'user', 'content': 'Say hello'}]
    }),
    contentType='application/json',
    accept='application/json'
)
print(json.loads(resp['body'].read())['content'][0]['text'])
"
```

If this returns a response, Bedrock is working privately via VPC Endpoint.

### How Private DNS Works

When `--private-dns-enabled` is set on the VPC Endpoint:
- The standard endpoint `bedrock-runtime.ap-south-1.amazonaws.com` automatically resolves to the VPC Endpoint's private IP within your VPC
- No code changes needed — boto3 uses the standard endpoint URL
- Traffic never leaves the VPC
- Even if NAT Gateway is removed, Bedrock calls continue to work

---

## Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `MONITOR_INTERVAL` | 30 | Seconds between full cluster scans |
| `MONITOR_NAMESPACES` | "" (all) | Comma-separated namespaces to monitor. Empty = all |
| `EXCLUDE_NAMESPACES` | kube-system,kube-public,kube-node-lease | Namespaces to skip |
| `BEDROCK_REGION` | ap-south-1 | AWS region for Bedrock |
| `BEDROCK_MODEL_ID` | anthropic.claude-3-haiku-20240307-v1:0 | Bedrock model to use |
| `BEDROCK_ENDPOINT_URL` | "" | VPC Endpoint URL. Empty = public endpoint |
| `BEDROCK_MAX_TOKENS` | 4096 | Max tokens for AI response |
| `AUTO_REMEDIATE` | true | Enable/disable auto-fix actions |
| `DRY_RUN` | true | Log actions without executing them |
| `CONFIDENCE_THRESHOLD` | 0.7 | Minimum AI confidence to auto-fix (0.0-1.0) |
| `OOM_MEMORY_INCREASE_PERCENT` | 50 | Percentage to increase memory on OOMKill |
| `OOM_MEMORY_MAX_MI` | 4096 | Maximum memory limit cap in Mi |
| `MAX_ROLLBACK_REVISIONS` | 3 | Max revisions to look back for rollback |
| `CRASHLOOP_RESTART_THRESHOLD` | 5 | Min restarts before acting on CrashLoop |
| `DEDUP_COOLDOWN_SECONDS` | 600 | Seconds before re-processing same issue |
| `REPORT_STORAGE` | configmap | Report backend: "configmap" or "s3" |
| `REPORT_S3_BUCKET` | "" | S3 bucket for reports (if using s3 storage) |
| `REPORT_NAMESPACE` | default | Namespace where report ConfigMaps are stored |
| `REPORT_RETENTION_DAYS` | 30 | Days to keep old reports |
| `LOG_LEVEL` | INFO | Logging level: DEBUG, INFO, WARNING, ERROR |

---

## Agent Capabilities

### Auto-Remediation Actions

| Issue Detected | Auto-Fix Action | How It Works |
|---|---|---|
| **OOMKilled** | Increase memory limit | Patches Deployment with 50% more memory (capped at 4Gi) |
| **CrashLoopBackOff** | Restart pod or rollback | Deletes pod for controller to recreate, or patches previous revision |
| **Error** (Exit Code 1) | Restart pod | Deletes pod for controller to recreate |
| **ImagePullBackOff** | Report only | Flags for human review (image name change too risky) |
| **Pending** (no nodes) | Report only | Flags for human review (requires infrastructure scaling) |

### Detection Logic

| K8s API Field | Detected State |
|---|---|
| `container.state.waiting.reason == "CrashLoopBackOff"` | CrashLoopBackOff |
| `container.state.waiting.reason == "ImagePullBackOff"` or `"ErrImagePull"` | ImagePullBackOff |
| `container.state.terminated.reason == "OOMKilled"` | OOMKilled |
| `container.state.terminated.reason == "Error"` | Error |
| `pod.status.phase == "Pending"` + scheduling failure conditions | Pending |
| `container.last_state.terminated.reason == "OOMKilled"` | OOMKilled (previous crash) |

---

## Safety Mechanisms

| Mechanism | Description |
|---|---|
| **Dry-run mode** | Default ON. Logs what would happen without executing |
| **Confidence threshold** | Only auto-fixes when AI confidence >= 70% |
| **Protected namespaces** | Never touches kube-system, kube-public, kube-node-lease |
| **Self-healing disabled label** | Agent pod has `self-healing: disabled` to prevent recursive modification |
| **Memory cap** | OOM fix never exceeds 4Gi (configurable) |
| **Dedup cooldown** | Won't re-process same pod/issue within 10 minutes |
| **Owner-aware** | Only patches pods owned by Deployments/StatefulSets. Bare pods are report-only |
| **Rollback depth limit** | Maximum 3 revisions back for rollback |

---

## Dashboard Features

### Tabs

| Tab | Description |
|---|---|
| **Issues** | All unhealthy pods sorted by severity (critical first). Each has a "Resolve" button |
| **AI Reports** | Bedrock-generated root cause analysis with actions taken. Click "View" for full report |
| **All Pods** | Complete pod list across all namespaces with status |

### Features

- **Stats cards** — Total, Healthy, Unhealthy, Critical, Warning pod counts
- **Namespace filter** — Filter by specific namespace
- **Color-coded badges** — CrashLoopBackOff (red), Pending (yellow), OOMKilled (red), Running (green)
- **One-click Resolve** — Click "Resolve" on any unhealthy pod to trigger AI analysis + auto-fix
- **Report viewer** — Full markdown reports with root cause, action, prevention steps
- **Auto-refresh** — Dashboard refreshes every 30 seconds
- **API endpoints** — `/api/pods`, `/api/reports`, `/api/report/{id}`, `/api/resolve` for integration

### Dashboard API

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | Dashboard HTML page |
| `/api/pods` | GET | List all pods (optional: `?namespace=xxx`) |
| `/api/reports` | GET | List all healing reports |
| `/api/report/{id}` | GET | Get specific report content |
| `/api/resolve` | POST | Trigger AI analysis + remediation for a pod |
| `/health` | GET | Health check |

---

## Roadmap

### Phase 1 — MVP (Completed)

- Real-time pod monitoring (Watch + Poll)
- AI-powered root cause analysis via Bedrock
- Auto-remediation (restart, memory increase, rollback)
- Web dashboard with namespace filtering
- One-click "Resolve" button for manual trigger
- Private environment support (VPC Endpoint)
- Report generation with prevention steps
- Safety guards (dry-run, confidence threshold, protected namespaces)

### Phase 2 — Enhanced Intelligence

- Slack/Teams alert notifications on detection and resolution
- Predictive failure detection using metrics trends
- Root cause correlation (link related failures as single incident)
- Custom remediation runbooks
- Historical trends dashboard

### Phase 3 — Enterprise

- Multi-cluster monitoring from single agent
- Cost optimization recommendations (right-sizing, SPOT vs on-demand)
- Compliance and audit report generation
- Self-learning from past resolutions
- Role-based dashboard access (view-only vs resolve permission)

---

## ROI Estimate

| Metric | Before (Manual) | After (Agent) |
|---|---|---|
| **Mean Time to Resolution** | 30-60 minutes | < 2 minutes |
| **On-call incidents per week** | 10-15 | 2-3 (only complex ones) |
| **Engineer hours on K8s ops** | 20 hrs/week | 5 hrs/week |
| **Incident documentation** | Manual / often skipped | 100% automated |
| **Common issue resolution** | Human intervention | Fully automated 24/7 |

---

## Troubleshooting

### Agent pod is CrashLoopBackOff

```bash
kubectl logs -n <NAMESPACE> -l app=k8s-healing-agent --tail=30
```
Common causes:
- Bedrock model not enabled (check Step 3)
- VPC Endpoint not available (check Step 2)
- RBAC permissions missing (check Step 6b)

### Dashboard shows "No reports"

Reports are stored in the namespace defined by `REPORT_NAMESPACE`. Ensure:
```bash
kubectl get configmaps -n <REPORT_NAMESPACE> -l app=k8s-healing-agent
```

### Bedrock returns "AccessDenied"

- Ensure model is enabled in Bedrock console (Step 3)
- Ensure node IAM role or IRSA has `bedrock:InvokeModel` permission
- Ensure VPC Endpoint SG allows 443 from EKS cluster SG

### Agent not detecting issues

Check agent logs:
```bash
kubectl logs -n <NAMESPACE> -l app=k8s-healing-agent --tail=50
```
Verify `MONITOR_NAMESPACES` and `EXCLUDE_NAMESPACES` in ConfigMap.
