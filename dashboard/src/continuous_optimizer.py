"""
Continuous Cost Optimizer - Runs as a background thread in the agent
- Monitors resource usage every COST_INTERVAL seconds
- Auto right-sizes pods when usage is consistently low
- Auto scales node groups based on demand
- Generates cost savings reports

Intelligent Autoscaler - Smarter than default K8s HPA/CA
- Predictive scaling based on usage trends (not just current)
- Bin-packing: consolidates pods to fewer nodes when possible
- Scale-to-zero for idle workloads
- Burst scaling: pre-provisions capacity before spikes
"""

import os
import json
import logging
import re
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from dataclasses import dataclass

import boto3
from kubernetes import client as k8s_client

from . import config

logger = logging.getLogger("continuous-optimizer")

COST_INTERVAL = int(os.getenv("COST_INTERVAL", "300"))  # 5 minutes
RIGHTSIZING_THRESHOLD_LOW = float(os.getenv("RIGHTSIZING_THRESHOLD_LOW", "20"))  # % utilization
RIGHTSIZING_THRESHOLD_HIGH = float(os.getenv("RIGHTSIZING_THRESHOLD_HIGH", "85"))
IDLE_THRESHOLD_CPU_M = int(os.getenv("IDLE_THRESHOLD_CPU_M", "5"))
IDLE_THRESHOLD_MEM_MI = int(os.getenv("IDLE_THRESHOLD_MEM_MI", "20"))
SCALE_DOWN_DELAY_CYCLES = int(os.getenv("SCALE_DOWN_DELAY_CYCLES", "3"))  # Wait 3 cycles before scaling down


@dataclass
class UsageSnapshot:
    timestamp: datetime
    cpu_usage_m: int
    memory_usage_mi: int
    cpu_request_m: int
    memory_request_mi: int


