"""
Node Optimizer - Replaces expensive nodes with better alternatives
- Analyzes current node groups: instance type, capacity type, cost
- Finds cheaper alternatives (Spot, different instance family)
- Can auto-execute: create new node group, migrate pods, delete old group
- Supports mixed instance policy (Spot + On-demand for reliability)
"""

import os
import json
import logging
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass

import boto3
from kubernetes import client as k8s_client

from . import config

logger = logging.getLogger(__name__)

SPOT_PRICES = {
    "t3.medium": 0.015, "t3.large": 0.030, "t3.xlarge": 0.060,
    "m6a.large": 0.031, "m6a.xlarge": 0.064, "m6a.2xlarge": 0.128,
    "m5.large": 0.035, "m5.xlarge": 0.070,
    "c6a.large": 0.028, "c6a.xlarge": 0.056,
    "r6a.large": 0.032, "r6a.xlarge": 0.065,
}

ONDEMAND_PRICES = {
    "t3.medium": 0.042, "t3.large": 0.083, "t3.xlarge": 0.166,
    "m6a.large": 0.086, "m6a.xlarge": 0.173, "m6a.2xlarge": 0.346,
    "m5.large": 0.096, "m5.xlarge": 0.192,
    "c6a.large": 0.077, "c6a.xlarge": 0.153,
    "r6a.large": 0.113, "r6a.xlarge": 0.226,
}

INSTANCE_SPECS = {
    "t3.medium": {"vcpu": 2, "mem_gb": 4}, "t3.large": {"vcpu": 2, "mem_gb": 8},
    "t3.xlarge": {"vcpu": 4, "mem_gb": 16},
    "m6a.large": {"vcpu": 2, "mem_gb": 8}, "m6a.xlarge": {"vcpu": 4, "mem_gb": 16},
    "m6a.2xlarge": {"vcpu": 8, "mem_gb": 32},
    "m5.large": {"vcpu": 2, "mem_gb": 8}, "m5.xlarge": {"vcpu": 4, "mem_gb": 16},
    "c6a.large": {"vcpu": 2, "mem_gb": 4}, "c6a.xlarge": {"vcpu": 4, "mem_gb": 8},
    "r6a.large": {"vcpu": 2, "mem_gb": 16}, "r6a.xlarge": {"vcpu": 4, "mem_gb": 32},
}


@dataclass
class NodeGroupAnalysis:
    name: str
    instance_type: str
    capacity_type: str
    node_count: int
    desired: int
    min_size: int
    max_size: int
    hourly_cost: float
    monthly_cost: float
    cpu_capacity: int
    memory_gb: int
    avg_cpu_util: float
    avg_mem_util: float


@dataclass
class Replacement:
    current_group: str
    current_type: str
    current_capacity: str
    current_monthly: float
    recommended_type: str
    recommended_capacity: str
    recommended_monthly: float
    savings_monthly: float
    savings_percent: float
    reason: str
    risk: str
    auto_executable: bool


