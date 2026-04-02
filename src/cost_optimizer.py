import os
import json
import logging
import re
from datetime import datetime, timezone
from dataclasses import dataclass, field

import boto3
from kubernetes import client as k8s_client

from . import config

logger = logging.getLogger(__name__)


@dataclass
class PodResourceUsage:
    name: str
    namespace: str
    container: str
    cpu_request: str
    cpu_limit: str
    memory_request: str
    memory_limit: str
    cpu_usage_m: int
    memory_usage_mi: int
    cpu_utilization_pct: float
    memory_utilization_pct: float
    owner_kind: str
    owner_name: str


@dataclass
class NodeCost:
    name: str
    instance_type: str
    capacity_type: str  # SPOT or ON_DEMAND
    node_group: str
    cpu_capacity: int
    memory_capacity_mi: int
    cpu_allocated_pct: float
    memory_allocated_pct: float
    pod_count: int
    max_pods: int
    estimated_hourly_cost: float


@dataclass
class CostReport:
    timestamp: datetime
    cluster_name: str
    total_nodes: int
    total_pods: int
    estimated_monthly_cost: float
    potential_savings: float
    oversized_pods: list
    idle_pods: list
    node_recommendations: list
    spot_recommendations: list
    right_size_recommendations: list
    ai_summary: str
    report_id: str = ""


