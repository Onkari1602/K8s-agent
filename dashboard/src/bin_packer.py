"""
Advanced Bin Packing Engine
- Pod scheduling score: rates each node for placement efficiency
- Defragmentation: moves pods to consolidate fragmented nodes
- Priority-based packing: keeps headroom on nodes for critical pods
"""

import os
import json
import logging
import uuid
from datetime import datetime, timezone
from collections import defaultdict

from kubernetes import client as k8s_client

from . import config

logger = logging.getLogger("bin-packer")

CRITICAL_LABELS = {"app.kubernetes.io/priority": "critical", "tier": "critical", "priority": "high"}


class BinPacker:
    def __init__(self, core_v1: k8s_client.CoreV1Api, apps_v1: k8s_client.AppsV1Api):
        self.core_v1 = core_v1
        self.apps_v1 = apps_v1

    def analyze(self) -> dict:
        nodes = self._get_nodes_with_pods()
        scores = self._score_nodes(nodes)
        defrag = self._defragmentation_plan(nodes)
        priority = self._priority_analysis(nodes)

        total_wasted_cpu = sum(n["free_cpu_m"] for n in nodes)
        total_wasted_mem = sum(n["free_mem_mi"] for n in nodes)
        avg_efficiency = sum(s["efficiency_score"] for s in scores) / len(scores) if scores else 0
        nodes_reclaimable = defrag.get("nodes_reclaimable", 0)

        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "summary": {
                "total_nodes": len(nodes),
                "avg_efficiency": round(avg_efficiency, 1),
                "total_wasted_cpu_m": total_wasted_cpu,
                "total_wasted_mem_mi": total_wasted_mem,
                "nodes_reclaimable": nodes_reclaimable,
                "critical_pods": priority["critical_pod_count"],
                "headroom_nodes": priority["headroom_nodes"],
            },
            "node_scores": scores,
            "defragmentation": defrag,
            "priority_analysis": priority,
        }

    def _get_nodes_with_pods(self) -> list:
        nodes = []
        try:
            node_list = self.core_v1.list_node()
            for node in node_list.items:
                labels = node.metadata.labels or {}
                cpu_cap = int(node.status.capacity.get("cpu", "1")) * 1000
                mem_cap = self._parse_mem(node.status.capacity.get("memory", "1Gi"))
                max_pods = int(node.status.capacity.get("pods", "29"))

                pod_list = []
                cpu_alloc = 0
                mem_alloc = 0
                try:
                    pods = self.core_v1.list_pod_for_all_namespaces(
                        field_selector=f"spec.nodeName={node.metadata.name},status.phase=Running"
                    )
                    for p in pods.items:
                        pod_cpu = 0
                        pod_mem = 0
                        for c in p.spec.containers:
                            if c.resources and c.resources.requests:
                                pod_cpu += self._parse_cpu(c.resources.requests.get("cpu", "0"))
                                pod_mem += self._parse_mem(c.resources.requests.get("memory", "0"))
                        cpu_alloc += pod_cpu
                        mem_alloc += pod_mem
                        is_critical = self._is_critical(p)
                        is_daemonset = any(ref.kind == "DaemonSet" for ref in (p.metadata.owner_references or []))
                        pod_list.append({
                            "name": p.metadata.name,
                            "namespace": p.metadata.namespace,
                            "cpu_m": pod_cpu,
                            "mem_mi": pod_mem,
                            "is_critical": is_critical,
                            "is_daemonset": is_daemonset,
                            "movable": not is_daemonset and p.metadata.namespace not in config.PROTECTED_NAMESPACES,
                        })
                except Exception:
                    pass

                nodes.append({
                    "name": node.metadata.name,
                    "instance_type": labels.get("node.kubernetes.io/instance-type", "unknown"),
                    "node_group": labels.get("eks.amazonaws.com/nodegroup", "unknown"),
                    "cpu_capacity_m": cpu_cap,
                    "mem_capacity_mi": mem_cap,
                    "cpu_allocated_m": cpu_alloc,
                    "mem_allocated_mi": mem_alloc,
                    "free_cpu_m": cpu_cap - cpu_alloc,
                    "free_mem_mi": mem_cap - mem_alloc,
                    "cpu_util_pct": round(cpu_alloc / cpu_cap * 100, 1) if cpu_cap > 0 else 0,
                    "mem_util_pct": round(mem_alloc / mem_cap * 100, 1) if mem_cap > 0 else 0,
                    "pod_count": len(pod_list),
                    "max_pods": max_pods,
                    "pods": pod_list,
                })
        except Exception as e:
            logger.error(f"Error getting nodes: {e}")
        return nodes

    def _score_nodes(self, nodes) -> list:
        """Score each node 0-100 for packing efficiency."""
        scores = []
        for node in nodes:
            cpu_score = node["cpu_util_pct"]
            mem_score = node["mem_util_pct"]
            pod_score = (node["pod_count"] / node["max_pods"] * 100) if node["max_pods"] > 0 else 0

            # Weighted: CPU 40%, Memory 40%, Pod density 20%
            efficiency = cpu_score * 0.4 + mem_score * 0.4 + pod_score * 0.2

            # Fragmentation penalty: if CPU high but memory low (or vice versa), node is fragmented
            imbalance = abs(cpu_score - mem_score)
            frag_penalty = min(imbalance * 0.3, 15)

            final_score = max(0, min(100, efficiency - frag_penalty))

            status = "optimal" if final_score >= 60 else "underutilized" if final_score < 30 else "moderate"

            scores.append({
                "node": node["name"][:40],
                "instance_type": node["instance_type"],
                "efficiency_score": round(final_score, 1),
                "cpu_util": f"{node['cpu_util_pct']}%",
                "mem_util": f"{node['mem_util_pct']}%",
                "pod_density": f"{node['pod_count']}/{node['max_pods']}",
                "fragmentation": f"{imbalance:.0f}% imbalance",
                "status": status,
                "free_cpu": f"{node['free_cpu_m']}m",
                "free_mem": f"{node['free_mem_mi']}Mi",
            })

        scores.sort(key=lambda s: s["efficiency_score"])
        return scores

    def _defragmentation_plan(self, nodes) -> dict:
        """Calculate which pods to move to consolidate nodes."""
        if len(nodes) < 2:
            return {"nodes_reclaimable": 0, "moves": [], "plan": "Not enough nodes for defragmentation"}

        # Sort nodes by utilization (lowest first = candidates for draining)
        sorted_nodes = sorted(nodes, key=lambda n: n["cpu_util_pct"] + n["mem_util_pct"])

        moves = []
        nodes_freed = 0
        simulated_nodes = {n["name"]: dict(n) for n in nodes}

        for drain_candidate in sorted_nodes:
            if drain_candidate["pod_count"] == 0:
                continue

            movable_pods = [p for p in drain_candidate["pods"] if p["movable"]]
            if not movable_pods:
                continue

            # Check if all movable pods can fit on other nodes
            total_cpu_needed = sum(p["cpu_m"] for p in movable_pods)
            total_mem_needed = sum(p["mem_mi"] for p in movable_pods)

            available_cpu = sum(
                simulated_nodes[n["name"]]["free_cpu_m"]
                for n in nodes if n["name"] != drain_candidate["name"]
            )
            available_mem = sum(
                simulated_nodes[n["name"]]["free_mem_mi"]
                for n in nodes if n["name"] != drain_candidate["name"]
            )

            if total_cpu_needed <= available_cpu and total_mem_needed <= available_mem:
                # Plan moves for each pod
                for pod in movable_pods:
                    # Find best target node (most utilized that still has room)
                    best_target = None
                    for target in sorted(nodes, key=lambda n: -(n["cpu_util_pct"] + n["mem_util_pct"])):
                        if target["name"] == drain_candidate["name"]:
                            continue
                        sim = simulated_nodes[target["name"]]
                        if sim["free_cpu_m"] >= pod["cpu_m"] and sim["free_mem_mi"] >= pod["mem_mi"]:
                            best_target = target
                            break

                    if best_target:
                        moves.append({
                            "pod": f"{pod['namespace']}/{pod['name']}",
                            "from_node": drain_candidate["name"][:30],
                            "to_node": best_target["name"][:30],
                            "cpu": f"{pod['cpu_m']}m",
                            "memory": f"{pod['mem_mi']}Mi",
                        })
                        simulated_nodes[best_target["name"]]["free_cpu_m"] -= pod["cpu_m"]
                        simulated_nodes[best_target["name"]]["free_mem_mi"] -= pod["mem_mi"]

                nodes_freed += 1
                if nodes_freed >= 2:
                    break

        return {
            "nodes_reclaimable": nodes_freed,
            "total_moves": len(moves),
            "moves": moves[:20],
            "plan": f"Can free {nodes_freed} node(s) by moving {len(moves)} pods" if nodes_freed > 0 else "Nodes are efficiently packed",
        }

    def _priority_analysis(self, nodes) -> dict:
        """Analyze critical vs non-critical pod placement."""
        critical_pods = []
        non_critical_pods = []
        nodes_with_headroom = 0

        for node in nodes:
            has_headroom = node["free_cpu_m"] > 200 and node["free_mem_mi"] > 256
            if has_headroom:
                nodes_with_headroom += 1

            for pod in node["pods"]:
                entry = {
                    "pod": f"{pod['namespace']}/{pod['name']}",
                    "node": node["name"][:30],
                    "cpu": f"{pod['cpu_m']}m",
                    "memory": f"{pod['mem_mi']}Mi",
                }
                if pod["is_critical"]:
                    critical_pods.append(entry)
                elif not pod["is_daemonset"]:
                    non_critical_pods.append(entry)

        return {
            "critical_pod_count": len(critical_pods),
            "non_critical_pod_count": len(non_critical_pods),
            "headroom_nodes": nodes_with_headroom,
            "critical_pods": critical_pods[:15],
            "recommendation": f"{nodes_with_headroom}/{len(nodes)} nodes have headroom for burst capacity" if nodes else "No nodes",
        }

    def _is_critical(self, pod):
        labels = pod.metadata.labels or {}
        for key, val in CRITICAL_LABELS.items():
            if labels.get(key) == val:
                return True
        ns = pod.metadata.namespace
        if ns in ("atlas", "ingress-nginx"):
            return True
        return False

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