class NodeOptimizer:
    def __init__(self, core_v1: k8s_client.CoreV1Api):
        self.core_v1 = core_v1
        self.region = os.getenv("AWS_REGION", config.BEDROCK_REGION)
        self.cluster_name = os.getenv("CLUSTER_NAME", "")
        self.eks = boto3.client("eks", region_name=self.region)
        self.ec2 = boto3.client("ec2", region_name=self.region)

        bedrock_kwargs = {"service_name": "bedrock-runtime", "region_name": config.BEDROCK_REGION}
        if config.BEDROCK_ENDPOINT_URL:
            bedrock_kwargs["endpoint_url"] = config.BEDROCK_ENDPOINT_URL
        self.bedrock = boto3.client(**bedrock_kwargs)

    def analyze_and_recommend(self) -> dict:
        """Full node optimization analysis with replacement recommendations."""
        logger.info("Starting node optimization analysis...")

        groups = self._analyze_node_groups()
        replacements = []

        for group in groups:
            recs = self._find_replacements(group)
            replacements.extend(recs)

        replacements.sort(key=lambda r: -r.savings_monthly)

        total_current = sum(g.monthly_cost for g in groups)
        total_savings = sum(r.savings_monthly for r in replacements)

        ai_plan = self._get_ai_migration_plan(groups, replacements, total_current)

        report_id = str(uuid.uuid4())[:8]

        result = {
            "report_id": report_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "cluster": self.cluster_name,
            "current_monthly_cost": round(total_current, 2),
            "potential_savings": round(total_savings, 2),
            "optimized_monthly_cost": round(total_current - total_savings, 2),
            "node_groups": [
                {
                    "name": g.name,
                    "instance_type": g.instance_type,
                    "capacity_type": g.capacity_type,
                    "nodes": g.node_count,
                    "monthly_cost": f"${g.monthly_cost:.0f}",
                    "avg_cpu_util": f"{g.avg_cpu_util:.0f}%",
                    "avg_mem_util": f"{g.avg_mem_util:.0f}%",
                }
                for g in groups
            ],
            "replacements": [
                {
                    "current_group": r.current_group,
                    "current": f"{r.current_type} ({r.current_capacity})",
                    "recommended": f"{r.recommended_type} ({r.recommended_capacity})",
                    "current_cost": f"${r.current_monthly:.0f}/mo",
                    "new_cost": f"${r.recommended_monthly:.0f}/mo",
                    "savings": f"${r.savings_monthly:.0f}/mo ({r.savings_percent:.0f}%)",
                    "reason": r.reason,
                    "risk": r.risk,
                    "auto_executable": r.auto_executable,
                }
                for r in replacements
            ],
            "ai_migration_plan": ai_plan,
        }

        # Store report
        self._store_report(report_id, result)
        logger.info(f"Node optimization complete: ${total_current:.0f}/mo -> ${total_current - total_savings:.0f}/mo (save ${total_savings:.0f}/mo)")

        return result

    def execute_replacement(self, node_group_name: str, new_instance_type: str, new_capacity_type: str) -> dict:
        """Execute a node replacement by updating the node group."""
        try:
            # Get current node group config
            ng = self.eks.describe_nodegroup(clusterName=self.cluster_name, nodegroupName=node_group_name)["nodegroup"]
            current_lt = ng.get("launchTemplate", {})

            if not current_lt.get("id"):
                return {"success": False, "message": "Node group has no launch template. Manual replacement needed."}

            # Create new launch template version with new instance type
            lt_id = current_lt["id"]
            new_version = self.ec2.create_launch_template_version(
                LaunchTemplateId=lt_id,
                SourceVersion=str(current_lt.get("version", "1")),
                LaunchTemplateData={"InstanceType": new_instance_type},
            )["LaunchTemplateVersion"]["VersionNumber"]

            # Update node group with new launch template version
            update_config = {}
            if new_capacity_type != ng.get("capacityType", "ON_DEMAND"):
                return {
                    "success": False,
                    "message": f"Cannot change capacity type in-place. Create a new node group with {new_capacity_type} and migrate pods.",
                    "manual_steps": [
                        f"1. Create new node group: {node_group_name}-{new_capacity_type.lower()} with {new_instance_type} ({new_capacity_type})",
                        f"2. Cordon old nodes: kubectl cordon <old-nodes>",
                        f"3. Drain old nodes: kubectl drain <old-nodes> --ignore-daemonsets --delete-emptydir-data",
                        f"4. Delete old node group: aws eks delete-nodegroup --cluster-name {self.cluster_name} --nodegroup-name {node_group_name}",
                    ],
                }

            self.eks.update_nodegroup_version(
                clusterName=self.cluster_name,
                nodegroupName=node_group_name,
                launchTemplate={"id": lt_id, "version": str(new_version)},
            )

            return {
                "success": True,
                "message": f"Updated {node_group_name} to {new_instance_type}. Rolling update in progress.",
                "new_instance_type": new_instance_type,
                "new_lt_version": new_version,
            }

        except Exception as e:
            logger.error(f"Replacement execution failed: {e}")
            return {"success": False, "message": str(e)}

    def _analyze_node_groups(self) -> list:
        groups = []
        try:
            ng_names = self.eks.list_nodegroups(clusterName=self.cluster_name).get("nodegroups", [])
            nodes = self.core_v1.list_node()
            node_map = {}
            for n in nodes.items:
                ng = (n.metadata.labels or {}).get("eks.amazonaws.com/nodegroup", "")
                if ng not in node_map:
                    node_map[ng] = []
                node_map[ng].append(n)

            for ng_name in ng_names:
                ng = self.eks.describe_nodegroup(clusterName=self.cluster_name, nodegroupName=ng_name)["nodegroup"]
                scaling = ng["scalingConfig"]

                cluster_nodes = node_map.get(ng_name, [])
                instance_type = "unknown"
                capacity_type = ng.get("capacityType", "ON_DEMAND")

                if cluster_nodes:
                    instance_type = (cluster_nodes[0].metadata.labels or {}).get("node.kubernetes.io/instance-type", "unknown")

                specs = INSTANCE_SPECS.get(instance_type, {"vcpu": 2, "mem_gb": 8})
                price = SPOT_PRICES.get(instance_type, 0.05) if capacity_type == "SPOT" else ONDEMAND_PRICES.get(instance_type, 0.10)

                # Calculate utilization
                avg_cpu = 0
                avg_mem = 0
                if cluster_nodes:
                    total_cpu_util = 0
                    total_mem_util = 0
                    for node in cluster_nodes:
                        cpu_cap = int(node.status.capacity.get("cpu", "1"))
                        mem_cap = self._parse_mem(node.status.capacity.get("memory", "1Gi"))
                        try:
                            pods = self.core_v1.list_pod_for_all_namespaces(
                                field_selector=f"spec.nodeName={node.metadata.name},status.phase=Running"
                            )
                            cpu_req = sum(
                                self._parse_cpu(c.resources.requests.get("cpu", "0"))
                                for p in pods.items for c in p.spec.containers
                                if c.resources and c.resources.requests
                            )
                            mem_req = sum(
                                self._parse_mem(c.resources.requests.get("memory", "0"))
                                for p in pods.items for c in p.spec.containers
                                if c.resources and c.resources.requests
                            )
                            total_cpu_util += (cpu_req / (cpu_cap * 1000) * 100) if cpu_cap > 0 else 0
                            total_mem_util += (mem_req / mem_cap * 100) if mem_cap > 0 else 0
                        except Exception:
                            pass
                    avg_cpu = total_cpu_util / len(cluster_nodes)
                    avg_mem = total_mem_util / len(cluster_nodes)

                node_count = len(cluster_nodes)
                groups.append(NodeGroupAnalysis(
                    name=ng_name,
                    instance_type=instance_type,
                    capacity_type=capacity_type,
                    node_count=node_count,
                    desired=scaling["desiredSize"],
                    min_size=scaling["minSize"],
                    max_size=scaling["maxSize"],
                    hourly_cost=price * max(node_count, scaling["desiredSize"]),
                    monthly_cost=price * max(node_count, scaling["desiredSize"]) * 730,
                    cpu_capacity=specs["vcpu"],
                    memory_gb=specs["mem_gb"],
                    avg_cpu_util=round(avg_cpu, 1),
                    avg_mem_util=round(avg_mem, 1),
                ))
        except Exception as e:
            logger.error(f"Error analyzing node groups: {e}")
        return groups

    def _find_replacements(self, group: NodeGroupAnalysis) -> list:
        replacements = []
        if group.node_count == 0 and group.desired == 0:
            return replacements

        current_specs = INSTANCE_SPECS.get(group.instance_type, {"vcpu": 2, "mem_gb": 8})
        is_spot = group.capacity_type == "SPOT"
        current_price = SPOT_PRICES.get(group.instance_type, 0.05) if is_spot else ONDEMAND_PRICES.get(group.instance_type, 0.10)
        count = max(group.node_count, group.desired)

        # 1. Check if on-demand can convert to SPOT
        if not is_spot:
            spot_price = SPOT_PRICES.get(group.instance_type, current_price * 0.4)
            savings = (current_price - spot_price) * count * 730
            if savings > 0:
                replacements.append(Replacement(
                    current_group=group.name,
                    current_type=group.instance_type,
                    current_capacity="ON_DEMAND",
                    current_monthly=current_price * count * 730,
                    recommended_type=group.instance_type,
                    recommended_capacity="SPOT",
                    recommended_monthly=spot_price * count * 730,
                    savings_monthly=savings,
                    savings_percent=(savings / (current_price * count * 730) * 100),
                    reason=f"Convert to SPOT: same instance, ~{(savings / (current_price * count * 730) * 100):.0f}% cheaper. Use mixed policy for reliability.",
                    risk="medium",
                    auto_executable=False,
                ))

        # 2. Find cheaper instance types with sufficient capacity
        for inst_type, specs in INSTANCE_SPECS.items():
            if inst_type == group.instance_type:
                continue
            if specs["vcpu"] < current_specs["vcpu"] * 0.8 or specs["mem_gb"] < current_specs["mem_gb"] * 0.8:
                continue

            price = SPOT_PRICES.get(inst_type, 999) if is_spot else ONDEMAND_PRICES.get(inst_type, 999)
            if price >= current_price:
                continue

            savings = (current_price - price) * count * 730
            replacements.append(Replacement(
                current_group=group.name,
                current_type=group.instance_type,
                current_capacity=group.capacity_type,
                current_monthly=current_price * count * 730,
                recommended_type=inst_type,
                recommended_capacity=group.capacity_type,
                recommended_monthly=price * count * 730,
                savings_monthly=savings,
                savings_percent=(savings / (current_price * count * 730) * 100),
                reason=f"Replace with {inst_type} ({specs['vcpu']}vCPU, {specs['mem_gb']}GB). Sufficient capacity, {(savings / (current_price * count * 730) * 100):.0f}% cheaper.",
                risk="low",
                auto_executable=True,
            ))

        # 3. If underutilized, recommend smaller instance
        if group.avg_cpu_util < 30 and group.avg_mem_util < 30 and group.avg_cpu_util > 0:
            for inst_type, specs in INSTANCE_SPECS.items():
                if inst_type == group.instance_type:
                    continue
                if specs["vcpu"] >= current_specs["vcpu"]:
                    continue
                if specs["vcpu"] * 1000 * 0.7 < (group.avg_cpu_util / 100 * current_specs["vcpu"] * 1000):
                    continue

                price = SPOT_PRICES.get(inst_type, 999) if is_spot else ONDEMAND_PRICES.get(inst_type, 999)
                if price >= current_price:
                    continue

                savings = (current_price - price) * count * 730
                replacements.append(Replacement(
                    current_group=group.name,
                    current_type=group.instance_type,
                    current_capacity=group.capacity_type,
                    current_monthly=current_price * count * 730,
                    recommended_type=inst_type,
                    recommended_capacity=group.capacity_type,
                    recommended_monthly=price * count * 730,
                    savings_monthly=savings,
                    savings_percent=(savings / (current_price * count * 730) * 100),
                    reason=f"Downsize to {inst_type}. Current utilization only CPU:{group.avg_cpu_util:.0f}% MEM:{group.avg_mem_util:.0f}%.",
                    risk="low",
                    auto_executable=True,
                ))

        # Keep top 3 per group
        replacements.sort(key=lambda r: -r.savings_monthly)
        return replacements[:3]

    def _get_ai_migration_plan(self, groups, replacements, total_cost) -> str:
        try:
            groups_json = json.dumps([
                {"name": g.name, "type": g.instance_type, "capacity": g.capacity_type,
                 "nodes": g.node_count, "cost": f"${g.monthly_cost:.0f}/mo",
                 "cpu_util": f"{g.avg_cpu_util:.0f}%", "mem_util": f"{g.avg_mem_util:.0f}%"}
                for g in groups
            ], indent=2)

            recs_json = json.dumps([
                {"group": r.current_group, "from": f"{r.current_type}({r.current_capacity})",
                 "to": f"{r.recommended_type}({r.recommended_capacity})",
                 "savings": f"${r.savings_monthly:.0f}/mo", "risk": r.risk}
                for r in replacements[:10]
            ], indent=2)

            prompt = f"""You are a Kubernetes infrastructure expert. Create a migration plan for replacing expensive nodes with cheaper alternatives.

Current cluster cost: ${total_cost:.0f}/month

Node Groups:
{groups_json}

Recommended Replacements:
{recs_json}

Provide a step-by-step migration plan:
1. Which replacements to do first (lowest risk, highest savings)
2. Migration steps for each (cordon, drain, replace, verify)
3. Rollback plan if something goes wrong
4. Estimated downtime per replacement
5. Mixed instance strategy recommendation (Spot + On-demand ratio)

Keep it concise and actionable."""

            body = json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
            })
            resp = self.bedrock.invoke_model(modelId=config.BEDROCK_MODEL_ID, body=body,
                                             contentType="application/json", accept="application/json")
            return json.loads(resp["body"].read())["content"][0]["text"]
        except Exception as e:
            return f"AI migration plan unavailable: {e}"

    def _store_report(self, report_id, result):
        try:
            content = json.dumps(result, indent=2, default=str)
            cm = k8s_client.V1ConfigMap(
                metadata=k8s_client.V1ObjectMeta(
                    name=f"node-optimization-{report_id}",
                    namespace=os.getenv("REPORT_NAMESPACE", config.REPORT_NAMESPACE),
                    labels={"app": "k8s-healing-agent", "type": "node-optimization"},
                ),
                data={"report.json": content},
            )
            self.core_v1.create_namespaced_config_map(
                os.getenv("REPORT_NAMESPACE", config.REPORT_NAMESPACE), cm
            )
        except Exception as e:
            logger.error(f"Failed to store report: {e}")

    def _parse_cpu(self, val):
        val = str(val)
        if val.endswith("n"): return max(1, int(int(val[:-1]) / 1000000))
        if val.endswith("m"): return int(val[:-1])
        try: return int(float(val) * 1000)
        except: return 0

    def _parse_mem(self, val):
        val = str(val)
        if val.endswith("Gi"): return int(float(val[:-2]) * 1024)
        if val.endswith("Mi"): return int(float(val[:-2]))
        if val.endswith("Ki"): return max(1, int(float(val[:-2]) / 1024))
        try: return max(1, int(int(val) / (1024 * 1024)))
        except: return 0
