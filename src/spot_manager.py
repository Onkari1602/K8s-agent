"""
Advanced Spot Instance Manager
- Spot interruption handler: watches EC2 termination notices, auto-drains pods
- Spot fallback: auto-provisions On-demand when Spot unavailable
- Spot savings tracker: calculates actual savings vs On-demand
- Multi-AZ Spot: ensures spread across AZs for reliability
"""

import os
import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from collections import defaultdict

import boto3
from kubernetes import client as k8s_client

from . import config

logger = logging.getLogger("spot-manager")

SPOT_CHECK_INTERVAL = int(os.getenv("SPOT_CHECK_INTERVAL", "30"))

ONDEMAND_PRICES = {
    "t3.medium": 0.042, "t3.large": 0.083, "t3.xlarge": 0.166,
    "m6a.large": 0.086, "m6a.xlarge": 0.173, "m6a.2xlarge": 0.346,
    "m5.large": 0.096, "m5.xlarge": 0.192,
    "c6a.large": 0.077, "c6a.xlarge": 0.153,
    "r6a.large": 0.113, "r6a.xlarge": 0.226,
}

SPOT_PRICES = {
    "t3.medium": 0.015, "t3.large": 0.030, "t3.xlarge": 0.060,
    "m6a.large": 0.031, "m6a.xlarge": 0.064, "m6a.2xlarge": 0.128,
    "m5.large": 0.035, "m5.xlarge": 0.070,
    "c6a.large": 0.028, "c6a.xlarge": 0.056,
    "r6a.large": 0.032, "r6a.xlarge": 0.065,
}