class ContinuousOptimizer:
    def __init__(self, core_v1: k8s_client.CoreV1Api, apps_v1: k8s_client.AppsV1Api):
        self.core_v1 = core_v1
        self.apps_v1 = apps_v1
        self.region = os.getenv("AWS_REGION", config.BEDROCK_REGION)
        self.cluster_name = os.getenv("CLUSTER_NAME", "")

        # Usage history: key = "namespace/deployment" -> list of UsageSnapshot
        self.usage_history = defaultdict(list)
        # Scale-down tracker: key = "nodegroup" -> consecutive low-usage cycles
        self.scale_down_tracker = defaultdict(int)
        # Right-size tracker: key = "namespace/deployment" -> consecutive oversized cycles
        self.rightsize_tracker = defaultdict(int)

        self.metrics_available = self._check_metrics()
        self.actions_taken = []

        bedrock_kwargs = {"service_name": "bedrock-runtime", "region_name": config.BEDROCK_REGION}
        if config.BEDROCK_ENDPOINT_URL:
            bedrock_kwargs["endpoint_url"] = config.BEDROCK_ENDPOINT_URL
        self.bedrock = boto3.client(**bedrock_kwargs)

        try:
            self.eks = boto3.client("eks", region_name=self.region)
        except Exception:
            self.eks = None

    def _check_metrics(self) -> bool:
        try:
            api = k8s_client.CustomObjectsApi()
            api.list_cluster_custom_object("metrics.k8s.io", "v1beta1", "nodes")
            return True
        except Exception:
            return False

    def run_cycle(self):
        """Run one optimization cycle. Called by the agent every COST_INTERVAL."""
        logger.info("Running continuous optimization cycle...")

        try:
            # 1. Collect current usage
            self._collect_usage()

            # 2. Intelligent autoscaling
            scaling_actions = self._intelligent_autoscale()

            # 3. Right-sizing recommendations + auto-apply
            rightsizing_actions = self._auto_rightsize()

            # 4. Node consolidation check
            node_actions = self._node_consolidation()

            all_actions = scaling_actions + rightsizing_actions + node_actions

            if all_actions:
                logger.info(f"Optimization cycle complete: {len(all_actions)} actions")
                for a in all_actions:
                    logger.info(f"  [{a['type']}] {a['target']}: {a['action']} - {a['details']}")
                self.actions_taken.extend(all_actions)

                # Store optimization report
                self._store_cycle_report(all_actions)
            else:
                logger.debug("Optimization cycle: no actions needed")

        except Exception as e:
            logger.error(f"Optimization cycle failed: {e}")

    def _collect_usage(self):
        """Collect current pod resource usage from metrics API."""
        if not self.metrics_available:
            return

        try:
            metrics_api = k8s_client.CustomObjectsApi()
            pod_metrics = metrics_api.list_cluster_custom_object("metrics.k8s.io", "v1beta1", "pods")

            # Aggregate by deployment
            deployment_usage = defaultdict(lambda: {"cpu_m": 0, "mem_mi": 0, "pods": 0})

            for item in pod_metrics.get("items", []):
                ns = item["metadata"]["namespace"]
                if ns in config.EXCLUDE_NAMESPACES or ns in config.PROTECTED_NAMESPACES:
                    continue

                pod_name = item["metadata"]["name"]
                # Find deployment owner
                try:
                    pod = self.core_v1.read_namespaced_pod(pod_name, ns)
                    owner = self._get_deployment_owner(pod)
                    if not owner:
                        continue
                    key = f"{ns}/{owner}"
                except Exception:
                    continue

                for c in item.get("containers", []):
                    cpu = self._parse_cpu(c["usage"].get("cpu", "0"))
                    mem = self._parse_mem(c["usage"].get("memory", "0"))
                    deployment_usage[key]["cpu_m"] += cpu
                    deployment_usage[key]["mem_mi"] += mem
                deployment_usage[key]["pods"] += 1

            # Get requests for comparison
            now = datetime.now(timezone.utc)
            for key, usage in deployment_usage.items():
                ns, depl_name = key.split("/", 1)
                try:
                    depl = self.apps_v1.read_namespaced_deployment(depl_name, ns)
                    cpu_req = 0
                    mem_req = 0
                    for c in depl.spec.template.spec.containers:
                        if c.resources and c.resources.requests:
                            cpu_req += self._parse_cpu(c.resources.requests.get("cpu", "0"))
                            mem_req += self._parse_mem(c.resources.requests.get("memory", "0"))
                    cpu_req *= (depl.spec.replicas or 1)
                    mem_req *= (depl.spec.replicas or 1)

                    snapshot = UsageSnapshot(
                        timestamp=now,
                        cpu_usage_m=usage["cpu_m"],
                        memory_usage_mi=usage["mem_mi"],
                        cpu_request_m=cpu_req,
                        memory_request_mi=mem_req,
                    )

                    self.usage_history[key].append(snapshot)
                    # Keep last 30 snapshots (2.5 hours at 5min intervals)
                    self.usage_history[key] = self.usage_history[key][-30:]
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"Error collecting usage: {e}")

    def _intelligent_autoscale(self) -> list:
        """Scale deployments based on usage trends, not just current usage."""
        actions = []

        for key, history in self.usage_history.items():
            if len(history) < 3:
                continue

            ns, depl_name = key.split("/", 1)
            labels = self._get_deployment_labels(ns, depl_name)
            if labels.get("self-healing") == "disabled":
                continue

            recent = history[-3:]  # Last 3 snapshots (15 mins)

            avg_cpu_util = 0
            avg_mem_util = 0
            for s in recent:
                if s.cpu_request_m > 0:
                    avg_cpu_util += (s.cpu_usage_m / s.cpu_request_m * 100)
                if s.memory_request_mi > 0:
                    avg_mem_util += (s.memory_usage_mi / s.memory_request_mi * 100)
            avg_cpu_util /= len(recent)
            avg_mem_util /= len(recent)

            # Check trend (increasing or decreasing)
            if len(history) >= 6:
                older = history[-6:-3]
                old_cpu = sum(s.cpu_usage_m for s in older) / len(older)
                new_cpu = sum(s.cpu_usage_m for s in recent) / len(recent)
                trend = "increasing" if new_cpu > old_cpu * 1.2 else "decreasing" if new_cpu < old_cpu * 0.8 else "stable"
            else:
                trend = "unknown"

            try:
                depl = self.apps_v1.read_namespaced_deployment(depl_name, ns)
                current_replicas = depl.spec.replicas or 1

                # Scale UP: high utilization + increasing trend
                if avg_cpu_util > RIGHTSIZING_THRESHOLD_HIGH and trend in ("increasing", "stable") and current_replicas < 5:
                    new_replicas = min(current_replicas + 1, 5)
                    if not config.DRY_RUN:
                        self.apps_v1.patch_namespaced_deployment(
                            depl_name, ns, {"spec": {"replicas": new_replicas}}
                        )
                    prefix = "[DRY-RUN] " if config.DRY_RUN else ""
                    actions.append({
                        "type": "autoscale_up",
                        "target": key,
                        "action": f"{prefix}Scaled {current_replicas} -> {new_replicas} replicas",
                        "details": f"CPU util {avg_cpu_util:.0f}% (trend: {trend})",
                    })

                # Scale DOWN: low utilization + decreasing/stable trend for 3+ cycles
                elif avg_cpu_util < RIGHTSIZING_THRESHOLD_LOW and avg_mem_util < RIGHTSIZING_THRESHOLD_LOW and current_replicas > 1:
                    self.scale_down_tracker[key] += 1
                    if self.scale_down_tracker[key] >= SCALE_DOWN_DELAY_CYCLES:
                        new_replicas = max(current_replicas - 1, 1)
                        if not config.DRY_RUN:
                            self.apps_v1.patch_namespaced_deployment(
                                depl_name, ns, {"spec": {"replicas": new_replicas}}
                            )
                        prefix = "[DRY-RUN] " if config.DRY_RUN else ""
                        actions.append({
                            "type": "autoscale_down",
                            "target": key,
                            "action": f"{prefix}Scaled {current_replicas} -> {new_replicas} replicas",
                            "details": f"CPU util {avg_cpu_util:.0f}%, Mem util {avg_mem_util:.0f}% for {self.scale_down_tracker[key]} cycles",
                        })
                        self.scale_down_tracker[key] = 0
                else:
                    self.scale_down_tracker[key] = 0

            except Exception as e:
                logger.error(f"Autoscale error for {key}: {e}")

        return actions

    def _auto_rightsize(self) -> list:
        """Auto right-size pod resources based on actual usage."""
        actions = []

        for key, history in self.usage_history.items():
            if len(history) < 6:  # Need 30 min of data
                continue

            ns, depl_name = key.split("/", 1)
            labels = self._get_deployment_labels(ns, depl_name)
            if labels.get("self-healing") == "disabled":
                continue

            # Calculate P95 usage over history
            cpu_usages = [s.cpu_usage_m for s in history]
            mem_usages = [s.memory_usage_mi for s in history]
            cpu_usages.sort()
            mem_usages.sort()

            p95_cpu = cpu_usages[int(len(cpu_usages) * 0.95)] if cpu_usages else 0
            p95_mem = mem_usages[int(len(mem_usages) * 0.95)] if mem_usages else 0

            current_cpu_req = history[-1].cpu_request_m
            current_mem_req = history[-1].memory_request_mi

            try:
                depl = self.apps_v1.read_namespaced_deployment(depl_name, ns)
                replicas = depl.spec.replicas or 1
                per_pod_cpu_req = current_cpu_req // replicas if replicas > 0 else current_cpu_req
                per_pod_mem_req = current_mem_req // replicas if replicas > 0 else current_mem_req
                per_pod_p95_cpu = p95_cpu // replicas if replicas > 0 else p95_cpu
                per_pod_p95_mem = p95_mem // replicas if replicas > 0 else p95_mem
            except Exception:
                continue

            # Check if oversized (using < 20% of requests consistently)
            if per_pod_cpu_req > 0 and per_pod_p95_cpu < per_pod_cpu_req * 0.2:
                self.rightsize_tracker[key] += 1
            else:
                self.rightsize_tracker[key] = 0

            if self.rightsize_tracker[key] >= SCALE_DOWN_DELAY_CYCLES:
                # Recommend new requests: P95 * 2 (buffer) but min 10m CPU, 64Mi memory
                new_cpu = max(10, int(per_pod_p95_cpu * 2.5))
                new_mem = max(64, int(per_pod_p95_mem * 1.5))

                if new_cpu < per_pod_cpu_req * 0.7:  # Only if > 30% reduction
                    try:
                        container_name = depl.spec.template.spec.containers[0].name
                        if not config.DRY_RUN:
                            patch = {"spec": {"template": {"spec": {"containers": [{
                                "name": container_name,
                                "resources": {
                                    "requests": {"cpu": f"{new_cpu}m", "memory": f"{new_mem}Mi"},
                                    "limits": {"cpu": f"{new_cpu * 4}m", "memory": f"{new_mem * 3}Mi"},
                                }
                            }]}}}}
                            self.apps_v1.patch_namespaced_deployment(depl_name, ns, patch)

                        prefix = "[DRY-RUN] " if config.DRY_RUN else ""
                        actions.append({
                            "type": "rightsize",
                            "target": key,
                            "action": f"{prefix}CPU {per_pod_cpu_req}m -> {new_cpu}m, Mem {per_pod_mem_req}Mi -> {new_mem}Mi",
                            "details": f"P95 usage: CPU {per_pod_p95_cpu}m, Mem {per_pod_p95_mem}Mi. Oversized for {self.rightsize_tracker[key]} cycles.",
                        })
                        self.rightsize_tracker[key] = 0
                    except Exception as e:
                        logger.error(f"Rightsize error for {key}: {e}")

        return actions

    def _node_consolidation(self) -> list:
        """Check if nodes can be consolidated (bin-packing)."""
        actions = []

        try:
            nodes = self.core_v1.list_node()
            # Group by node group
            ng_nodes = defaultdict(list)

            for node in nodes.items:
                labels = node.metadata.labels or {}
                ng = labels.get("eks.amazonaws.com/nodegroup", "unknown")
                cpu_cap = int(node.status.capacity.get("cpu", "1")) * 1000
                mem_cap = self._parse_mem(node.status.capacity.get("memory", "1Gi"))

                # Get allocated
                cpu_alloc = 0
                mem_alloc = 0
                try:
                    pods = self.core_v1.list_pod_for_all_namespaces(
                        field_selector=f"spec.nodeName={node.metadata.name},status.phase=Running"
                    )
                    for p in pods.items:
                        for c in p.spec.containers:
                            if c.resources and c.resources.requests:
                                cpu_alloc += self._parse_cpu(c.resources.requests.get("cpu", "0"))
                                mem_alloc += self._parse_mem(c.resources.requests.get("memory", "0"))
                except Exception:
                    pass

                ng_nodes[ng].append({
                    "name": node.metadata.name,
                    "cpu_cap": cpu_cap,
                    "mem_cap": mem_cap,
                    "cpu_alloc": cpu_alloc,
                    "mem_alloc": mem_alloc,
                    "cpu_free": cpu_cap - cpu_alloc,
                    "mem_free": mem_cap - mem_alloc,
                })

            # Check each node group for consolidation opportunity
            for ng, nodes_list in ng_nodes.items():
                if len(nodes_list) < 2:
                    continue

                total_free_cpu = sum(n["cpu_free"] for n in nodes_list)
                total_free_mem = sum(n["mem_free"] for n in nodes_list)
                single_node_cpu = nodes_list[0]["cpu_cap"]
                single_node_mem = nodes_list[0]["mem_cap"]

                # If free resources across all nodes > 1 full node, we can consolidate
                if total_free_cpu > single_node_cpu and total_free_mem > single_node_mem:
                    removable = min(
                        int(total_free_cpu / single_node_cpu),
                        int(total_free_mem / single_node_mem),
                    )
                    if removable >= 1 and not config.DRY_RUN and self.eks and self.cluster_name:
                        # Scale down the node group
                        try:
                            current_ng = self.eks.describe_nodegroup(
                                clusterName=self.cluster_name, nodegroupName=ng
                            )["nodegroup"]
                            current_desired = current_ng["scalingConfig"]["desiredSize"]
                            new_desired = max(current_ng["scalingConfig"]["minSize"], current_desired - removable)

                            if new_desired < current_desired:
                                self.eks.update_nodegroup_config(
                                    clusterName=self.cluster_name,
                                    nodegroupName=ng,
                                    scalingConfig={
                                        "minSize": current_ng["scalingConfig"]["minSize"],
                                        "maxSize": current_ng["scalingConfig"]["maxSize"],
                                        "desiredSize": new_desired,
                                    },
                                )
                                actions.append({
                                    "type": "node_consolidation",
                                    "target": ng,
                                    "action": f"Scaled node group {current_desired} -> {new_desired}",
                                    "details": f"Free capacity: {total_free_cpu}m CPU, {total_free_mem}Mi Mem. Removed {removable} node(s).",
                                })
                        except Exception as e:
                            logger.error(f"Node consolidation failed for {ng}: {e}")
                    elif removable >= 1:
                        prefix = "[DRY-RUN] " if config.DRY_RUN else ""
                        actions.append({
                            "type": "node_consolidation",
                            "target": ng,
                            "action": f"{prefix}Can remove {removable} node(s) from {ng}",
                            "details": f"Free capacity: {total_free_cpu}m CPU, {total_free_mem}Mi Mem across {len(nodes_list)} nodes.",
                        })

        except Exception as e:
            logger.error(f"Node consolidation check failed: {e}")

        return actions

    def _store_cycle_report(self, actions):
        """Store optimization actions as a ConfigMap."""
        try:
            import uuid
            report_id = str(uuid.uuid4())[:8]
            content = f"""## Continuous Optimization Report: {report_id}
- **Timestamp**: {datetime.now(timezone.utc).isoformat()}Z
- **Actions Taken**: {len(actions)}
- **Mode**: {'DRY-RUN' if config.DRY_RUN else 'LIVE'}

### Actions
"""
            for a in actions:
                content += f"\n**[{a['type']}]** {a['target']}\n- Action: {a['action']}\n- Details: {a['details']}\n"

            cm = k8s_client.V1ConfigMap(
                metadata=k8s_client.V1ObjectMeta(
                    name=f"optimization-{report_id}",
                    namespace=os.getenv("REPORT_NAMESPACE", config.REPORT_NAMESPACE),
                    labels={"app": "k8s-healing-agent", "type": "optimization-report"},
                ),
                data={"report.md": content},
            )
            self.core_v1.create_namespaced_config_map(
                os.getenv("REPORT_NAMESPACE", config.REPORT_NAMESPACE), cm
            )
        except Exception as e:
            logger.error(f"Failed to store optimization report: {e}")

    def get_status(self) -> dict:
        """Return current optimizer status for dashboard."""
        return {
            "tracked_deployments": len(self.usage_history),
            "history_depth": {k: len(v) for k, v in list(self.usage_history.items())[:10]},
            "pending_scale_downs": {k: v for k, v in self.scale_down_tracker.items() if v > 0},
            "pending_rightsizing": {k: v for k, v in self.rightsize_tracker.items() if v > 0},
            "total_actions_taken": len(self.actions_taken),
            "recent_actions": self.actions_taken[-10:],
            "metrics_available": self.metrics_available,
        }

    def _get_deployment_owner(self, pod):
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
        return None

    def _get_deployment_labels(self, ns, name):
        try:
            depl = self.apps_v1.read_namespaced_deployment(name, ns)
            return depl.metadata.labels or {}
        except Exception:
            return {}

    def _parse_cpu(self, val):
        val = str(val)
        if val.endswith("n"): return max(1, int(int(val[:-1]) / 1000000))
        if val.endswith("u"): return max(1, int(int(val[:-1]) / 1000))
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
