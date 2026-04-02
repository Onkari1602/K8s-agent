"""
AI Prediction Engine
- Usage forecasting: predict next 24h usage via Bedrock
- Anomaly detection: flag unusual patterns before crashes
- Capacity planning: predict when more nodes needed
"""

import os
import json
import logging
from datetime import datetime, timezone
from collections import defaultdict

import boto3
from kubernetes import client as k8s_client

from . import config

logger = logging.getLogger("prediction-engine")


class PredictionEngine:
    def __init__(self, core_v1: k8s_client.CoreV1Api, apps_v1: k8s_client.AppsV1Api):
        self.core_v1 = core_v1
        self.apps_v1 = apps_v1

        bedrock_kwargs = {"service_name": "bedrock-runtime", "region_name": config.BEDROCK_REGION}
        if config.BEDROCK_ENDPOINT_URL:
            bedrock_kwargs["endpoint_url"] = config.BEDROCK_ENDPOINT_URL
        self.bedrock = boto3.client(**bedrock_kwargs)

    def analyze(self) -> dict:
        """Run full prediction analysis."""
        logger.info("Running prediction analysis...")

        current = self._collect_current_state()
        history = self._get_cost_history()
        anomalies = self._detect_anomalies(current)
        forecast = self._forecast_usage(current, history)
        capacity = self._capacity_planning(current, history)

        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "current_state": current,
            "anomalies": anomalies,
            "forecast": forecast,
            "capacity_planning": capacity,
        }

    def _collect_current_state(self) -> dict:
        nodes = []
        namespaces = defaultdict(lambda: {"cpu_req": 0, "mem_req": 0, "pods": 0, "restarts": 0})

        try:
            node_list = self.core_v1.list_node()
            for n in node_list.items:
                cpu_cap = int(n.status.capacity.get("cpu", "1")) * 1000
                mem_cap = self._parse_mem(n.status.capacity.get("memory", "1Gi"))
                nodes.append({"cpu_cap": cpu_cap, "mem_cap": mem_cap})

            pods = self.core_v1.list_pod_for_all_namespaces()
            for p in pods.items:
                ns = p.metadata.namespace
                if ns in config.PROTECTED_NAMESPACES:
                    continue
                for c in p.spec.containers:
                    if c.resources and c.resources.requests:
                        namespaces[ns]["cpu_req"] += self._parse_cpu(c.resources.requests.get("cpu", "0"))
                        namespaces[ns]["mem_req"] += self._parse_mem(c.resources.requests.get("memory", "0"))
                namespaces[ns]["pods"] += 1
                if p.status.container_statuses:
                    namespaces[ns]["restarts"] += sum(cs.restart_count or 0 for cs in p.status.container_statuses)

        except Exception as e:
            logger.error(f"Error collecting state: {e}")

        total_cpu_cap = sum(n["cpu_cap"] for n in nodes)
        total_mem_cap = sum(n["mem_cap"] for n in nodes)
        total_cpu_req = sum(ns["cpu_req"] for ns in namespaces.values())
        total_mem_req = sum(ns["mem_req"] for ns in namespaces.values())

        return {
            "node_count": len(nodes),
            "total_cpu_capacity_m": total_cpu_cap,
            "total_mem_capacity_mi": total_mem_cap,
            "total_cpu_requested_m": total_cpu_req,
            "total_mem_requested_mi": total_mem_req,
            "cpu_utilization_pct": round(total_cpu_req / total_cpu_cap * 100, 1) if total_cpu_cap > 0 else 0,
            "mem_utilization_pct": round(total_mem_req / total_mem_cap * 100, 1) if total_mem_cap > 0 else 0,
            "total_pods": sum(ns["pods"] for ns in namespaces.values()),
            "total_restarts": sum(ns["restarts"] for ns in namespaces.values()),
            "namespaces": {k: v for k, v in sorted(namespaces.items(), key=lambda x: -x[1]["cpu_req"])[:15]},
        }

    def _get_cost_history(self) -> list:
        history = []
        try:
            cms = self.core_v1.list_namespaced_config_map(
                os.getenv("REPORT_NAMESPACE", config.REPORT_NAMESPACE),
                label_selector="app=k8s-healing-agent,type=cost-snapshot",
            )
            for cm in sorted(cms.items, key=lambda x: x.metadata.name)[-30:]:
                data = json.loads(cm.data.get("snapshot.json", "{}"))
                if data:
                    history.append(data)
        except Exception:
            pass
        return history

    def _detect_anomalies(self, current) -> dict:
        anomalies = []

        # High restart count
        for ns, data in current.get("namespaces", {}).items():
            if data["restarts"] > 50:
                anomalies.append({
                    "type": "high_restarts",
                    "severity": "critical",
                    "namespace": ns,
                    "detail": f"{data['restarts']} container restarts in namespace {ns}. Pods are unstable.",
                    "action": "Investigate CrashLoopBackOff pods in this namespace",
                })
            elif data["restarts"] > 20:
                anomalies.append({
                    "type": "elevated_restarts",
                    "severity": "warning",
                    "namespace": ns,
                    "detail": f"{data['restarts']} restarts in {ns}. Above normal threshold.",
                    "action": "Monitor for increasing trend",
                })

        # Cluster near capacity
        if current["cpu_utilization_pct"] > 80:
            anomalies.append({
                "type": "high_cpu_utilization",
                "severity": "critical",
                "namespace": "cluster",
                "detail": f"Cluster CPU at {current['cpu_utilization_pct']}%. Near capacity.",
                "action": "Scale up nodes or right-size pods",
            })
        if current["mem_utilization_pct"] > 80:
            anomalies.append({
                "type": "high_memory_utilization",
                "severity": "critical",
                "namespace": "cluster",
                "detail": f"Cluster memory at {current['mem_utilization_pct']}%. OOM risk.",
                "action": "Add nodes or reduce memory requests",
            })

        # Very low utilization (wasting money)
        if current["cpu_utilization_pct"] < 20 and current["node_count"] > 2:
            anomalies.append({
                "type": "low_utilization",
                "severity": "warning",
                "namespace": "cluster",
                "detail": f"Cluster CPU only {current['cpu_utilization_pct']}% utilized with {current['node_count']} nodes. Over-provisioned.",
                "action": "Consolidate nodes or right-size",
            })

        return {"anomalies": anomalies, "total": len(anomalies), "critical": sum(1 for a in anomalies if a["severity"] == "critical")}

    def _forecast_usage(self, current, history) -> dict:
        """Use Bedrock AI to forecast usage."""
        try:
            history_text = json.dumps(history[-7:], indent=2) if history else "No historical data available"
            current_text = json.dumps({
                "nodes": current["node_count"],
                "cpu_util": f"{current['cpu_utilization_pct']}%",
                "mem_util": f"{current['mem_utilization_pct']}%",
                "pods": current["total_pods"],
                "restarts": current["total_restarts"],
            }, indent=2)

            prompt = f"""You are a Kubernetes capacity planning expert. Based on the current state and historical data, predict the next 24 hours and 7 days.

Current State:
{current_text}

Cost History (last 7 days):
{history_text}

Provide predictions in this format:
1. **Next 24 hours**: Expected CPU/memory utilization, any risks
2. **Next 7 days**: Usage trend (growing/stable/declining), estimated cost change
3. **Scaling needs**: Will more nodes be needed? When?
4. **Risk alerts**: Any predicted issues (OOM, capacity exhaustion)

Be specific with numbers and timeframes. Keep it concise."""

            body = json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 800,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
            })
            resp = self.bedrock.invoke_model(modelId=config.BEDROCK_MODEL_ID, body=body,
                                             contentType="application/json", accept="application/json")
            forecast_text = json.loads(resp["body"].read())["content"][0]["text"]

            return {"forecast": forecast_text, "data_points": len(history), "confidence": "high" if len(history) >= 7 else "medium" if len(history) >= 3 else "low"}

        except Exception as e:
            return {"forecast": f"Forecast unavailable: {e}", "data_points": len(history), "confidence": "none"}

    def _capacity_planning(self, current, history) -> dict:
        """Predict when more nodes are needed."""
        try:
            if len(history) < 2:
                return {"plan": "Insufficient data. Need at least 2 days of cost snapshots for capacity planning.", "days_until_full": None}

            # Simple linear projection
            costs = [h.get("cluster_monthly_cost", 0) for h in history if "cluster_monthly_cost" in h]
            if len(costs) >= 2:
                daily_change = (costs[-1] - costs[0]) / len(costs) if len(costs) > 1 else 0
                trend = "growing" if daily_change > 0 else "declining" if daily_change < 0 else "stable"
            else:
                daily_change = 0
                trend = "unknown"

            cpu_headroom = 100 - current["cpu_utilization_pct"]
            mem_headroom = 100 - current["mem_utilization_pct"]
            min_headroom = min(cpu_headroom, mem_headroom)

            # Estimate days until 90% utilization (if growing)
            days_until_full = None
            if current["cpu_utilization_pct"] > 0 and trend == "growing" and daily_change > 0:
                pct_per_day = daily_change / (costs[-1] if costs[-1] > 0 else 1) * 100
                remaining_pct = 90 - current["cpu_utilization_pct"]
                days_until_full = int(remaining_pct / pct_per_day) if pct_per_day > 0 else None

            return {
                "trend": trend,
                "daily_cost_change": round(daily_change, 2),
                "current_headroom_pct": round(min_headroom, 1),
                "days_until_full": days_until_full,
                "recommendation": f"At current growth, cluster reaches 90% capacity in ~{days_until_full} days. Plan to add nodes." if days_until_full and days_until_full < 30 else "Sufficient capacity for the foreseeable future.",
                "plan": f"Trend: {trend}. Headroom: {min_headroom:.0f}%. " + (f"Full in ~{days_until_full} days." if days_until_full else "No capacity concerns."),
            }

        except Exception as e:
            return {"plan": f"Capacity planning error: {e}", "days_until_full": None}

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