class CostOptimizer:
    def __init__(self, core_v1: k8s_client.CoreV1Api, apps_v1: k8s_client.AppsV1Api):
        self.core_v1 = core_v1
        self.apps_v1 = apps_v1
        self.metrics_available = self._check_metrics()

        bedrock_kwargs = {
            "service_name": "bedrock-runtime",
            "region_name": config.BEDROCK_REGION,
        }
        if config.BEDROCK_ENDPOINT_URL:
            bedrock_kwargs["endpoint_url"] = config.BEDROCK_ENDPOINT_URL
        self.bedrock = boto3.client(**bedrock_kwargs)

    def _check_metrics(self) -> bool:
        try:
            api = k8s_client.CustomObjectsApi()
            api.list_cluster_custom_object("metrics.k8s.io", "v1beta1", "nodes")
            return True
        except Exception:
            return False

    def analyze(self) -> CostReport:
        """Run full cost analysis."""
        logger.info("Starting cost optimization analysis...")

        pod_usage = self._get_pod_usage()
        node_costs = self._get_node_costs()

        oversized = self._find_oversized_pods(pod_usage)
        idle = self._find_idle_pods(pod_usage)
        node_recs = self._node_recommendations(node_costs)
        spot_recs = self._spot_recommendations(node_costs)
        right_size = self._right_size_recommendations(pod_usage)

        total_monthly = sum(n.estimated_hourly_cost * 730 for n in node_costs)
        potential_savings = self._calculate_savings(oversized, idle, node_recs, spot_recs)

        ai_summary = self._get_ai_summary(
            pod_usage, node_costs, oversized, idle, node_recs, spot_recs, right_size, total_monthly
        )

        import uuid
        report = CostReport(
            timestamp=datetime.now(timezone.utc),
            cluster_name=os.getenv("CLUSTER_NAME", "unknown"),
            total_nodes=len(node_costs),
            total_pods=len(pod_usage),
            estimated_monthly_cost=round(total_monthly, 2),
            potential_savings=round(potential_savings, 2),
            oversized_pods=oversized,
            idle_pods=idle,
            node_recommendations=node_recs,
            spot_recommendations=spot_recs,
            right_size_recommendations=right_size,
            ai_summary=ai_summary,
            report_id=str(uuid.uuid4())[:8],
        )

        logger.info(f"Cost analysis complete: ${total_monthly:.0f}/mo, potential savings: ${potential_savings:.0f}/mo")
        return report

    def _get_pod_usage(self) -> list[PodResourceUsage]:
        """Get resource usage for all pods."""
        pods_usage = []
        exclude = config.EXCLUDE_NAMESPACES + config.PROTECTED_NAMESPACES

        try:
            if self.metrics_available:
                metrics_api = k8s_client.CustomObjectsApi()
                pod_metrics = metrics_api.list_cluster_custom_object("metrics.k8s.io", "v1beta1", "pods")
                metrics_map = {}
                for item in pod_metrics.get("items", []):
                    key = f"{item['metadata']['namespace']}/{item['metadata']['name']}"
                    for c in item.get("containers", []):
                        cpu_str = c["usage"].get("cpu", "0")
                        mem_str = c["usage"].get("memory", "0")
                        metrics_map[f"{key}/{c['name']}"] = {
                            "cpu_m": self._parse_cpu_m(cpu_str),
                            "memory_mi": self._parse_memory_mi(mem_str),
                        }
            else:
                metrics_map = {}

            all_pods = self.core_v1.list_pod_for_all_namespaces()
            for pod in all_pods.items:
                if pod.metadata.namespace in exclude:
                    continue
                if pod.status.phase != "Running":
                    continue

                owner_kind, owner_name = self._get_owner(pod)

                for container in pod.spec.containers:
                    req = container.resources.requests or {} if container.resources else {}
                    lim = container.resources.limits or {} if container.resources else {}

                    cpu_req = req.get("cpu", "0")
                    mem_req = req.get("memory", "0")
                    cpu_lim = lim.get("cpu", "0")
                    mem_lim = lim.get("memory", "0")

                    key = f"{pod.metadata.namespace}/{pod.metadata.name}/{container.name}"
                    usage = metrics_map.get(key, {"cpu_m": 0, "memory_mi": 0})

                    cpu_req_m = self._parse_cpu_m(cpu_req)
                    mem_req_mi = self._parse_memory_mi(mem_req)

                    cpu_util = (usage["cpu_m"] / cpu_req_m * 100) if cpu_req_m > 0 else 0
                    mem_util = (usage["memory_mi"] / mem_req_mi * 100) if mem_req_mi > 0 else 0

                    pods_usage.append(PodResourceUsage(
                        name=pod.metadata.name,
                        namespace=pod.metadata.namespace,
                        container=container.name,
                        cpu_request=cpu_req,
                        cpu_limit=cpu_lim,
                        memory_request=mem_req,
                        memory_limit=mem_lim,
                        cpu_usage_m=usage["cpu_m"],
                        memory_usage_mi=usage["memory_mi"],
                        cpu_utilization_pct=round(cpu_util, 1),
                        memory_utilization_pct=round(mem_util, 1),
                        owner_kind=owner_kind or "N/A",
                        owner_name=owner_name or "N/A",
                    ))
        except Exception as e:
            logger.error(f"Error getting pod usage: {e}")

        return pods_usage

    def _get_node_costs(self) -> list[NodeCost]:
        """Get node cost estimates."""
        nodes = []
        spot_prices = {
            "m6a.large": 0.031, "m6a.xlarge": 0.064, "r6a.large": 0.032,
            "m5.large": 0.035, "m5.xlarge": 0.070, "t3.medium": 0.015,
            "t3.large": 0.030, "c6a.large": 0.028, "c6a.xlarge": 0.056,
        }
        ondemand_prices = {
            "m6a.large": 0.086, "m6a.xlarge": 0.173, "r6a.large": 0.113,
            "m5.large": 0.096, "m5.xlarge": 0.192, "t3.medium": 0.042,
            "t3.large": 0.083, "c6a.large": 0.077, "c6a.xlarge": 0.153,
        }

        try:
            node_list = self.core_v1.list_node()
            for node in node_list.items:
                labels = node.metadata.labels or {}
                instance_type = labels.get("node.kubernetes.io/instance-type", "unknown")
                capacity_type = "SPOT" if labels.get("eks.amazonaws.com/capacityType") == "SPOT" else "ON_DEMAND"
                node_group = labels.get("eks.amazonaws.com/nodegroup", "unknown")

                cpu_cap = int(node.status.capacity.get("cpu", "0"))
                mem_cap = self._parse_memory_mi(node.status.capacity.get("memory", "0"))
                max_pods = int(node.status.capacity.get("pods", "0"))

                # Get allocated resources
                cpu_alloc_pct = 0
                mem_alloc_pct = 0
                pod_count = 0
                field_selector = f"spec.nodeName={node.metadata.name},status.phase=Running"
                try:
                    pods = self.core_v1.list_pod_for_all_namespaces(field_selector=field_selector)
                    pod_count = len(pods.items)
                    total_cpu_req = 0
                    total_mem_req = 0
                    for pod in pods.items:
                        for c in pod.spec.containers:
                            if c.resources and c.resources.requests:
                                total_cpu_req += self._parse_cpu_m(c.resources.requests.get("cpu", "0"))
                                total_mem_req += self._parse_memory_mi(c.resources.requests.get("memory", "0"))
                    cpu_alloc_pct = (total_cpu_req / (cpu_cap * 1000) * 100) if cpu_cap > 0 else 0
                    mem_alloc_pct = (total_mem_req / mem_cap * 100) if mem_cap > 0 else 0
                except Exception:
                    pass

                price_map = spot_prices if capacity_type == "SPOT" else ondemand_prices
                hourly_cost = price_map.get(instance_type, 0.05)

                nodes.append(NodeCost(
                    name=node.metadata.name,
                    instance_type=instance_type,
                    capacity_type=capacity_type,
                    node_group=node_group,
                    cpu_capacity=cpu_cap,
                    memory_capacity_mi=mem_cap,
                    cpu_allocated_pct=round(cpu_alloc_pct, 1),
                    memory_allocated_pct=round(mem_alloc_pct, 1),
                    pod_count=pod_count,
                    max_pods=max_pods,
                    estimated_hourly_cost=hourly_cost,
                ))
        except Exception as e:
            logger.error(f"Error getting node costs: {e}")

        return nodes

    def _find_oversized_pods(self, pods: list[PodResourceUsage]) -> list[dict]:
        """Find pods using less than 20% of requested resources."""
        oversized = []
        for p in pods:
            if p.cpu_utilization_pct > 0 and p.cpu_utilization_pct < 20 and p.memory_utilization_pct < 30:
                oversized.append({
                    "pod": f"{p.namespace}/{p.name}",
                    "container": p.container,
                    "cpu_request": p.cpu_request,
                    "cpu_usage": f"{p.cpu_usage_m}m",
                    "cpu_util": f"{p.cpu_utilization_pct}%",
                    "memory_request": p.memory_request,
                    "memory_usage": f"{p.memory_usage_mi}Mi",
                    "memory_util": f"{p.memory_utilization_pct}%",
                    "recommendation": f"Reduce CPU request to {max(10, p.cpu_usage_m * 2)}m, memory to {max(64, p.memory_usage_mi * 2)}Mi",
                })
        return oversized[:20]

    def _find_idle_pods(self, pods: list[PodResourceUsage]) -> list[dict]:
        """Find pods with near-zero usage."""
        idle = []
        for p in pods:
            if p.cpu_usage_m <= 1 and p.memory_usage_mi <= 10 and self.metrics_available:
                idle.append({
                    "pod": f"{p.namespace}/{p.name}",
                    "container": p.container,
                    "cpu_usage": f"{p.cpu_usage_m}m",
                    "memory_usage": f"{p.memory_usage_mi}Mi",
                    "owner": f"{p.owner_kind}/{p.owner_name}",
                    "recommendation": "Consider scaling to 0 or removing if unused",
                })
        return idle[:20]

    def _node_recommendations(self, nodes: list[NodeCost]) -> list[dict]:
        """Recommend node optimizations."""
        recs = []
        for n in nodes:
            if n.cpu_allocated_pct < 30 and n.memory_allocated_pct < 30:
                recs.append({
                    "node": n.name,
                    "instance_type": n.instance_type,
                    "cpu_allocated": f"{n.cpu_allocated_pct}%",
                    "memory_allocated": f"{n.memory_allocated_pct}%",
                    "pods": n.pod_count,
                    "recommendation": "Node underutilized. Consider consolidating workloads and reducing node count.",
                    "potential_savings": f"${n.estimated_hourly_cost * 730:.0f}/mo",
                })
            elif n.cpu_allocated_pct > 85 or n.memory_allocated_pct > 85:
                recs.append({
                    "node": n.name,
                    "instance_type": n.instance_type,
                    "cpu_allocated": f"{n.cpu_allocated_pct}%",
                    "memory_allocated": f"{n.memory_allocated_pct}%",
                    "pods": n.pod_count,
                    "recommendation": "Node near capacity. Consider adding nodes or right-sizing pods.",
                    "potential_savings": "$0",
                })
        return recs

    def _spot_recommendations(self, nodes: list[NodeCost]) -> list[dict]:
        """Recommend SPOT conversion for on-demand nodes."""
        recs = []
        for n in nodes:
            if n.capacity_type == "ON_DEMAND":
                spot_prices = {"m6a.large": 0.031, "m6a.xlarge": 0.064, "r6a.large": 0.032}
                spot_price = spot_prices.get(n.instance_type, n.estimated_hourly_cost * 0.4)
                savings = (n.estimated_hourly_cost - spot_price) * 730
                recs.append({
                    "node": n.name,
                    "node_group": n.node_group,
                    "instance_type": n.instance_type,
                    "current_cost": f"${n.estimated_hourly_cost * 730:.0f}/mo",
                    "spot_cost": f"${spot_price * 730:.0f}/mo",
                    "savings": f"${savings:.0f}/mo",
                    "recommendation": f"Switch to SPOT instance. Save ~${savings:.0f}/month (~{savings/(n.estimated_hourly_cost*730)*100:.0f}% reduction)",
                })
        return recs

    def _right_size_recommendations(self, pods: list[PodResourceUsage]) -> list[dict]:
        """Recommend right-sizing based on actual usage."""
        recs = []
        seen = set()
        for p in pods:
            key = f"{p.owner_kind}/{p.owner_name}"
            if key in seen or p.owner_kind == "N/A":
                continue

            if p.cpu_utilization_pct > 0 and p.cpu_utilization_pct < 30:
                cpu_req_m = self._parse_cpu_m(p.cpu_request)
                new_cpu = max(10, int(p.cpu_usage_m * 2.5))
                if new_cpu < cpu_req_m * 0.7:
                    seen.add(key)
                    recs.append({
                        "workload": key,
                        "namespace": p.namespace,
                        "current_cpu": p.cpu_request,
                        "actual_usage": f"{p.cpu_usage_m}m",
                        "recommended_cpu": f"{new_cpu}m",
                        "current_memory": p.memory_request,
                        "actual_memory": f"{p.memory_usage_mi}Mi",
                        "recommended_memory": f"{max(64, int(p.memory_usage_mi * 1.5))}Mi",
                    })
        return recs[:20]

    def _calculate_savings(self, oversized, idle, node_recs, spot_recs) -> float:
        savings = 0
        for rec in spot_recs:
            val = re.search(r'\$(\d+)', rec.get("savings", "$0"))
            if val:
                savings += int(val.group(1))
        for rec in node_recs:
            val = re.search(r'\$(\d+)', rec.get("potential_savings", "$0"))
            if val:
                savings += int(val.group(1))
        return savings

    def _get_ai_summary(self, pods, nodes, oversized, idle, node_recs, spot_recs, right_size, monthly_cost) -> str:
        """Get AI-generated cost optimization summary."""
        try:
            prompt = f"""You are a Kubernetes cost optimization expert. Analyze this cluster and provide actionable recommendations.

## Cluster Overview
- Total Nodes: {len(nodes)}
- Total Pods: {len(pods)}
- Estimated Monthly Cost: ${monthly_cost:.0f}
- Oversized Pods: {len(oversized)}
- Idle Pods: {len(idle)}

## Node Details
{json.dumps([{{"node": n.name, "type": n.instance_type, "capacity": n.capacity_type, "cpu_alloc": f"{n.cpu_allocated_pct}%", "mem_alloc": f"{n.memory_allocated_pct}%", "pods": n.pod_count}} for n in nodes[:10]], indent=2)}

## Top Oversized Pods
{json.dumps(oversized[:5], indent=2)}

## SPOT Conversion Opportunities
{json.dumps(spot_recs[:5], indent=2)}

## Right-Size Recommendations
{json.dumps(right_size[:5], indent=2)}

Provide a concise executive summary (3-5 bullet points) with:
1. Current cost assessment
2. Top 3 immediate savings opportunities with dollar amounts
3. Long-term optimization strategy
4. Risk considerations

Respond in plain text, no JSON."""

            body = json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
            })

            response = self.bedrock.invoke_model(
                modelId=config.BEDROCK_MODEL_ID,
                body=body,
                contentType="application/json",
                accept="application/json",
            )
            result = json.loads(response["body"].read())
            return result["content"][0]["text"]
        except Exception as e:
            logger.error(f"AI summary failed: {e}")
            return f"AI summary unavailable: {e}"

    def _get_owner(self, pod):
        if pod.metadata.owner_references:
            for ref in pod.metadata.owner_references:
                if ref.kind == "ReplicaSet":
                    try:
                        rs = self.apps_v1.read_namespaced_replica_set(ref.name, pod.metadata.namespace)
                        if rs.metadata.owner_references:
                            for rs_ref in rs.metadata.owner_references:
                                if rs_ref.kind == "Deployment":
                                    return "Deployment", rs_ref.name
                    except Exception:
                        pass
                return ref.kind, ref.name
        return None, None

    def _parse_cpu_m(self, value: str) -> int:
        value = str(value)
        if value.endswith("n"):
            return max(1, int(int(value[:-1]) / 1_000_000))
        if value.endswith("u"):
            return max(1, int(int(value[:-1]) / 1_000))
        if value.endswith("m"):
            return int(value[:-1])
        try:
            return int(float(value) * 1000)
        except ValueError:
            return 0

    def _parse_memory_mi(self, value: str) -> int:
        value = str(value)
        if value.endswith("Gi"):
            return int(float(value[:-2]) * 1024)
        if value.endswith("Mi"):
            return int(float(value[:-2]))
        if value.endswith("Ki"):
            return max(1, int(float(value[:-2]) / 1024))
        try:
            return max(1, int(int(value) / (1024 * 1024)))
        except ValueError:
            return 0

    def format_report(self, report: CostReport) -> str:
        oversized_text = "\n".join(
            f"  - {o['pod']}: CPU {o['cpu_util']} used ({o['cpu_request']} requested) | {o['recommendation']}"
            for o in report.oversized_pods[:10]
        ) or "  None found"

        idle_text = "\n".join(
            f"  - {i['pod']}: CPU {i['cpu_usage']}, Memory {i['memory_usage']} | {i['recommendation']}"
            for i in report.idle_pods[:10]
        ) or "  None found"

        node_text = "\n".join(
            f"  - {r['node']} ({r['instance_type']}): CPU {r['cpu_allocated']}, Memory {r['memory_allocated']} | {r['recommendation']}"
            for r in report.node_recommendations[:10]
        ) or "  All nodes optimally utilized"

        spot_text = "\n".join(
            f"  - {r['node_group']} ({r['instance_type']}): {r['current_cost']} -> {r['spot_cost']} | {r['recommendation']}"
            for r in report.spot_recommendations[:10]
        ) or "  All nodes already on SPOT"

        right_size_text = "\n".join(
            f"  - {r['workload']}: CPU {r['current_cpu']} -> {r['recommended_cpu']}, Memory {r['current_memory']} -> {r['recommended_memory']}"
            for r in report.right_size_recommendations[:10]
        ) or "  All workloads properly sized"

        return f"""## Cost Optimization Report: {report.report_id}
- **Timestamp**: {report.timestamp.isoformat()}Z
- **Cluster**: {report.cluster_name}
- **Total Nodes**: {report.total_nodes}
- **Total Pods**: {report.total_pods}
- **Estimated Monthly Cost**: ${report.estimated_monthly_cost}
- **Potential Monthly Savings**: ${report.potential_savings}

### AI Executive Summary
{report.ai_summary}

### Oversized Pods (using < 20% of requested resources)
{oversized_text}

### Idle Pods (near-zero usage)
{idle_text}

### Node Recommendations
{node_text}

### SPOT Conversion Opportunities
{spot_text}

### Right-Size Recommendations
{right_size_text}
"""
