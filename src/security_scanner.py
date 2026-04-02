"""
Security & Compliance Scanner
- Misconfiguration: no limits, privileged, host network, root
- Image vulnerability: ECR scan results
- RBAC audit: overly permissive roles
- Network policy: namespaces without policies
- Secret exposure: secrets in env vars
- CIS Benchmark compliance report
"""

import os
import json
import logging
from datetime import datetime, timezone
from collections import defaultdict

import boto3
from kubernetes import client as k8s_client

from . import config

logger = logging.getLogger("security-scanner")

SECRET_PATTERNS = [
    "password", "secret", "token", "api_key", "apikey", "access_key",
    "private_key", "credential", "auth", "jwt", "db_pass",
]


class SecurityScanner:
    def __init__(self, core_v1: k8s_client.CoreV1Api):
        self.core_v1 = core_v1
        self.rbac_v1 = k8s_client.RbacAuthorizationV1Api()
        self.apps_v1 = k8s_client.AppsV1Api()
        self.region = os.getenv("AWS_REGION", config.BEDROCK_REGION)
        try:
            self.ecr = boto3.client("ecr", region_name=self.region)
        except Exception:
            self.ecr = None

    def full_scan(self) -> dict:
        logger.info("Starting security scan...")
        misconfigs = self._scan_misconfigurations()
        vuln = self._scan_image_vulnerabilities()
        rbac = self._audit_rbac()
        netpol = self._check_network_policies()
        secrets = self._detect_secret_exposure()

        total_issues = (
            misconfigs["total_issues"] + vuln["total_vulnerabilities"] +
            rbac["total_issues"] + netpol["unprotected_namespaces"] + secrets["total_exposures"]
        )

        critical = misconfigs["critical"] + vuln["critical_vulns"] + rbac["critical"] + secrets["critical"]
        warning = misconfigs["warning"] + vuln["high_vulns"] + netpol["unprotected_namespaces"]

        score = max(0, 100 - (critical * 10) - (warning * 3))

        logger.info(f"Security scan complete: score={score}, issues={total_issues}")

        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "security_score": score,
            "total_issues": total_issues,
            "critical_count": critical,
            "warning_count": warning,
            "misconfigurations": misconfigs,
            "vulnerabilities": vuln,
            "rbac_audit": rbac,
            "network_policies": netpol,
            "secret_exposure": secrets,
        }

    def _scan_misconfigurations(self) -> dict:
        issues = []
        critical = 0
        warning = 0

        try:
            pods = self.core_v1.list_pod_for_all_namespaces()
            for pod in pods.items:
                ns = pod.metadata.namespace
                if ns in config.PROTECTED_NAMESPACES:
                    continue

                for c in pod.spec.containers:
                    # No resource limits
                    if not c.resources or not c.resources.limits:
                        issues.append({
                            "severity": "warning",
                            "type": "no_resource_limits",
                            "pod": f"{ns}/{pod.metadata.name}",
                            "container": c.name,
                            "detail": "No resource limits set. Pod can consume unlimited resources.",
                            "fix": "Add resources.limits.cpu and resources.limits.memory",
                        })
                        warning += 1

                    # No resource requests
                    if not c.resources or not c.resources.requests:
                        issues.append({
                            "severity": "warning",
                            "type": "no_resource_requests",
                            "pod": f"{ns}/{pod.metadata.name}",
                            "container": c.name,
                            "detail": "No resource requests. Scheduler cannot make informed decisions.",
                            "fix": "Add resources.requests.cpu and resources.requests.memory",
                        })
                        warning += 1

                    # Privileged container
                    sc = c.security_context
                    if sc and sc.privileged:
                        issues.append({
                            "severity": "critical",
                            "type": "privileged_container",
                            "pod": f"{ns}/{pod.metadata.name}",
                            "container": c.name,
                            "detail": "Container runs in privileged mode. Full host access.",
                            "fix": "Set securityContext.privileged: false",
                        })
                        critical += 1

                    # Running as root
                    if sc and sc.run_as_user == 0:
                        issues.append({
                            "severity": "critical",
                            "type": "running_as_root",
                            "pod": f"{ns}/{pod.metadata.name}",
                            "container": c.name,
                            "detail": "Container runs as root (UID 0).",
                            "fix": "Set securityContext.runAsNonRoot: true",
                        })
                        critical += 1

                # Host network
                if pod.spec.host_network:
                    issues.append({
                        "severity": "critical",
                        "type": "host_network",
                        "pod": f"{ns}/{pod.metadata.name}",
                        "container": "",
                        "detail": "Pod uses host network. Can access all host network traffic.",
                        "fix": "Set hostNetwork: false unless absolutely required",
                    })
                    critical += 1

                # Host PID
                if pod.spec.host_pid:
                    issues.append({
                        "severity": "critical",
                        "type": "host_pid",
                        "pod": f"{ns}/{pod.metadata.name}",
                        "container": "",
                        "detail": "Pod shares host PID namespace. Can see all host processes.",
                        "fix": "Set hostPID: false",
                    })
                    critical += 1

        except Exception as e:
            logger.error(f"Misconfiguration scan error: {e}")

        return {"issues": issues[:50], "total_issues": len(issues), "critical": critical, "warning": warning}

    def _scan_image_vulnerabilities(self) -> dict:
        vulns = []
        critical_count = 0
        high_count = 0

        if not self.ecr:
            return {"vulnerabilities": [], "total_vulnerabilities": 0, "critical_vulns": 0, "high_vulns": 0}

        try:
            images_checked = set()
            pods = self.core_v1.list_pod_for_all_namespaces()

            for pod in pods.items:
                for c in pod.spec.containers:
                    image = c.image or ""
                    if image in images_checked or ".ecr." not in image:
                        continue
                    images_checked.add(image)

                    # Parse ECR image
                    try:
                        parts = image.split("/", 1)
                        repo_and_tag = parts[1] if len(parts) > 1 else ""
                        if ":" in repo_and_tag:
                            repo, tag = repo_and_tag.rsplit(":", 1)
                        else:
                            repo, tag = repo_and_tag, "latest"

                        findings = self.ecr.describe_image_scan_findings(
                            repositoryName=repo,
                            imageId={"imageTag": tag},
                        )

                        for finding in findings.get("imageScanFindings", {}).get("findings", [])[:5]:
                            severity = finding.get("severity", "UNKNOWN")
                            vulns.append({
                                "image": image[:60],
                                "severity": severity,
                                "name": finding.get("name", ""),
                                "description": finding.get("description", "")[:100],
                                "uri": finding.get("uri", ""),
                            })
                            if severity == "CRITICAL":
                                critical_count += 1
                            elif severity == "HIGH":
                                high_count += 1
                    except Exception:
                        pass

        except Exception as e:
            logger.error(f"Vulnerability scan error: {e}")

        return {"vulnerabilities": vulns[:30], "total_vulnerabilities": len(vulns), "critical_vulns": critical_count, "high_vulns": high_count}

    def _audit_rbac(self) -> dict:
        issues = []
        critical = 0

        try:
            # Check ClusterRoleBindings for overly permissive access
            crbs = self.rbac_v1.list_cluster_role_binding()
            for crb in crbs.items:
                role_name = crb.role_ref.name
                if role_name in ("cluster-admin", "admin"):
                    for subject in (crb.subjects or []):
                        if subject.kind == "ServiceAccount":
                            issues.append({
                                "severity": "critical",
                                "type": "overly_permissive_crb",
                                "binding": crb.metadata.name,
                                "role": role_name,
                                "subject": f"{subject.kind}/{subject.namespace}/{subject.name}",
                                "detail": f"ServiceAccount has {role_name} ClusterRole. Full cluster access.",
                                "fix": "Create a scoped Role/ClusterRole with only needed permissions",
                            })
                            critical += 1

            # Check ClusterRoles with wildcard permissions
            crs = self.rbac_v1.list_cluster_role()
            for cr in crs.items:
                if cr.metadata.name.startswith("system:"):
                    continue
                for rule in (cr.rules or []):
                    resources = rule.resources or []
                    verbs = rule.verbs or []
                    if "*" in resources and "*" in verbs:
                        issues.append({
                            "severity": "critical",
                            "type": "wildcard_permissions",
                            "role": cr.metadata.name,
                            "subject": "",
                            "detail": f"ClusterRole {cr.metadata.name} has wildcard resources AND verbs.",
                            "fix": "Restrict to specific resources and verbs",
                        })
                        critical += 1

        except Exception as e:
            logger.error(f"RBAC audit error: {e}")

        return {"issues": issues[:30], "total_issues": len(issues), "critical": critical}

    def _check_network_policies(self) -> dict:
        protected = []
        unprotected = []

        try:
            net_v1 = k8s_client.NetworkingV1Api()
            namespaces = self.core_v1.list_namespace()

            for ns in namespaces.items:
                ns_name = ns.metadata.name
                if ns_name in config.PROTECTED_NAMESPACES:
                    continue
                try:
                    policies = net_v1.list_namespaced_network_policy(ns_name)
                    if policies.items:
                        protected.append({"namespace": ns_name, "policy_count": len(policies.items)})
                    else:
                        unprotected.append({"namespace": ns_name, "detail": "No NetworkPolicy defined. All traffic allowed.", "fix": "Create default deny NetworkPolicy"})
                except Exception:
                    unprotected.append({"namespace": ns_name, "detail": "Unable to check", "fix": ""})

        except Exception as e:
            logger.error(f"Network policy check error: {e}")

        return {"protected_namespaces": protected, "unprotected": unprotected, "unprotected_namespaces": len(unprotected)}

    def _detect_secret_exposure(self) -> dict:
        exposures = []
        critical = 0

        try:
            pods = self.core_v1.list_pod_for_all_namespaces()
            for pod in pods.items:
                ns = pod.metadata.namespace
                if ns in config.PROTECTED_NAMESPACES:
                    continue

                for c in pod.spec.containers:
                    for env in (c.env or []):
                        if not env.value:
                            continue
                        env_lower = env.name.lower()
                        for pattern in SECRET_PATTERNS:
                            if pattern in env_lower and len(env.value) > 3:
                                exposures.append({
                                    "severity": "critical",
                                    "pod": f"{ns}/{pod.metadata.name}",
                                    "container": c.name,
                                    "env_var": env.name,
                                    "value_preview": env.value[:3] + "***",
                                    "detail": f"Secret '{env.name}' exposed as plain text env var.",
                                    "fix": "Use Kubernetes Secret or AWS Secrets Manager instead",
                                })
                                critical += 1
                                break

        except Exception as e:
            logger.error(f"Secret exposure scan error: {e}")

        return {"exposures": exposures[:30], "total_exposures": len(exposures), "critical": critical}

    def _parse_mem(self, val):
        val = str(val)
        if val.endswith("Gi"): return int(float(val[:-2]) * 1024)
        if val.endswith("Mi"): return int(float(val[:-2]))
        if val.endswith("Ki"): return max(1, int(float(val[:-2]) / 1024))
        try: return max(1, int(int(val) / (1024 * 1024)))
        except: return 0
