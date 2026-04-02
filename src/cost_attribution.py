"""
Cost Attribution Engine
- Cost per Namespace
- Cost per Deployment
- Cost per Pod
- Cost trending (store daily snapshots)
- Cost alerts (namespace budget exceeded)
"""

import os
import json
import logging
from datetime import datetime, timezone
from collections import defaultdict
from dataclasses import dataclass

from kubernetes import client as k8s_client

from . import config

logger = logging.getLogger("cost-attribution")

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


class CostAttribution:
    def __init__(self, core_v1: k8s_client.CoreV1Api, apps_v1: k8s_client.AppsV1Api):
        self.core_v1 = core_v1
        self.apps_v1 = apps_v1

    def get_full_breakdown(self) -> dict:
        """Get complete cost breakdown: cluster, namespace, deployment, pod."""
        nodes = self._get_nodes()
        total_cluster_cost_hourly = sum(n["hourly_cost"] for n in nodes)
        total_cluster_cpu = sum(n["cpu_capacity_m"] for n in nodes)
        total_cluster_mem = sum(n["mem_capacity_mi"] for n in nodes)

        # Cost per CPU-millisecond-hour and per Mi-hour
        cpu_cost_per_m_hr = total_cluster_cost_hourly / total_cluster_cpu if total_cluster_cpu > 0 else 0
        mem_cost_per_mi_hr = total_cluster_cost_hourly / total_cluster_mem if total_cluster_mem > 0 else 0
        # Split 60% CPU weight, 40% memory weight
        cpu_weight = 0.6
        mem_weight = 0.4

        pods = self._get_all_pods()

        # Calculate per-pod cost
        pod_costs = []
        ns_costs = defaultdict(lambda: {"cpu_m": 0, "mem_mi": 0, "cost_hourly": 0, "pods": 0, "deployments": set()})
        depl_costs = defaultdict(lambda: {"cpu_m": 0, "mem_mi": 0, "cost_hourly": 0, "pods": 0, "namespace": ""})

        for pod in pods:
            cpu_req = pod["cpu_request_m"]
            mem_req = pod["memory_request_mi"]

            # Cost = (CPU share * CPU weight + Memory share * Memory weight) * total cost
            cpu_share = (cpu_req / total_cluster_cpu) if total_cluster_cpu > 0 else 0
            mem_share = (mem_req / total_cluster_mem) if total_cluster_mem > 0 else 0
            pod_hourly = (cpu_share * cpu_weight + mem_share * mem_weight) * total_cluster_cost_hourly
            pod_monthly = pod_hourly * 730

            pod_costs.append({
                "name": pod["name"],
                "namespace": pod["namespace"],
                "deployment": pod["deployment"],
                "node": pod["node"],
                "cpu_request": f"{cpu_req}m",
                "memory_request": f"{mem_req}Mi",
                "hourly_cost": round(pod_hourly, 4),
                "monthly_cost": round(pod_monthly, 2),
            })

            ns = pod["namespace"]
            ns_costs[ns]["cpu_m"] += cpu_req
            ns_costs[ns]["mem_mi"] += mem_req
            ns_costs[ns]["cost_hourly"] += pod_hourly
            ns_costs[ns]["pods"] += 1
            if pod["deployment"]:
                ns_costs[ns]["deployments"].add(pod["deployment"])

            if pod["deployment"]:
                dk = f"{ns}/{pod['deployment']}"
                depl_costs[dk]["cpu_m"] += cpu_req
                depl_costs[dk]["mem_mi"] += mem_req
                depl_costs[dk]["cost_hourly"] += pod_hourly
                depl_costs[dk]["pods"] += 1
                depl_costs[dk]["namespace"] = ns

        # Sort
        pod_costs.sort(key=lambda x: -x["monthly_cost"])

        namespace_breakdown = sorted([
            {
                "namespace": ns,
                "cpu_total_m": data["cpu_m"],
                "memory_total_mi": data["mem_mi"],
                "hourly_cost": round(data["cost_hourly"], 4),
                "monthly_cost": round(data["cost_hourly"] * 730, 2),
                "pod_count": data["pods"],
                "deployment_count": len(data["deployments"]),
                "cost_percent": round((data["cost_hourly"] / total_cluster_cost_hourly * 100) if total_cluster_cost_hourly > 0 else 0, 1),
            }
            for ns, data in ns_costs.items()
        ], key=lambda x: -x["monthly_cost"])

        deployment_breakdown = sorted([
            {
                "deployment": dk,
                "namespace": data["namespace"],
                "cpu_total_m": data["cpu_m"],
                "memory_total_mi": data["mem_mi"],
                "hourly_cost": round(data["cost_hourly"], 4),
                "monthly_cost": round(data["cost_hourly"] * 730, 2),
                "pod_count": data["pods"],
                "cost_percent": round((data["cost_hourly"] / total_cluster_cost_hourly * 100) if total_cluster_cost_hourly > 0 else 0, 1),
            }
            for dk, data in depl_costs.items()
        ], key=lambda x: -x["monthly_cost"])

        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "cluster": {
                "total_nodes": len(nodes),
                "total_pods": len(pods),
                "total_hourly_cost": round(total_cluster_cost_hourly, 4),
                "total_monthly_cost": round(total_cluster_cost_hourly * 730, 2),
                "total_cpu_m": total_cluster_cpu,
                "total_memory_mi": total_cluster_mem,
            },
            "nodes": [
                {
                    "name": n["name"],
                    "instance_type": n["instance_type"],
                    "capacity_type": n["capacity_type"],
                    "hourly_cost": round(n["hourly_cost"], 4),
                    "monthly_cost": round(n["hourly_cost"] * 730, 2),
                }
                for n in nodes
            ],
            "by_namespace": namespace_breakdown,
            "by_deployment": deployment_breakdown[:30],
            "by_pod": pod_costs[:50],
            "top_expensive_namespaces": namespace_breakdown[:5],
            "top_expensive_deployments": deployment_breakdown[:10],
        }

    def check_budget_alerts(self, budgets: dict) -> list:
        """Check if any namespace exceeds its monthly budget.
        budgets = {"atlas": 200, "atlas-dev": 100}
        """
        breakdown = self.get_full_breakdown()
        alerts = []
        for ns_data in breakdown["by_namespace"]:
            ns = ns_data["namespace"]
            if ns in budgets:
                budget = budgets[ns]
                cost = ns_data["monthly_cost"]
                if cost > budget:
                    alerts.append({
                        "namespace": ns,
                        "budget": budget,
                        "current_cost": cost,
                        "overage": round(cost - budget, 2),
                        "overage_percent": round((cost - budget) / budget * 100, 1),
                        "severity": "critical" if cost > budget * 1.2 else "warning",
                    })
                elif cost > budget * 0.8:
                    alerts.append({
                        "namespace": ns,
                        "budget": budget,
                        "current_cost": cost,
                        "overage": 0,
                        "overage_percent": 0,
                        "severity": "approaching",
                        "usage_percent": round(cost / budget * 100, 1),
                    })
        return alerts

    def store_daily_snapshot(self):
        """Store today's cost snapshot for trending."""
        try:
            breakdown = self.get_full_breakdown()
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

            snapshot = {
                "date": today,
                "cluster_monthly_cost": breakdown["cluster"]["total_monthly_cost"],
                "namespaces": {
                    ns["namespace"]: ns["monthly_cost"]
                    for ns in breakdown["by_namespace"]
                },
            }

            cm_name = f"cost-snapshot-{today}"
            try:
                existing = self.core_v1.read_namespaced_config_map(
                    cm_name, os.getenv("REPORT_NAMESPACE", config.REPORT_NAMESPACE)
                )
                existing.data["snapshot.json"] = json.dumps(snapshot)
                self.core_v1.replace_namespaced_config_map(
                    cm_name, os.getenv("REPORT_NAMESPACE", config.REPORT_NAMESPACE), existing
                )
            except Exception:
                cm = k8s_client.V1ConfigMap(
                    metadata=k8s_client.V1ObjectMeta(
                        name=cm_name,
                        namespace=os.getenv("REPORT_NAMESPACE", config.REPORT_NAMESPACE),
                        labels={"app": "k8s-healing-agent", "type": "cost-snapshot"},
                    ),
                    data={"snapshot.json": json.dumps(snapshot)},
                )
                self.core_v1.create_namespaced_config_map(
                    os.getenv("REPORT_NAMESPACE", config.REPORT_NAMESPACE), cm
                )
            logger.info(f"Stored cost snapshot for {today}: ${breakdown['cluster']['total_monthly_cost']}/mo")
        except Exception as e:
            logger.error(f"Failed to store cost snapshot: {e}")

    def get_cost_trend(self, days: int = 7) -> list:
        """Get cost trend from stored snapshots."""
        trend = []
        try:
            cms = self.core_v1.list_namespaced_config_map(
                os.getenv("REPORT_NAMESPACE", config.REPORT_NAMESPACE),
                label_selector="app=k8s-healing-agent,type=cost-snapshot",
            )
            for cm in sorted(cms.items, key=lambda x: x.metadata.name, reverse=True)[:days]:
                data = json.loads(cm.data.get("snapshot.json", "{}"))
                trend.append(data)
        except Exception as e:
            logger.error(f"Error fetching cost trend: {e}")
        trend.reverse()
        return trend

    def _get_nodes(self) -> list:
        nodes = []
        try:
            node_list = self.core_v1.list_node()
            for n in node_list.items:
                labels = n.metadata.labels or {}
                inst_type = labels.get("node.kubernetes.io/instance-type", "unknown")
                cap_type = "SPOT" if labels.get("eks.amazonaws.com/capacityType") == "SPOT" else "ON_DEMAND"
                price = SPOT_PRICES.get(inst_type, 0.05) if cap_type == "SPOT" else ONDEMAND_PRICES.get(inst_type, 0.10)
                cpu_cap = int(n.status.capacity.get("cpu", "1")) * 1000
                mem_cap = self._parse_mem(n.status.capacity.get("memory", "1Gi"))

                nodes.append({
                    "name": n.metadata.name,
                    "instance_type": inst_type,
                    "capacity_type": cap_type,
                    "hourly_cost": price,
                    "cpu_capacity_m": cpu_cap,
                    "mem_capacity_mi": mem_cap,
                })
        except Exception as e:
            logger.error(f"Error getting nodes: {e}")
        return nodes

    def _get_all_pods(self) -> list:
        pods = []
        try:
            pod_list = self.core_v1.list_pod_for_all_namespaces()
            for p in pod_list.items:
                ns = p.metadata.namespace
                if ns in config.PROTECTED_NAMESPACES:
                    continue
                if p.status.phase != "Running":
                    continue

                cpu_req = 0
                mem_req = 0
                for c in p.spec.containers:
                    if c.resources and c.resources.requests:
                        cpu_req += self._parse_cpu(c.resources.requests.get("cpu", "0"))
                        mem_req += self._parse_mem(c.resources.requests.get("memory", "0"))

                deployment = self._get_deployment(p)

                pods.append({
                    "name": p.metadata.name,
                    "namespace": ns,
                    "node": p.spec.node_name or "",
                    "deployment": deployment,
                    "cpu_request_m": cpu_req,
                    "memory_request_mi": mem_req,
                })
        except Exception as e:
            logger.error(f"Error getting pods: {e}")
        return pods

    def _get_deployment(self, pod):
        if pod.metadata.owner_references:
            for ref in pod.metadata.owner_references:
                if ref.kind == "ReplicaSet":
                    try:
                        rs = self.apps_v1.read_namespaced_replica_set(ref.name, pod.metadata.namespace)
                        if rs.metadata.owner_references:
                            for rs_ref in rs.metadata.owner_references:
                                if rs_ref.kind == "Deployment":
                                    return rs_ref.name
                    except Exception:
                        pass
                elif ref.kind in ("Deployment", "StatefulSet", "DaemonSet"):
                    return ref.name
        return ""

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
