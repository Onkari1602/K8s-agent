"""
Enterprise Support Features
- Slack/Teams notifications
- Email alerts (via SES)
- SLA monitoring & reporting
- Audit logging (all agent actions)
- Role-based access (admin/viewer/operator)
- Webhook integration (generic)
- Escalation policies
"""

import os
import json
import logging
from datetime import datetime, timezone
from enum import Enum

import boto3

from . import config

logger = logging.getLogger("enterprise")


class UserRole(Enum):
    ADMIN = "admin"         # Full access + settings
    OPERATOR = "operator"   # Can click Resolve + view all
    VIEWER = "viewer"       # Read-only dashboard


class NotificationChannel(Enum):
    SLACK = "slack"
    TEAMS = "teams"
    EMAIL = "email"
    WEBHOOK = "webhook"


# =====================================================
# Notification Manager
# =====================================================
class NotificationManager:
    def __init__(self):
        self.slack_webhook = os.getenv("SLACK_WEBHOOK_URL", "")
        self.teams_webhook = os.getenv("TEAMS_WEBHOOK_URL", "")
        self.email_enabled = os.getenv("EMAIL_ALERTS_ENABLED", "false").lower() == "true"
        self.email_from = os.getenv("ALERT_EMAIL_FROM", "")
        self.email_to = [e.strip() for e in os.getenv("ALERT_EMAIL_TO", "").split(",") if e.strip()]
        self.webhook_url = os.getenv("CUSTOM_WEBHOOK_URL", "")
        self.ses_region = os.getenv("SES_REGION", config.BEDROCK_REGION)

    def notify(self, title: str, message: str, severity: str = "info", data: dict = None):
        """Send notification to all configured channels."""
        if self.slack_webhook:
            self._send_slack(title, message, severity, data)
        if self.teams_webhook:
            self._send_teams(title, message, severity, data)
        if self.email_enabled and self.email_to:
            self._send_email(title, message, severity, data)
        if self.webhook_url:
            self._send_webhook(title, message, severity, data)

    def notify_issue_detected(self, pod_name, namespace, state, root_cause):
        color = {"critical": "#e53e3e", "warning": "#ecc94b"}.get("critical" if state in ("CrashLoopBackOff", "OOMKilled") else "warning", "#3182ce")
        self.notify(
            title=f"🚨 Issue Detected: {state}",
            message=f"*Pod:* {namespace}/{pod_name}\n*State:* {state}\n*Root Cause:* {root_cause}",
            severity="critical" if state in ("CrashLoopBackOff", "OOMKilled", "Error") else "warning",
            data={"pod": pod_name, "namespace": namespace, "state": state, "root_cause": root_cause},
        )

    def notify_issue_resolved(self, pod_name, namespace, action, details):
        self.notify(
            title=f"✅ Issue Resolved: {action}",
            message=f"*Pod:* {namespace}/{pod_name}\n*Action:* {action}\n*Details:* {details}",
            severity="info",
            data={"pod": pod_name, "namespace": namespace, "action": action},
        )

    def notify_cost_alert(self, namespace, budget, current_cost):
        self.notify(
            title=f"💰 Budget Alert: {namespace}",
            message=f"*Namespace:* {namespace}\n*Budget:* ${budget}/mo\n*Current:* ${current_cost}/mo\n*Overage:* ${current_cost - budget:.0f}",
            severity="warning",
            data={"namespace": namespace, "budget": budget, "cost": current_cost},
        )

    def notify_security_alert(self, score, critical_count):
        self.notify(
            title=f"🛡️ Security Score: {score}/100",
            message=f"*Score:* {score}/100\n*Critical Issues:* {critical_count}\n*Action Required:* Review security dashboard",
            severity="critical" if score < 50 else "warning",
        )

    def notify_spot_interruption(self, node_name, pods_evicted):
        self.notify(
            title=f"⚡ Spot Interruption: {node_name}",
            message=f"*Node:* {node_name}\n*Pods Evicted:* {pods_evicted}\n*Status:* Pods rescheduled automatically",
            severity="warning",
        )

    def _send_slack(self, title, message, severity, data=None):
        try:
            import httpx
            color = {"critical": "#e53e3e", "warning": "#ecc94b", "info": "#48bb78"}.get(severity, "#63b3ed")
            payload = {
                "attachments": [{
                    "color": color,
                    "title": title,
                    "text": message,
                    "footer": "K8s Healing Agent",
                    "ts": int(datetime.now(timezone.utc).timestamp()),
                }]
            }
            httpx.post(self.slack_webhook, json=payload, timeout=10)
            logger.info(f"Slack notification sent: {title}")
        except Exception as e:
            logger.error(f"Slack notification failed: {e}")

    def _send_teams(self, title, message, severity, data=None):
        try:
            import httpx
            color = {"critical": "FF0000", "warning": "FFC107", "info": "28A745"}.get(severity, "007BFF")
            payload = {
                "@type": "MessageCard",
                "themeColor": color,
                "summary": title,
                "sections": [{
                    "activityTitle": title,
                    "text": message.replace("\n", "<br>").replace("*", "**"),
                    "facts": [{"name": k, "value": str(v)} for k, v in (data or {}).items()],
                }],
            }
            httpx.post(self.teams_webhook, json=payload, timeout=10)
            logger.info(f"Teams notification sent: {title}")
        except Exception as e:
            logger.error(f"Teams notification failed: {e}")

    def _send_email(self, title, message, severity, data=None):
        try:
            ses = boto3.client("ses", region_name=self.ses_region)
            html_body = f"""
            <h2 style="color:{'#e53e3e' if severity == 'critical' else '#ecc94b' if severity == 'warning' else '#48bb78'}">{title}</h2>
            <pre style="background:#f5f5f5; padding:15px; border-radius:8px;">{message}</pre>
            <hr><small>K8s Healing Agent | {datetime.now(timezone.utc).isoformat()}</small>
            """
            ses.send_email(
                Source=self.email_from,
                Destination={"ToAddresses": self.email_to},
                Message={
                    "Subject": {"Data": f"[K8s Agent] {title}"},
                    "Body": {"Html": {"Data": html_body}},
                },
            )
            logger.info(f"Email sent: {title}")
        except Exception as e:
            logger.error(f"Email failed: {e}")

    def _send_webhook(self, title, message, severity, data=None):
        try:
            import httpx
            payload = {
                "title": title,
                "message": message,
                "severity": severity,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "source": "k8s-healing-agent",
                "data": data or {},
            }
            httpx.post(self.webhook_url, json=payload, timeout=10)
            logger.info(f"Webhook sent: {title}")
        except Exception as e:
            logger.error(f"Webhook failed: {e}")


