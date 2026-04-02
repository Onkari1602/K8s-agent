import logging
import re
from kubernetes import client as k8s_client

from . import config
from .models import AnalysisResult, PodIssue, RemediationAction, RemediationReport

logger = logging.getLogger(__name__)


def parse_memory_mi(value: str) -> int:
    if not value:
        return 256
    value = str(value)
    if value.endswith("Gi"):
        return int(float(value[:-2]) * 1024)
    if value.endswith("Mi"):
        return int(float(value[:-2]))
    if value.endswith("Ki"):
        return max(1, int(float(value[:-2]) / 1024))
    m = re.match(r'^(\d+)$', value)
    if m:
        return max(1, int(int(m.group(1)) / (1024 * 1024)))
    return 256


class Remediator:
    def __init__(self, apps_v1: k8s_client.AppsV1Api, core_v1: k8s_client.CoreV1Api):
        self.apps_v1 = apps_v1
        self.core_v1 = core_v1
        self.dry_run = config.DRY_RUN

    def execute(self, analysis: AnalysisResult) -> RemediationReport:
        issue = analysis.issue

        if not config.AUTO_REMEDIATE:
            return RemediationReport(
                issue=issue, analysis=analysis,
                action_taken=RemediationAction.NO_ACTION,
                success=True, details="Auto-remediation disabled. Report only.",
            )

        if analysis.confidence < config.CONFIDENCE_THRESHOLD:
            return RemediationReport(
                issue=issue, analysis=analysis,
                action_taken=RemediationAction.NO_ACTION,
                success=True,
                details=f"Confidence {analysis.confidence:.2f} below threshold {config.CONFIDENCE_THRESHOLD}. Report only.",
            )

        if issue.namespace in config.PROTECTED_NAMESPACES:
            return RemediationReport(
                issue=issue, analysis=analysis,
                action_taken=RemediationAction.NO_ACTION,
                success=True, details=f"Namespace {issue.namespace} is protected.",
            )

        action_map = {
            RemediationAction.INCREASE_MEMORY: self._increase_memory,
            RemediationAction.RESTART_POD: self._restart_pod,
            RemediationAction.ROLLBACK_DEPLOYMENT: self._rollback_deployment,
            RemediationAction.DELETE_AND_RECREATE: self._restart_pod,
            RemediationAction.FIX_IMAGE_REFERENCE: self._report_only,
            RemediationAction.SCALE_NODE_GROUP: self._report_only,
            RemediationAction.NO_ACTION: self._report_only,
        }

        handler = action_map.get(analysis.recommended_action, self._report_only)
        try:
            return handler(issue, analysis)
        except Exception as e:
            logger.error(f"Remediation failed for {issue.namespace}/{issue.pod_name}: {e}")
            return RemediationReport(
                issue=issue, analysis=analysis,
                action_taken=analysis.recommended_action,
                success=False, details=f"Remediation failed: {e}",
            )

    def _increase_memory(self, issue: PodIssue, analysis: AnalysisResult) -> RemediationReport:
        if not issue.owner_kind or issue.owner_kind not in ("Deployment", "StatefulSet"):
            return self._report_only(issue, analysis, "Cannot increase memory: pod not owned by Deployment/StatefulSet")

        current_limit = issue.resource_limits.get("memory", "256Mi")
        current_mi = parse_memory_mi(current_limit)
        new_mi = min(
            int(current_mi * (1 + config.OOM_MEMORY_INCREASE_PERCENT / 100)),
            config.OOM_MEMORY_MAX_MI,
        )

        if new_mi <= current_mi:
            return self._report_only(issue, analysis, f"Memory already at max ({current_mi}Mi >= {config.OOM_MEMORY_MAX_MI}Mi cap)")

        patch = {
            "spec": {"template": {"spec": {"containers": [{
                "name": issue.container_name,
                "resources": {"limits": {"memory": f"{new_mi}Mi"}}
            }]}}}
        }

        dry = ["All"] if self.dry_run else None
        prefix = "[DRY-RUN] " if self.dry_run else ""

        if issue.owner_kind == "Deployment":
            self.apps_v1.patch_namespaced_deployment(issue.owner_name, issue.namespace, patch, dry_run=dry)
        else:
            self.apps_v1.patch_namespaced_stateful_set(issue.owner_name, issue.namespace, patch, dry_run=dry)

        details = f"{prefix}Increased memory limit from {current_mi}Mi to {new_mi}Mi on {issue.owner_kind}/{issue.owner_name}"
        logger.info(details)

        return RemediationReport(
            issue=issue, analysis=analysis,
            action_taken=RemediationAction.INCREASE_MEMORY,
            success=True, details=details,
        )

    def _restart_pod(self, issue: PodIssue, analysis: AnalysisResult) -> RemediationReport:
        dry = ["All"] if self.dry_run else None
        prefix = "[DRY-RUN] " if self.dry_run else ""

        self.core_v1.delete_namespaced_pod(issue.pod_name, issue.namespace, dry_run=dry)

        details = f"{prefix}Deleted pod {issue.namespace}/{issue.pod_name} for controller to recreate"
        logger.info(details)

        return RemediationReport(
            issue=issue, analysis=analysis,
            action_taken=RemediationAction.RESTART_POD,
            success=True, details=details,
        )

    def _rollback_deployment(self, issue: PodIssue, analysis: AnalysisResult) -> RemediationReport:
        if issue.owner_kind != "Deployment":
            return self._report_only(issue, analysis, "Rollback only supported for Deployments")

        try:
            rs_list = self.apps_v1.list_namespaced_replica_set(
                issue.namespace,
                label_selector=f"app={issue.owner_name.replace('-depl', '')}",
            )

            sorted_rs = sorted(
                [rs for rs in rs_list.items if rs.status.replicas and rs.status.replicas > 0],
                key=lambda r: r.metadata.creation_timestamp,
                reverse=True,
            )

            if len(sorted_rs) < 2:
                return self._report_only(issue, analysis, "No previous revision available for rollback")

            previous_rs = sorted_rs[1]
            previous_template = previous_rs.spec.template

            dry = ["All"] if self.dry_run else None
            prefix = "[DRY-RUN] " if self.dry_run else ""

            patch = {"spec": {"template": previous_template.to_dict()}}
            self.apps_v1.patch_namespaced_deployment(issue.owner_name, issue.namespace, patch, dry_run=dry)

            details = f"{prefix}Rolled back {issue.owner_kind}/{issue.owner_name} to previous revision"
            logger.info(details)

            return RemediationReport(
                issue=issue, analysis=analysis,
                action_taken=RemediationAction.ROLLBACK_DEPLOYMENT,
                success=True, details=details,
            )
        except Exception as e:
            return RemediationReport(
                issue=issue, analysis=analysis,
                action_taken=RemediationAction.ROLLBACK_DEPLOYMENT,
                success=False, details=f"Rollback failed: {e}",
            )

    def _report_only(self, issue: PodIssue, analysis: AnalysisResult, extra: str = "") -> RemediationReport:
        details = f"Report only - no automated action taken. {extra}".strip()
        return RemediationReport(
            issue=issue, analysis=analysis,
            action_taken=RemediationAction.NO_ACTION,
            success=True, details=details,
        )
