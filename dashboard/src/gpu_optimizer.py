"""
GPU Optimization Engine
- GPU node tracking: discover and monitor GPU nodes
- GPU utilization: track GPU usage per pod/node
- GPU right-sizing: recommend cheaper GPU instances
- GPU cost attribution: cost per GPU workload
- GPU spot recommendations: GPU spot pricing
"""

import os
import json
import logging
from datetime import datetime, timezone
from collections import defaultdict

import boto3
from kubernetes import client as k8s_client

from . import config

logger = logging.getLogger("gpu-optimizer")

GPU_INSTANCES = {
    "g4dn.xlarge": {"gpu": 1, "gpu_type": "T4", "gpu_mem_gb": 16, "vcpu": 4, "mem_gb": 16, "ondemand": 0.526, "spot": 0.158},
    "g4dn.2xlarge": {"gpu": 1, "gpu_type": "T4", "gpu_mem_gb": 16, "vcpu": 8, "mem_gb": 32, "ondemand": 0.752, "spot": 0.226},
    "g4dn.4xlarge": {"gpu": 1, "gpu_type": "T4", "gpu_mem_gb": 16, "vcpu": 16, "mem_gb": 64, "ondemand": 1.204, "spot": 0.361},
    "g4dn.8xlarge": {"gpu": 1, "gpu_type": "T4", "gpu_mem_gb": 16, "vcpu": 32, "mem_gb": 128, "ondemand": 2.176, "spot": 0.653},
    "g4dn.12xlarge": {"gpu": 4, "gpu_type": "T4", "gpu_mem_gb": 64, "vcpu": 48, "mem_gb": 192, "ondemand": 3.912, "spot": 1.174},
    "g5.xlarge": {"gpu": 1, "gpu_type": "A10G", "gpu_mem_gb": 24, "vcpu": 4, "mem_gb": 16, "ondemand": 1.006, "spot": 0.302},
    "g5.2xlarge": {"gpu": 1, "gpu_type": "A10G", "gpu_mem_gb": 24, "vcpu": 8, "mem_gb": 32, "ondemand": 1.212, "spot": 0.364},
    "g5.4xlarge": {"gpu": 1, "gpu_type": "A10G", "gpu_mem_gb": 24, "vcpu": 16, "mem_gb": 64, "ondemand": 1.624, "spot": 0.487},
    "g5.12xlarge": {"gpu": 4, "gpu_type": "A10G", "gpu_mem_gb": 96, "vcpu": 48, "mem_gb": 192, "ondemand": 5.672, "spot": 1.702},
    "p3.2xlarge": {"gpu": 1, "gpu_type": "V100", "gpu_mem_gb": 16, "vcpu": 8, "mem_gb": 61, "ondemand": 3.06, "spot": 0.918},
    "p3.8xlarge": {"gpu": 4, "gpu_type": "V100", "gpu_mem_gb": 64, "vcpu": 32, "mem_gb": 244, "ondemand": 12.24, "spot": 3.672},
    "p4d.24xlarge": {"gpu": 8, "gpu_type": "A100", "gpu_mem_gb": 320, "vcpu": 96, "mem_gb": 1152, "ondemand": 32.77, "spot": 9.831},
    "inf1.xlarge": {"gpu": 1, "gpu_type": "Inferentia", "gpu_mem_gb": 8, "vcpu": 4, "mem_gb": 8, "ondemand": 0.368, "spot": 0.110},
    "inf1.2xlarge": {"gpu": 1, "gpu_type": "Inferentia", "gpu_mem_gb": 8, "vcpu": 8, "mem_gb": 16, "ondemand": 0.584, "spot": 0.175},
    "inf2.xlarge": {"gpu": 1, "gpu_type": "Inferentia2", "gpu_mem_gb": 32, "vcpu": 4, "mem_gb": 16, "ondemand": 0.758, "spot": 0.227},
    "inf2.8xlarge": {"gpu": 1, "gpu_type": "Inferentia2", "gpu_mem_gb": 32, "vcpu": 32, "mem_gb": 128, "ondemand": 1.968, "spot": 0.590},
}