# =====================================================
# SLA Monitor
# =====================================================
class SLAMonitor:
    def __init__(self, core_v1: object):
        self.core_v1 = core_v1
        self.targets = json.loads(os.getenv("SLA_TARGETS", '{"uptime": 99.9, "mttr_minutes": 5}'))

    def check(self) -> dict:
        """Check SLA compliance."""
        uptime_target = self.targets.get("uptime", 99.9)
        mttr_target = self.targets.get("mttr_minutes", 5)

        total_pods = 0
        healthy_pods = 0

        try:
            pods = self.core_v1.list_pod_for_all_namespaces()
            for p in pods.items:
                if p.metadata.namespace in config.PROTECTED_NAMESPACES:
                    continue
                total_pods += 1
                if p.status.phase == "Running" and p.status.container_statuses and all(cs.ready for cs in p.status.container_statuses):
                    healthy_pods += 1
        except Exception:
            pass

        uptime = (healthy_pods / total_pods * 100) if total_pods > 0 else 100
        meets_sla = uptime >= uptime_target

        return {
            "uptime_current": round(uptime, 2),
            "uptime_target": uptime_target,
            "meets_uptime_sla": meets_sla,
            "total_pods": total_pods,
            "healthy_pods": healthy_pods,
            "unhealthy_pods": total_pods - healthy_pods,
            "mttr_target_minutes": mttr_target,
            "status": "✅ SLA Met" if meets_sla else "❌ SLA Breached",
        }


# =====================================================
# Role-Based Access
# =====================================================
class RBACManager:
    """Simple role-based access for dashboard."""

    # Default role assignments (override via RBAC_CONFIG env var)
    DEFAULT_ROLES = {
        "admin@agentichumans.in": "admin",
    }

    def __init__(self):
        roles_config = os.getenv("RBAC_CONFIG", "")
        if roles_config:
            try:
                self.roles = json.loads(roles_config)
            except Exception:
                self.roles = self.DEFAULT_ROLES
        else:
            self.roles = self.DEFAULT_ROLES

        self.default_role = os.getenv("DEFAULT_USER_ROLE", "viewer")

    def get_role(self, email: str) -> str:
        return self.roles.get(email, self.default_role)

    def can_resolve(self, email: str) -> bool:
        role = self.get_role(email)
        return role in ("admin", "operator")

    def can_configure(self, email: str) -> bool:
        return self.get_role(email) == "admin"

    def can_view(self, email: str) -> bool:
        return True  # All authenticated users can view


# =====================================================
# Escalation Policy
# =====================================================
class EscalationPolicy:
    def __init__(self, notifier: NotificationManager):
        self.notifier = notifier
        self.failure_counts = {}
        self.escalation_threshold = int(os.getenv("ESCALATION_THRESHOLD", "3"))

    def record_failure(self, target: str, issue: str) -> bool:
        """Record a failure. Returns True if escalation triggered."""
        key = f"{target}/{issue}"
        self.failure_counts[key] = self.failure_counts.get(key, 0) + 1

        if self.failure_counts[key] >= self.escalation_threshold:
            self.notifier.notify(
                title=f"🔴 ESCALATION: {target}",
                message=f"*Issue:* {issue}\n*Failed attempts:* {self.failure_counts[key]}\n*Action:* Manual intervention required. Auto-fix has failed {self.escalation_threshold}+ times.",
                severity="critical",
                data={"target": target, "issue": issue, "failures": self.failure_counts[key]},
            )
            self.failure_counts[key] = 0
            return True
        return False

    def record_success(self, target: str, issue: str):
        key = f"{target}/{issue}"
        self.failure_counts.pop(key, None)
