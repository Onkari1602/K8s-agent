import json
import logging
from datetime import datetime, timedelta, timezone

import boto3
from kubernetes import client as k8s_client

from . import config
from .models import RemediationReport

logger = logging.getLogger(__name__)


class Reporter:
    def __init__(self, core_v1: k8s_client.CoreV1Api):
        self.core_v1 = core_v1
        self.s3_client = None
        if config.REPORT_STORAGE == "s3" and config.REPORT_S3_BUCKET:
            self.s3_client = boto3.client("s3", region_name=config.BEDROCK_REGION)

    def generate_report(self, report: RemediationReport) -> str:
        content = self._format_report(report)

        try:
            self._emit_k8s_event(report)
        except Exception as e:
            logger.error(f"Failed to emit K8s event: {e}")

        try:
            if config.REPORT_STORAGE == "s3" and self.s3_client:
                self._store_in_s3(report.report_id, content)
            else:
                self._store_as_configmap(report.report_id, content, report.issue.namespace)
        except Exception as e:
            logger.error(f"Failed to store report: {e}")

        logger.info(f"Report {report.report_id} generated for {report.issue.namespace}/{report.issue.pod_name}")
        return content

    def _format_report(self, report: RemediationReport) -> str:
        analysis = report.analysis
        issue = report.issue
        prevention = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(analysis.prevention_steps))

        return f"""## Self-Healing Report: {report.report_id}
- **Timestamp**: {report.timestamp.isoformat()}Z
- **Pod**: {issue.namespace}/{issue.pod_name}
- **Container**: {issue.container_name or 'N/A'}
- **Node**: {issue.node_name or 'Not assigned'}
- **State Detected**: {issue.state.value}
- **Restart Count**: {issue.restart_count}
- **Owner**: {issue.owner_kind or 'N/A'}/{issue.owner_name or 'N/A'}

### Root Cause
{analysis.root_cause}

### AI Analysis
{analysis.explanation}

### Action Taken
- **Action**: {report.action_taken.value}
- **Success**: {report.success}
- **Confidence**: {analysis.confidence:.2f}
- **Details**: {report.details}
- **Dry Run**: {config.DRY_RUN}

### Resource Info
- **Requests**: {json.dumps(issue.resource_requests)}
- **Limits**: {json.dumps(issue.resource_limits)}

### Prevention Steps
{prevention}
"""

    def _store_as_configmap(self, report_id: str, content: str, namespace: str):
        cm = k8s_client.V1ConfigMap(
            metadata=k8s_client.V1ObjectMeta(
                name=f"healing-report-{report_id}",
                namespace=config.REPORT_NAMESPACE,
                labels={
                    "app": "k8s-healing-agent",
                    "type": "healing-report",
                },
            ),
            data={"report.md": content},
        )
        self.core_v1.create_namespaced_config_map(config.REPORT_NAMESPACE, cm)
        logger.info(f"Report stored as ConfigMap: healing-report-{report_id}")

    def _store_in_s3(self, report_id: str, content: str):
        now = datetime.now(timezone.utc)
        key = f"healing-reports/{now.strftime('%Y/%m/%d')}/{report_id}.md"
        self.s3_client.put_object(
            Bucket=config.REPORT_S3_BUCKET,
            Key=key,
            Body=content.encode("utf-8"),
            ContentType="text/markdown",
        )
        logger.info(f"Report stored in S3: s3://{config.REPORT_S3_BUCKET}/{key}")

    def _emit_k8s_event(self, report: RemediationReport):
        event = k8s_client.CoreV1Event(
            metadata=k8s_client.V1ObjectMeta(
                generate_name="healing-agent-",
                namespace=report.issue.namespace,
            ),
            involved_object=k8s_client.V1ObjectReference(
                kind="Pod",
                name=report.issue.pod_name,
                namespace=report.issue.namespace,
            ),
            reason="SelfHealing",
            message=f"[{report.action_taken.value}] {report.analysis.root_cause} | Success: {report.success} | Report: {report.report_id}",
            type="Normal" if report.success else "Warning",
            first_timestamp=report.timestamp.isoformat() + "Z",
            last_timestamp=report.timestamp.isoformat() + "Z",
            source=k8s_client.V1EventSource(component="k8s-healing-agent"),
        )
        self.core_v1.create_namespaced_event(report.issue.namespace, event)

    def cleanup_old_reports(self):
        try:
            cms = self.core_v1.list_namespaced_config_map(
                config.REPORT_NAMESPACE,
                label_selector="app=k8s-healing-agent,type=healing-report",
            )
            cutoff = datetime.now(timezone.utc) - timedelta(days=config.REPORT_RETENTION_DAYS)
            for cm in cms.items:
                if cm.metadata.creation_timestamp and cm.metadata.creation_timestamp < cutoff:
                    self.core_v1.delete_namespaced_config_map(cm.metadata.name, config.REPORT_NAMESPACE)
                    logger.info(f"Cleaned up old report: {cm.metadata.name}")
        except Exception as e:
            logger.error(f"Failed to cleanup reports: {e}")