class GPUOptimizer:
    def __init__(self, core_v1: k8s_client.CoreV1Api):
        self.core_v1 = core_v1
        self.region = os.getenv("AWS_REGION", config.BEDROCK_REGION)
        self.cluster_name = os.getenv("CLUSTER_NAME", "")

        try:
            self.eks = boto3.client("eks", region_name=self.region)
        except Exception:
            self.eks = None

        bedrock_kwargs = {"service_name": "bedrock-runtime", "region_name": config.BEDROCK_REGION}
        if config.BEDROCK_ENDPOINT_URL:
            bedrock_kwargs["endpoint_url"] = config.BEDROCK_ENDPOINT_URL
        self.bedrock = boto3.client(**bedrock_kwargs)

    def analyze(self) -> dict:
        """Full GPU analysis."""
        logger.info("Running GPU optimization analysis...")

        gpu_nodes = self._discover_gpu_nodes()
        gpu_pods = self._discover_gpu_pods()
        node_groups = self._get_gpu_node_groups()
        utilization = self._analyze_utilization(gpu_nodes, gpu_pods)
        recommendations = self._generate_recommendations(gpu_nodes, gpu_pods, node_groups)
        cost_breakdown = self._cost_breakdown(gpu_nodes)
        ai_summary = self._ai_analysis(gpu_nodes, gpu_pods, recommendations, cost_breakdown)

        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "summary": {
                "gpu_nodes": len(gpu_nodes),
                "gpu_pods": len(gpu_pods),
                "total_gpus": sum(n["gpu_count"] for n in gpu_nodes),
                "gpus_allocated": sum(p["gpu_requested"] for p in gpu_pods),
                "gpus_idle": sum(n["gpu_count"] for n in gpu_nodes) - sum(p["gpu_requested"] for p in gpu_pods),
                "monthly_gpu_cost": round(sum(n["monthly_cost"] for n in gpu_nodes), 2),
            },
            "gpu_nodes": gpu_nodes,
            "gpu_pods": gpu_pods,
            "utilization": utilization,
            "recommendations": recommendations,
            "cost_breakdown": cost_breakdown,
            "ai_summary": ai_summary,
        }

    def _discover_gpu_nodes(self) -> list:
        """Find all nodes with GPUs."""
        gpu_nodes = []
        try:
            nodes = self.core_v1.list_node()
            for node in nodes.items:
                labels = node.metadata.labels or {}
                capacity = node.status.capacity or {}
                allocatable = node.status.allocatable or {}

                gpu_cap = int(capacity.get("nvidia.com/gpu", 0))
                gpu_alloc = int(allocatable.get("nvidia.com/gpu", 0))

                if gpu_cap == 0:
                    # Check for AWS Inferentia
                    gpu_cap = int(capacity.get("aws.amazon.com/neuron", 0))
                    gpu_alloc = int(allocatable.get("aws.amazon.com/neuron", 0))

                if gpu_cap == 0:
                    continue

                inst_type = labels.get("node.kubernetes.io/instance-type", "unknown")
                cap_type = "SPOT" if labels.get("eks.amazonaws.com/capacityType") == "SPOT" else "ON_DEMAND"
                ng = labels.get("eks.amazonaws.com/nodegroup", "unknown")
                gpu_info = GPU_INSTANCES.get(inst_type, {})

                price = gpu_info.get("spot", 0.5) if cap_type == "SPOT" else gpu_info.get("ondemand", 1.0)

                # Count GPU pods on this node
                allocated_gpus = 0
                try:
                    pods = self.core_v1.list_pod_for_all_namespaces(
                        field_selector=f"spec.nodeName={node.metadata.name},status.phase=Running"
                    )
                    for p in pods.items:
                        for c in p.spec.containers:
                            if c.resources and c.resources.requests:
                                allocated_gpus += int(c.resources.requests.get("nvidia.com/gpu", 0))
                                allocated_gpus += int(c.resources.requests.get("aws.amazon.com/neuron", 0))
                            if c.resources and c.resources.limits:
                                allocated_gpus = max(allocated_gpus, int(c.resources.limits.get("nvidia.com/gpu", 0)))
                                allocated_gpus = max(allocated_gpus, int(c.resources.limits.get("aws.amazon.com/neuron", 0)))
                except Exception:
                    pass

                gpu_nodes.append({
                    "name": node.metadata.name[:40],
                    "instance_type": inst_type,
                    "capacity_type": cap_type,
                    "node_group": ng,
                    "gpu_count": gpu_cap,
                    "gpu_type": gpu_info.get("gpu_type", "Unknown"),
                    "gpu_memory_gb": gpu_info.get("gpu_mem_gb", 0),
                    "gpus_allocated": allocated_gpus,
                    "gpus_free": gpu_cap - allocated_gpus,
                    "utilization_pct": round(allocated_gpus / gpu_cap * 100, 1) if gpu_cap > 0 else 0,
                    "hourly_cost": price,
                    "monthly_cost": price * 730,
                    "vcpu": gpu_info.get("vcpu", 0),
                    "memory_gb": gpu_info.get("mem_gb", 0),
                })

        except Exception as e:
            logger.error(f"Error discovering GPU nodes: {e}")
        return gpu_nodes

    def _discover_gpu_pods(self) -> list:
        """Find all pods requesting GPUs."""
        gpu_pods = []
        try:
            pods = self.core_v1.list_pod_for_all_namespaces()
            for pod in pods.items:
                ns = pod.metadata.namespace
                for c in pod.spec.containers:
                    gpu_req = 0
                    gpu_type = ""
                    if c.resources:
                        reqs = c.resources.requests or {}
                        lims = c.resources.limits or {}
                        nvidia = int(reqs.get("nvidia.com/gpu", lims.get("nvidia.com/gpu", 0)))
                        inferentia = int(reqs.get("aws.amazon.com/neuron", lims.get("aws.amazon.com/neuron", 0)))
                        if nvidia > 0:
                            gpu_req = nvidia
                            gpu_type = "NVIDIA"
                        elif inferentia > 0:
                            gpu_req = inferentia
                            gpu_type = "Inferentia"

                    if gpu_req > 0:
                        gpu_pods.append({
                            "pod": f"{ns}/{pod.metadata.name}",
                            "namespace": ns,
                            "container": c.name,
                            "node": (pod.spec.node_name or "")[:30],
                            "gpu_requested": gpu_req,
                            "gpu_type": gpu_type,
                            "phase": pod.status.phase,
                            "deployment": self._get_owner(pod),
                        })
        except Exception as e:
            logger.error(f"Error discovering GPU pods: {e}")
        return gpu_pods

    def _get_gpu_node_groups(self) -> list:
        """Get GPU node groups from EKS."""
        groups = []
        if not self.eks or not self.cluster_name:
            return groups
        try:
            ng_names = self.eks.list_nodegroups(clusterName=self.cluster_name).get("nodegroups", [])
            for name in ng_names:
                ng = self.eks.describe_nodegroup(clusterName=self.cluster_name, nodegroupName=name)["nodegroup"]
                # Check if any instance type is GPU
                for it in (ng.get("instanceTypes") or []):
                    if it in GPU_INSTANCES:
                        groups.append({
                            "name": name,
                            "instance_type": it,
                            "capacity_type": ng.get("capacityType", "ON_DEMAND"),
                            "desired": ng["scalingConfig"]["desiredSize"],
                            "min": ng["scalingConfig"]["minSize"],
                            "max": ng["scalingConfig"]["maxSize"],
                        })
                        break
        except Exception as e:
            logger.error(f"Error getting GPU node groups: {e}")
        return groups

    def _analyze_utilization(self, gpu_nodes, gpu_pods) -> dict:
        total_gpus = sum(n["gpu_count"] for n in gpu_nodes)
        allocated = sum(p["gpu_requested"] for p in gpu_pods)
        idle = total_gpus - allocated
        idle_nodes = [n for n in gpu_nodes if n["gpus_free"] == n["gpu_count"]]
        partial_nodes = [n for n in gpu_nodes if 0 < n["gpus_free"] < n["gpu_count"]]

        wasted_cost = sum(n["monthly_cost"] for n in idle_nodes)

        return {
            "total_gpus": total_gpus,
            "allocated_gpus": allocated,
            "idle_gpus": idle,
            "utilization_pct": round(allocated / total_gpus * 100, 1) if total_gpus > 0 else 0,
            "idle_nodes": len(idle_nodes),
            "partially_used_nodes": len(partial_nodes),
            "wasted_monthly_cost": round(wasted_cost, 2),
            "idle_node_details": [
                {"node": n["name"], "type": n["instance_type"], "cost": f"${n['monthly_cost']:.0f}/mo"}
                for n in idle_nodes
            ],
        }

    def _generate_recommendations(self, gpu_nodes, gpu_pods, node_groups) -> list:
        recs = []

        for node in gpu_nodes:
            # Idle GPU node — scale down
            if node["gpus_free"] == node["gpu_count"]:
                recs.append({
                    "type": "remove_idle_gpu",
                    "priority": "high",
                    "target": node["name"],
                    "current": f"{node['instance_type']} ({node['capacity_type']})",
                    "savings": f"${node['monthly_cost']:.0f}/mo",
                    "detail": f"GPU node completely idle. {node['gpu_count']}x {node['gpu_type']} unused.",
                    "action": "Scale down node group or remove node",
                })

            # On-demand GPU — convert to Spot
            if node["capacity_type"] == "ON_DEMAND":
                gpu_info = GPU_INSTANCES.get(node["instance_type"], {})
                if gpu_info:
                    spot_price = gpu_info.get("spot", node["hourly_cost"])
                    savings = (node["hourly_cost"] - spot_price) * 730
                    if savings > 0:
                        recs.append({
                            "type": "gpu_spot_conversion",
                            "priority": "medium",
                            "target": node["name"],
                            "current": f"{node['instance_type']} ON_DEMAND ${node['monthly_cost']:.0f}/mo",
                            "savings": f"${savings:.0f}/mo ({savings/node['monthly_cost']*100:.0f}%)",
                            "detail": f"Convert to SPOT. Same GPU ({node['gpu_type']}), ~{savings/node['monthly_cost']*100:.0f}% cheaper.",
                            "action": "Create SPOT node group with same instance type",
                        })

            # Oversized GPU — can use smaller instance
            if node["gpus_allocated"] > 0 and node["gpus_free"] > 0:
                for smaller_type, info in GPU_INSTANCES.items():
                    if info["gpu"] >= node["gpus_allocated"] and info["gpu"] < node["gpu_count"]:
                        is_spot = node["capacity_type"] == "SPOT"
                        smaller_price = (info["spot"] if is_spot else info["ondemand"]) * 730
                        if smaller_price < node["monthly_cost"] * 0.7:
                            recs.append({
                                "type": "gpu_rightsize",
                                "priority": "medium",
                                "target": node["name"],
                                "current": f"{node['instance_type']} ({node['gpu_count']}x {node['gpu_type']})",
                                "savings": f"${node['monthly_cost'] - smaller_price:.0f}/mo",
                                "detail": f"Downsize to {smaller_type} ({info['gpu']}x {info['gpu_type']}). Only using {node['gpus_allocated']}/{node['gpu_count']} GPUs.",
                                "action": f"Replace with {smaller_type}",
                            })
                            break

        recs.sort(key=lambda r: 0 if r["priority"] == "high" else 1)
        return recs

    def _cost_breakdown(self, gpu_nodes) -> dict:
        by_type = defaultdict(lambda: {"count": 0, "monthly_cost": 0})
        by_capacity = defaultdict(lambda: {"count": 0, "monthly_cost": 0})
        by_group = defaultdict(lambda: {"count": 0, "monthly_cost": 0})

        for n in gpu_nodes:
            by_type[n["gpu_type"]]["count"] += n["gpu_count"]
            by_type[n["gpu_type"]]["monthly_cost"] += n["monthly_cost"]
            by_capacity[n["capacity_type"]]["count"] += 1
            by_capacity[n["capacity_type"]]["monthly_cost"] += n["monthly_cost"]
            by_group[n["node_group"]]["count"] += 1
            by_group[n["node_group"]]["monthly_cost"] += n["monthly_cost"]

        return {
            "by_gpu_type": [{"type": k, "gpus": v["count"], "cost": f"${v['monthly_cost']:.0f}/mo"} for k, v in by_type.items()],
            "by_capacity": [{"type": k, "nodes": v["count"], "cost": f"${v['monthly_cost']:.0f}/mo"} for k, v in by_capacity.items()],
            "by_node_group": [{"group": k, "nodes": v["count"], "cost": f"${v['monthly_cost']:.0f}/mo"} for k, v in by_group.items()],
            "total_monthly": round(sum(n["monthly_cost"] for n in gpu_nodes), 2),
        }

    def _ai_analysis(self, gpu_nodes, gpu_pods, recommendations, cost_breakdown) -> str:
        if not gpu_nodes:
            return "No GPU nodes detected in the cluster. If you need GPU workloads, consider g4dn.xlarge (SPOT ~$0.16/hr) for inference or g5.xlarge for training."

        try:
            context = json.dumps({
                "gpu_nodes": len(gpu_nodes),
                "total_gpus": sum(n["gpu_count"] for n in gpu_nodes),
                "gpu_types": list(set(n["gpu_type"] for n in gpu_nodes)),
                "monthly_cost": cost_breakdown["total_monthly"],
                "recommendations": len(recommendations),
                "top_recs": [{"type": r["type"], "savings": r["savings"]} for r in recommendations[:5]],
            }, indent=2)

            prompt = f"""You are a GPU cloud cost optimization expert. Analyze this GPU infrastructure and provide recommendations.

{context}

Provide 3-5 bullet points:
1. Current GPU cost assessment
2. Immediate savings opportunities
3. Right-sizing recommendations (match GPU type to workload)
4. SPOT vs On-Demand strategy for GPU workloads
5. Best practices for GPU utilization

Be specific with dollar amounts. Keep concise."""

            body = json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 512,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
            })
            resp = self.bedrock.invoke_model(modelId=config.BEDROCK_MODEL_ID, body=body,
                                             contentType="application/json", accept="application/json")
            return json.loads(resp["body"].read())["content"][0]["text"]
        except Exception as e:
            return f"AI analysis unavailable: {e}"

    def _get_owner(self, pod):
        if pod.metadata.owner_references:
            for ref in pod.metadata.owner_references:
                if ref.kind == "ReplicaSet":
                    try:
                        rs = k8s_client.AppsV1Api().read_namespaced_replica_set(ref.name, pod.metadata.namespace)
                        if rs.metadata.owner_references:
                            for rs_ref in rs.metadata.owner_references:
                                if rs_ref.kind == "Deployment":
                                    return rs_ref.name
                    except Exception:
                        pass
                return ref.name
        return ""