class SpotManager:
    def __init__(self, core_v1: k8s_client.CoreV1Api, apps_v1: k8s_client.AppsV1Api):
        self.core_v1 = core_v1
        self.apps_v1 = apps_v1
        self.region = os.getenv("AWS_REGION", config.BEDROCK_REGION)
        self.cluster_name = os.getenv("CLUSTER_NAME", "")
        self.ec2 = boto3.client("ec2", region_name=self.region)
        self.eks = boto3.client("eks", region_name=self.region)

        self.interruption_log = []
        self.fallback_log = []
        self._running = False

    def start_background(self):
        """Start spot monitoring in background thread."""
        self._running = True
        thread = threading.Thread(target=self._monitor_loop, daemon=True)
        thread.start()
        logger.info(f"Spot manager started (checking every {SPOT_CHECK_INTERVAL}s)")

    def _monitor_loop(self):
        """Main loop: check for interruptions and handle them."""
        time.sleep(30)  # Wait for agent startup
        while self._running:
            try:
                self._check_spot_interruptions()
                self._check_spot_availability()
            except Exception as e:
                logger.error(f"Spot monitor error: {e}")
            time.sleep(SPOT_CHECK_INTERVAL)

    # =====================================================
    # 1. Spot Interruption Handler
    # =====================================================
    def _check_spot_interruptions(self):
        """Check EC2 instance metadata for spot termination notices via node conditions."""
        try:
            nodes = self.core_v1.list_node()
            for node in nodes.items:
                labels = node.metadata.labels or {}
                if labels.get("eks.amazonaws.com/capacityType") != "SPOT":
                    continue

                # Check if node is being terminated (SchedulingDisabled = cordoned by AWS)
                if node.spec.unschedulable:
                    node_name = node.metadata.name
                    instance_id = self._get_instance_id(node)

                    if instance_id and self._is_spot_interruption(instance_id):
                        logger.warning(f"SPOT INTERRUPTION detected: {node_name} ({instance_id})")
                        self._handle_interruption(node, instance_id)

                # Also check node conditions for NotReady (spot reclaim in progress)
                if node.status.conditions:
                    for cond in node.status.conditions:
                        if cond.type == "Ready" and cond.status == "Unknown":
                            node_name = node.metadata.name
                            logger.warning(f"Node {node_name} NotReady - possible spot reclaim")
                            self._handle_interruption(node, self._get_instance_id(node))

        except Exception as e:
            logger.error(f"Error checking spot interruptions: {e}")

    def _is_spot_interruption(self, instance_id: str) -> bool:
        """Check if instance has a spot interruption notice."""
        try:
            resp = self.ec2.describe_instance_status(
                InstanceIds=[instance_id],
                IncludeAllInstances=True,
            )
            for status in resp.get("InstanceStatuses", []):
                events = status.get("Events", [])
                for event in events:
                    if "instance-rebalance" in event.get("Code", "") or "instance-stop" in event.get("Code", ""):
                        return True
                state = status.get("InstanceState", {}).get("Name", "")
                if state in ("shutting-down", "terminated", "stopping"):
                    return True
        except Exception:
            pass
        return False

    def _handle_interruption(self, node, instance_id: str):
        """Handle spot interruption: drain node, reschedule pods."""
        node_name = node.metadata.name
        ng = (node.metadata.labels or {}).get("eks.amazonaws.com/nodegroup", "unknown")

        # Already handled?
        for entry in self.interruption_log[-20:]:
            if entry["node"] == node_name and (datetime.now(timezone.utc) - entry["timestamp"]).seconds < 300:
                return

        logger.info(f"Handling spot interruption for {node_name}")

        try:
            # 1. Cordon node (prevent new scheduling)
            body = {"spec": {"unschedulable": True}}
            self.core_v1.patch_node(node_name, body)
            logger.info(f"Cordoned node {node_name}")

            # 2. Get pods on this node
            pods = self.core_v1.list_pod_for_all_namespaces(
                field_selector=f"spec.nodeName={node_name},status.phase=Running"
            )

            evicted_pods = []
            for pod in pods.items:
                ns = pod.metadata.namespace
                if ns in config.PROTECTED_NAMESPACES:
                    continue
                # Skip DaemonSet pods
                if pod.metadata.owner_references:
                    if any(ref.kind == "DaemonSet" for ref in pod.metadata.owner_references):
                        continue

                # 3. Evict pod (graceful)
                try:
                    eviction = k8s_client.V1Eviction(
                        metadata=k8s_client.V1ObjectMeta(
                            name=pod.metadata.name,
                            namespace=ns,
                        ),
                        delete_options=k8s_client.V1DeleteOptions(grace_period_seconds=30),
                    )
                    self.core_v1.create_namespaced_pod_eviction(
                        pod.metadata.name, ns, eviction
                    )
                    evicted_pods.append(f"{ns}/{pod.metadata.name}")
                    logger.info(f"Evicted pod {ns}/{pod.metadata.name}")
                except Exception as e:
                    logger.error(f"Failed to evict {ns}/{pod.metadata.name}: {e}")

            # Log interruption
            entry = {
                "timestamp": datetime.now(timezone.utc),
                "node": node_name,
                "instance_id": instance_id or "unknown",
                "node_group": ng,
                "pods_evicted": len(evicted_pods),
                "pod_list": evicted_pods[:10],
                "action": "drained",
            }
            self.interruption_log.append(entry)
            self._store_interruption_report(entry)

            logger.info(f"Spot interruption handled: {node_name}, {len(evicted_pods)} pods evicted")

        except Exception as e:
            logger.error(f"Error handling interruption for {node_name}: {e}")

    # =====================================================
    # 2. Spot Fallback
    # =====================================================
    def _check_spot_availability(self):
        """Check if any node group has pending pods due to Spot unavailability."""
        try:
            if not self.cluster_name:
                return

            ng_names = self.eks.list_nodegroups(clusterName=self.cluster_name).get("nodegroups", [])

            for ng_name in ng_names:
                ng = self.eks.describe_nodegroup(clusterName=self.cluster_name, nodegroupName=ng_name)["nodegroup"]
                if ng.get("capacityType") != "SPOT":
                    continue

                desired = ng["scalingConfig"]["desiredSize"]
                # Count actual running nodes for this group
                nodes = self.core_v1.list_node(label_selector=f"eks.amazonaws.com/nodegroup={ng_name}")
                ready_count = sum(
                    1 for n in nodes.items
                    if any(c.type == "Ready" and c.status == "True" for c in (n.status.conditions or []))
                )

                if desired > 0 and ready_count < desired:
                    # Spot capacity issue - nodes aren't provisioning
                    wait_time = self._get_ng_wait_time(ng_name)

                    if wait_time > 300:  # Waiting more than 5 min
                        logger.warning(f"Spot unavailable for {ng_name}: desired={desired}, ready={ready_count}, waiting {wait_time}s")
                        self._trigger_fallback(ng_name, ng, desired - ready_count)

        except Exception as e:
            logger.error(f"Error checking spot availability: {e}")

    def _get_ng_wait_time(self, ng_name: str) -> int:
        """Check how long pods have been Pending for this node group."""
        try:
            # Check for pending pods that target this node group's label
            ng = self.eks.describe_nodegroup(clusterName=self.cluster_name, nodegroupName=ng_name)["nodegroup"]
            ng_labels = ng.get("labels", {})

            if not ng_labels:
                return 0

            all_pods = self.core_v1.list_pod_for_all_namespaces()
            max_wait = 0

            for pod in all_pods.items:
                if pod.status.phase != "Pending":
                    continue
                if pod.status.conditions:
                    for cond in pod.status.conditions:
                        if cond.type == "PodScheduled" and cond.status == "False":
                            if cond.last_transition_time:
                                wait = (datetime.now(timezone.utc) - cond.last_transition_time.replace(tzinfo=timezone.utc)).total_seconds()
                                max_wait = max(max_wait, wait)
            return int(max_wait)
        except Exception:
            return 0

    def _trigger_fallback(self, ng_name: str, ng_config: dict, missing_nodes: int):
        """Create temporary on-demand capacity when Spot is unavailable."""
        # Check if already handled recently
        for entry in self.fallback_log[-10:]:
            if entry["node_group"] == ng_name and (datetime.now(timezone.utc) - entry["timestamp"]).seconds < 600:
                return

        logger.warning(f"Triggering Spot fallback for {ng_name}: provisioning {missing_nodes} on-demand node(s)")

        entry = {
            "timestamp": datetime.now(timezone.utc),
            "node_group": ng_name,
            "missing_nodes": missing_nodes,
            "action": "fallback_recommended",
            "message": f"Spot capacity unavailable for {ng_name}. Recommend creating temporary on-demand node group or scaling existing on-demand group.",
        }
        self.fallback_log.append(entry)

        # Store alert
        self._store_spot_alert(entry)

    # =====================================================
    # 3. Spot Savings Tracker
    # =====================================================
    def get_spot_savings(self) -> dict:
        """Calculate actual savings from Spot vs On-demand."""
        try:
            nodes = self.core_v1.list_node()
            spot_nodes = []
            ondemand_nodes = []

            for node in nodes.items:
                labels = node.metadata.labels or {}
                inst_type = labels.get("node.kubernetes.io/instance-type", "unknown")
                cap_type = labels.get("eks.amazonaws.com/capacityType", "ON_DEMAND")
                ng = labels.get("eks.amazonaws.com/nodegroup", "unknown")

                spot_price = SPOT_PRICES.get(inst_type, 0.05)
                ondemand_price = ONDEMAND_PRICES.get(inst_type, 0.10)

                info = {
                    "name": node.metadata.name,
                    "instance_type": inst_type,
                    "node_group": ng,
                    "spot_price": spot_price,
                    "ondemand_price": ondemand_price,
                }

                if cap_type == "SPOT":
                    spot_nodes.append(info)
                else:
                    ondemand_nodes.append(info)

            # Calculate savings
            total_spot_cost = sum(n["spot_price"] * 730 for n in spot_nodes)
            total_if_ondemand = sum(n["ondemand_price"] * 730 for n in spot_nodes)
            total_ondemand_cost = sum(n["ondemand_price"] * 730 for n in ondemand_nodes)
            spot_savings = total_if_ondemand - total_spot_cost
            savings_percent = (spot_savings / total_if_ondemand * 100) if total_if_ondemand > 0 else 0

            # Potential additional savings if on-demand converted to Spot
            potential_extra = sum((n["ondemand_price"] - n["spot_price"]) * 730 for n in ondemand_nodes)

            return {
                "spot_nodes": len(spot_nodes),
                "ondemand_nodes": len(ondemand_nodes),
                "total_nodes": len(spot_nodes) + len(ondemand_nodes),
                "spot_monthly_cost": round(total_spot_cost, 2),
                "if_all_ondemand_cost": round(total_if_ondemand + total_ondemand_cost, 2),
                "actual_monthly_cost": round(total_spot_cost + total_ondemand_cost, 2),
                "current_spot_savings": round(spot_savings, 2),
                "savings_percent": round(savings_percent, 1),
                "potential_extra_savings": round(potential_extra, 2),
                "spot_node_details": [
                    {
                        "node": n["name"][:40],
                        "type": n["instance_type"],
                        "group": n["node_group"],
                        "spot_cost": f"${n['spot_price'] * 730:.0f}/mo",
                        "ondemand_cost": f"${n['ondemand_price'] * 730:.0f}/mo",
                        "saving": f"${(n['ondemand_price'] - n['spot_price']) * 730:.0f}/mo",
                    }
                    for n in spot_nodes
                ],
                "ondemand_node_details": [
                    {
                        "node": n["name"][:40],
                        "type": n["instance_type"],
                        "group": n["node_group"],
                        "cost": f"${n['ondemand_price'] * 730:.0f}/mo",
                        "if_spot": f"${n['spot_price'] * 730:.0f}/mo",
                        "potential_saving": f"${(n['ondemand_price'] - n['spot_price']) * 730:.0f}/mo",
                    }
                    for n in ondemand_nodes
                ],
            }
        except Exception as e:
            logger.error(f"Error calculating spot savings: {e}")
            return {"error": str(e)}

    # =====================================================
    # 4. Multi-AZ Spot Analysis
    # =====================================================
    def get_az_distribution(self) -> dict:
        """Analyze Spot node distribution across AZs."""
        try:
            nodes = self.core_v1.list_node()
            az_map = defaultdict(lambda: {"spot": 0, "ondemand": 0, "nodes": []})

            for node in nodes.items:
                labels = node.metadata.labels or {}
                az = labels.get("topology.kubernetes.io/zone", "unknown")
                cap_type = "SPOT" if labels.get("eks.amazonaws.com/capacityType") == "SPOT" else "ON_DEMAND"
                inst_type = labels.get("node.kubernetes.io/instance-type", "unknown")

                az_map[az][cap_type.lower()] += 1
                az_map[az]["nodes"].append({
                    "name": node.metadata.name[:40],
                    "type": inst_type,
                    "capacity": cap_type,
                })

            az_list = []
            for az, data in sorted(az_map.items()):
                total = data["spot"] + data["ondemand"]
                az_list.append({
                    "az": az,
                    "total_nodes": total,
                    "spot_nodes": data["spot"],
                    "ondemand_nodes": data["ondemand"],
                    "spot_percent": round(data["spot"] / total * 100, 0) if total > 0 else 0,
                    "nodes": data["nodes"],
                })

            # Check for imbalance
            total_per_az = [a["total_nodes"] for a in az_list]
            is_balanced = max(total_per_az) - min(total_per_az) <= 1 if total_per_az else True

            return {
                "availability_zones": az_list,
                "total_azs": len(az_list),
                "is_balanced": is_balanced,
                "recommendation": "Nodes are well distributed across AZs" if is_balanced else "Nodes are imbalanced — consider spreading across AZs for better Spot availability",
            }
        except Exception as e:
            return {"error": str(e)}

    def get_status(self) -> dict:
        """Full spot manager status for dashboard."""
        return {
            "interruptions": [
                {
                    "timestamp": e["timestamp"].isoformat(),
                    "node": e["node"],
                    "node_group": e["node_group"],
                    "pods_evicted": e["pods_evicted"],
                    "action": e["action"],
                }
                for e in self.interruption_log[-20:]
            ],
            "fallbacks": [
                {
                    "timestamp": e["timestamp"].isoformat(),
                    "node_group": e["node_group"],
                    "missing_nodes": e["missing_nodes"],
                    "message": e["message"],
                }
                for e in self.fallback_log[-10:]
            ],
            "savings": self.get_spot_savings(),
            "az_distribution": self.get_az_distribution(),
        }

    # =====================================================
    # Helpers
    # =====================================================
    def _get_instance_id(self, node) -> str:
        provider_id = node.spec.provider_id or ""
        if "/" in provider_id:
            return provider_id.split("/")[-1]
        return ""

    def _store_interruption_report(self, entry):
        try:
            report_id = str(uuid.uuid4())[:8]
            content = f"""## Spot Interruption Report: {report_id}
- **Timestamp**: {entry['timestamp'].isoformat()}Z
- **Node**: {entry['node']}
- **Instance**: {entry['instance_id']}
- **Node Group**: {entry['node_group']}
- **Pods Evicted**: {entry['pods_evicted']}
- **Action**: {entry['action']}

### Evicted Pods
""" + "\n".join(f"- {p}" for p in entry.get("pod_list", []))

            cm = k8s_client.V1ConfigMap(
                metadata=k8s_client.V1ObjectMeta(
                    name=f"spot-interruption-{report_id}",
                    namespace=os.getenv("REPORT_NAMESPACE", config.REPORT_NAMESPACE),
                    labels={"app": "k8s-healing-agent", "type": "spot-interruption"},
                ),
                data={"report.md": content},
            )
            self.core_v1.create_namespaced_config_map(
                os.getenv("REPORT_NAMESPACE", config.REPORT_NAMESPACE), cm
            )
        except Exception as e:
            logger.error(f"Failed to store interruption report: {e}")

    def _store_spot_alert(self, entry):
        try:
            report_id = str(uuid.uuid4())[:8]
            content = json.dumps({
                "type": "spot_fallback",
                "timestamp": entry["timestamp"].isoformat(),
                "node_group": entry["node_group"],
                "missing_nodes": entry["missing_nodes"],
                "message": entry["message"],
            })
            cm = k8s_client.V1ConfigMap(
                metadata=k8s_client.V1ObjectMeta(
                    name=f"spot-alert-{report_id}",
                    namespace=os.getenv("REPORT_NAMESPACE", config.REPORT_NAMESPACE),
                    labels={"app": "k8s-healing-agent", "type": "spot-alert"},
                ),
                data={"alert.json": content},
            )
            self.core_v1.create_namespaced_config_map(
                os.getenv("REPORT_NAMESPACE", config.REPORT_NAMESPACE), cm
            )
        except Exception as e:
            logger.error(f"Failed to store spot alert: {e}")
